# Manga Gateway

A single external service that **Mangarr** (a manga library manager — a Sonarr
fork) talks to over HTTP/JSON. The gateway is **one process exposing two
JSON-REST API surfaces** that share **one authenticated secure-site session**
and **one anti-bot/Cloudflare solver**:

- a **search/indexer surface** (the Jackett/Torznab analog) that fans out to
  many manga aggregator sites and returns normalized releases, each carrying an
  opaque `downloadHandle`; and
- a **download surface** (the SABnzbd/qBittorrent analog) that resolves a handle
  into a page manifest, fetches the chapter's images through the same session,
  packages them, and reports queue/progress/completion.

`manga-gateway.openapi.yaml` is the contract of record — when prose and the
OpenAPI file disagree, the OpenAPI file wins.

See `CLAUDE.md` for the full project brief, stack, and conventions.

## Running

The gateway is a **single-process** FastAPI app — never run it multi-worker
(the shared anti-bot session, in-memory caches, and job queue all assume one
process):

```bash
uv run python -m manga_gateway
```

Configuration is TOML-first (`config.toml`, source of truth) with
`GATEWAY_`-prefixed env vars overriding ops knobs. See `config.example.toml`.

Every endpoint requires the configured API key — sent as the `X-Api-Key` header
(or the `?apikey=` query parameter). The service binds `127.0.0.1` (localhost)
by default.

## Docker

The gateway ships as a multi-stage image (`Dockerfile`): a uv-built venv over
`python:3.12-slim-bookworm` with Chromium (Patchright), Xvfb, and fonts
provisioned for the Cloudflare path. It runs a **single uvicorn process** via
`python -m manga_gateway` (R1 — never `--workers`).

### Local (compose)

```bash
docker compose up --build          # builds + runs LOCAL source (dev override merged)
```

`docker-compose.override.yml` bind-mounts `./src` for live iteration. The image
runs a single process with no `--reload`, so the dev loop is: edit `./src`, then
`docker compose restart gateway`. For a clean prod-parity run that **excludes**
the dev bind-mount:

```bash
docker compose -f docker-compose.yml up --build
```

Copy `.env.example` → `.env` (git-ignored) to set operator knobs; the gateway
also runs with no `.env` (the image bakes safe defaults).

### Volumes & state

Two mounts persist across container recreation:

- `/state` — `config.toml` (incl. the **auto-generated API key**), `gateway.db`,
  and the Cloudflare `cf_clearance` user-data dir.
- `/data/manga` — packaged CBZ output.

Both default to Docker **named volumes** (portable — work on any host, including a
Windows dev box). To land them on specific **host directories** instead (e.g. a
Linux server), set these in `.env`:

```dotenv
MANGARR_STATE_DIR=/opt/mangarrgateway   # app data → /state
MANGARR_DOWNLOADS_DIR=/mnt/mediatrial/mangarr_dl    # downloads → /data/manga
```

The container runs **non-root (uid 10001)**, so a bound host dir must be writable
by that uid or first-run key generation fails. On the server, once:

```bash
sudo mkdir -p /opt/mangarrgateway /mnt/mediatrial/mangarr_dl
sudo chown -R 10001:10001 /opt/mangarrgateway /mnt/mediatrial/mangarr_dl
```

### Reading the API key

The API key is auto-generated into the state volume's `config.toml` on first run
(`GATEWAY_API_KEY` is **ignored** — D-01). Read it, then authenticate every
request with `X-Api-Key`:

```bash
docker compose exec gateway cat /state/config.toml   # find the api_key line
curl -H "X-Api-Key: <key>" http://127.0.0.1:9191/api/v1/version
```

**Security posture:** the container binds `0.0.0.0` so the published port is
reachable, but the API key is still required on **every** endpoint (AUTH-01). Run
it on a trusted network or behind a reverse proxy.

### Datacenter hosts (headed Chromium)

On a datacenter IP, Cloudflare blocks headless Chromium — set
`GATEWAY_CLOUDFLARE_HEADLESS=false`. Xvfb is already in the image, so the headed
path works with **no image change** (the solver auto-starts the virtual display).
Routing egress through a residential proxy (`GATEWAY_CLOUDFLARE_PROXY_*`,
issue #65) is the alternative mitigation.

### GHCR image

```bash
docker pull ghcr.io/devbrian/mangarrgateway:latest   # or a :<version>
```

Releases publish automatically on a `v*` git tag (or a manual
`workflow_dispatch`) via `.github/workflows/docker-publish.yml` — linux/amd64,
SHA-pinned actions, independent of the `ci.yml` gate.

> `docker/exp4a.Dockerfile` remains a **diagnostic harness** (the residential-IP
> Cloudflare probe), not the production image — the productionized siblings are
> the top-level `Dockerfile` + compose files above.

## Anti-bot engine & parallel Comix search

Cloudflare-gated sources (Comix) are cleared by a single shared anti-bot solver
driving a stealth browser. The engine is a config flip
(`GATEWAY_CLOUDFLARE_ENGINE`); both engines back the same `AntiBotSolver`
interface:

- **`patchright`** (Chromium-based) — the **default**. The only engine that can
  run Comix `/search` candidates in parallel.
- **`camoufox`** (Firefox-based) — opt-in fallback for hosts where Chromium's
  fingerprint is flagged (e.g. some datacenter runners, issue #35). Cannot run
  parallel.

Comix `/search` fans a series-candidate query out to one browser navigation per
candidate. Those navigations run **concurrently** (`asyncio.gather`), bounded by
`GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY` (the `CloudflareSolver._browser_lock`
Semaphore). The default is **3** — paired with the default `patchright` engine,
the candidate fan-out runs 3-at-a-time. Set it to **1** to serialize (required
for `engine=camoufox`).

**Parallelism (`fetch_concurrency > 1`) is ENGINE-SPECIFIC** — only safe on
`engine=patchright` (Chromium), which is why Chromium is the default. The
combination `engine=camoufox` + `fetch_concurrency > 1` **fails fast at startup**
(a Settings validator, #64) rather than silently returning zero results:

| Engine | `fetch_concurrency > 1` | Why |
|--------|-------------------------|-----|
| `camoufox` (Firefox) | **NOT safe — keep at 1** | Firefox stalls N>1 concurrent Cloudflare navigations on one warm context at goto-commit; the chapter-list DOM never renders. |
| `patchright` (Chromium) | **Safe** | Chromium runs N concurrent CF navigations on one shared warm context cleanly (4/4 proven on residential-IP Windows + Linux, and through a residential proxy). |

This is **engine-specific behaviour, not a Cloudflare per-IP burst limit** — the
earlier "never exceed 1" diagnosis (issue #59) was an artifact of testing only
Camoufox and is refuted (debug session `comix-parallel-engine-probe`,
2026-06-01).

Parallel Comix search is **on by default** (patchright + concurrency 3). For a
host where Chromium is flagged, fall back to camoufox — which must pin
concurrency to 1 (the guard above enforces it):

```bash
GATEWAY_CLOUDFLARE_ENGINE=camoufox \
GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY=1 \
  uv run python -m manga_gateway
```

### Headless vs headed (residential vs datacenter)

`engine=patchright` (Chromium) clears Comix's Cloudflare differently depending on
the host's IP reputation:

- **Residential IP** (dev box, residential prod): **headless works** — no display
  needed. This is the default (`cloudflare_headless=true`).
- **Datacenter IP** (cloud VPS, CI runners): Cloudflare fingerprints *headless*
  Chrome at the binary level and blocks it (the root of issue #35). **Headed**
  Chromium clears it — set `GATEWAY_CLOUDFLARE_HEADLESS=false`. On a display-less
  Linux host the solver auto-starts an **Xvfb** virtual display
  (`pyvirtualdisplay`); the host just needs the `xvfb` package installed:

  ```bash
  apt-get install -y xvfb fonts-liberation
  GATEWAY_CLOUDFLARE_ENGINE=patchright \
  GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY=3 \
  GATEWAY_CLOUDFLARE_HEADLESS=false \
    uv run python -m manga_gateway
  ```

  (An alternative datacenter mitigation is routing the egress through a
  **residential proxy** — tracked in issue #65 — but headed+Xvfb needs no proxy.)

`docker/exp4a.Dockerfile` is the documented minimal Linux Chromium deploy basis
(`python:3.12-slim-bookworm` + `patchright==1.60.0` + `patchright install
chromium --with-deps` + `xvfb`); on a datacenter host run it with
`GATEWAY_CLOUDFLARE_HEADLESS=false`.

## Development

Run the full CI gate locally before pushing:

```bash
uv run nox -s gate   # ruff check + ruff format --check + mypy + pytest (whole repo)
```

Live tests (excluded from the gate) exercise the real sources / anti-bot path:

```bash
uv run nox -s live
```
