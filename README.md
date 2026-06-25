# Manga Gateway

📖 **[Documentation &amp; Wiki](https://mangarr.github.io/)** &nbsp;·&nbsp; 💬 **[Join the Discord](https://mangarr.github.io/discord)**

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
  the Cloudflare `cf_clearance` user-data dir, the metrics snapshot DB
  (`metrics.db`), and the JSON-lines logs (`logs/gateway.jsonl`) — see
  **Observability & Metrics** below.
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

### Headed Chromium (the image default)

The Docker image bakes `GATEWAY_CLOUDFLARE_HEADLESS=false` (headed Chromium under
Xvfb) — headless Chromium is now fingerprinted/blocked by Comix's Cloudflare even
from a residential IP (debug `comix-recent-403`), so headed is the safe default on
every host. Xvfb is already in the image, so this works with **no image change**
(the solver auto-starts the virtual display). Routing egress through a residential
proxy (`GATEWAY_CLOUDFLARE_PROXY_*`, issue #65) is an alternative/additional
mitigation on flagged hosts.

### Run from GHCR (release images)

You don't need the source tree to run Manga Gateway — grab just
[`docker-compose.release.yml`](docker-compose.release.yml), which **pulls** the
published images instead of building:

```bash
# latest:
docker compose -f docker-compose.release.yml up -d
# pin a specific release (recommended):
MANGA_GATEWAY_VERSION=1.0.0 docker compose -f docker-compose.release.yml up -d
```

`MANGA_GATEWAY_VERSION` pins both owned images to one release (default `latest`).
**Image tags drop the leading `v`** — the git tag is `v1.0.0`, but the image tag
is `1.0.0` (use `1.0.0`, not `v1.0.0`).
Then read the auto-generated API key and call the API:

```bash
docker compose -f docker-compose.release.yml exec gateway cat /state/config.toml
curl -H "X-Api-Key: <key>" http://127.0.0.1:9191/api/v1/version
```

To also run the Android-WebView Cloudflare solver (mangadot / kagane / mangaball /
comix), set `COMPOSE_PROFILES=android` + `GATEWAY_ANDROID_SOLVER_API_KEY` in `.env`
first — see **Android-WebView Cloudflare solver** below for the host prerequisites
(`binder_linux`/`ashmem_linux` modules, `/dev/dri`). A bare `up` runs the gateway
alone.

**Images** (the gateway pulls fine on its own; the third is only needed for the
`android` profile):

| Image | Built from | Published |
|-------|-----------|-----------|
| `ghcr.io/devbrian/mangarrgateway` | this repo (`Dockerfile`) | on `v*` tag |
| `ghcr.io/devbrian/mangarrgateway-android-solver` | `android_solver/` | on `v*` tag |
| `redroid/redroid:11.0.0-latest` | upstream (Docker Hub) | third-party — pulled as-is |

Both owned images publish automatically on a `v*` git tag (or a manual
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

`engine=patchright` (Chromium) is driven by `GATEWAY_CLOUDFLARE_HEADLESS`, which
has **two different defaults** depending on how you run it:

- **Image / runtime default: `false` (headed).** The Docker image bakes
  `GATEWAY_CLOUDFLARE_HEADLESS=false`. As of debug `comix-recent-403`, Comix's
  Cloudflare fingerprints *headless* Chromium at the binary level and blocks it
  even from a **residential** IP (a generalization of issue #35, which first hit
  datacenter IPs) — a headless solve no longer earns `cf_clearance`, so Comix
  `/recent` and `/search` 403. **Headed** Chromium clears it on every host, so it
  is the safe default everywhere. On a display-less Linux host the solver
  auto-starts an **Xvfb** virtual display (`pyvirtualdisplay`); the host just
  needs the `xvfb` package installed.
- **App / config default: `true` (headless).** The bare-app `Settings`
  (`cloudflare_headless`) defaults to `true` for local dev on a machine with a
  **real display**, where headless is unnecessary. The shipped image overrides
  this to `false` (above).

To override — e.g. force headless on a residential dev box with a real display:

  ```bash
  apt-get install -y xvfb fonts-liberation   # only needed for the headed/Xvfb path
  GATEWAY_CLOUDFLARE_ENGINE=patchright \
  GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY=3 \
  GATEWAY_CLOUDFLARE_HEADLESS=true \
    uv run python -m manga_gateway
  ```

  (Note: headless is no longer reliable against Comix — prefer the default
  `GATEWAY_CLOUDFLARE_HEADLESS=false`. An alternative mitigation on flagged hosts
  is routing egress through a **residential proxy** — tracked in issue #65 — but
  headed+Xvfb needs no proxy.)

`docker/exp4a.Dockerfile` is the documented minimal Linux Chromium deploy basis
(`python:3.12-slim-bookworm` + `patchright==1.60.0` + `patchright install
chromium --with-deps` + `xvfb`); it runs headed via the baked
`GATEWAY_CLOUDFLARE_HEADLESS=false` default.

## Android-WebView Cloudflare solver (mangadot + kagane)

`mangadot.net` and `kagane.to` serve a **strict** Cloudflare Turnstile that the
desktop Patchright/Camoufox path **provably cannot clear from Linux** — a
mechanistic catch-22: an honest Linux fingerprint is distrusted, and spoofing a
non-Linux one contradicts the real software/GPU render output that Cloudflare's
render-consistency hash flags. This was proven across Patchright, Camoufox,
nodriver, curl_cffi TLS-impersonation, software **and** real-iGPU WebGL, and
headless **and** headed (Weston-GPU) — all fail (debug
`mangadot-cf-linux-fingerprint`). Comix's lighter challenge is **unaffected** and
stays on Patchright.

A **real Android WebView** is a different, trusted fingerprint class (the same one
the Tachiyomi `mangadotnet` extension rides). Two docker-compose sidecars provide
it, keeping all Android machinery **out of the gateway process** (R1):

- **`redroid`** — Android 11 in Docker (`redroid/redroid:11.0.0-latest`). Runs the
  bundled `org.chromium.webview_shell`. `privileged: true` and the host iGPU
  (`/dev/dri`) are **required** so Android renders WebGL on real hardware and the
  render-consistency check passes; the GPU is selected with the boot arg
  `androidboot.redroid_gpu_mode=host`. By default adb `5555` is docker-internal
  only; this deploy **publishes** it as host `15555` by operator decision for
  LAN-only device inspection (see the `REMOVE for untrusted networks` note in
  `docker-compose.yml`).
- **`android-solver`** — a thin sidecar that drives redroid over adb + CDP: load
  the challenge URL in the WebView, dynamically locate and hardware-`input tap` the
  Turnstile checkbox, poll for `cf_clearance`, and return `{cf_clearance,
  user_agent}` from an authenticated, SSRF-allowlisted `POST /solve`. The control
  API (`:8080`) is docker-internal by default; this deploy **publishes** it as host
  `18080` (LAN-only, operator decision — same `docker-compose.yml` caveat;
  `/solve` still requires `X-Solver-Key`). The gateway's `AndroidSolver` POSTs it
  and injects the returned cookie + UA on the existing shared httpx leg.

### Host prerequisite (redroid will NOT boot without it)

The host kernel must load the **`binder_linux`** (and `ashmem_linux`) modules.
Kernel 5.15-generic ships them as loadable modules (binderfs) — no rebuild. Load
and persist them on the host **once**:

```bash
sudo modprobe binder_linux ashmem_linux
printf 'binder_linux\nashmem_linux\n' | sudo tee /etc/modules-load.d/redroid.conf
```

The host must also expose the iGPU at `/dev/dri` (verified on the deploy's Alder
Lake iGPU). Without binder, `docker compose up redroid` never reaches
`sys.boot_completed=1` and the `android-solver` healthcheck dependency never goes
healthy.

### Shared egress IP (locked constraint)

`cf_clearance` is **IP-bound**: redroid and the gateway's httpx image-fetch leg
**must egress the same public IP**. The deploy NATs both through the home WAN
residential IP, so they are consistent with no extra config. If a residential
proxy is ever added for these sources (issue #65), **redroid must egress through
the SAME proxy** or the minted clearance is rejected.

### Landmine: do NOT `adb root`

`adb root` against redroid **wedges its `adbd`** — the solver must run **unrooted**.
If a device wedges, recover by restarting the `redroid` container. (The sidecar
never escalates; this is only a manual-debugging hazard.)

### Enabling on the deploy

The gateway points at the sidecar via `GATEWAY_ANDROID_SOLVER_URL` +
`GATEWAY_ANDROID_SOLVER_API_KEY` (the api key must match the sidecar's
`SOLVER_API_KEY` — compose wires both from one `.env` value; see `.env.example`).
When the URL is unset, the `AndroidSolver` leg boots mangadot/kagane **disabled**
(D-33) and the gateway still runs. Once the sidecars are up, **drop
`mangadot,kagane` from `GATEWAY_DISABLED_SOURCES`** to re-enable them.

Note the distinction: the gateway **application** runs fine without the sidecar
configured (android sources just boot disabled). The android sidecars are **opt-in**
via the `android` compose profile — a bare `docker compose up` runs the gateway
alone (works with no `.env`, exposes no extra ports). To bring up redroid +
android-solver, set `COMPOSE_PROFILES=android` in `.env` (or pass `--profile
android`); this goes **with** `GATEWAY_ANDROID_SOLVER_API_KEY`, since the sidecar
fails fast (`ConfigError`) on an empty `SOLVER_API_KEY`. With both set, the deploy's
usual `docker compose -f docker-compose.yml up --build -d` brings up the full stack.

**CI stays gated.** GitHub Actions has no `binder` kernel module, so redroid cannot
boot there — the mangadot/kagane live-smoke tests keep their `ci_skip_reason` and
skip in CI. This is a **deploy-only** capability.

### Add a solver lane (LANE-04 / DOC-01)

By default the Android stack is **one** redroid + **one** android-solver, and every
Android source shares that single device's WebView cookie jar. A **solver lane** is
a **dedicated extra redroid** for one source whose cleared `cf_clearance` must not be
wiped when another source's WebView is `pm clear`-ed (kagane is the canonical case).
A lane is purely a **deployer opt-in** — the default (no lane config) deploy needs
**zero change**.

`docker-compose.yml` ships a worked example, `redroid-kagane`, gated behind the
`lane-kagane` compose sub-profile. To enable it (or model a new lane on it):

1. **Declare the lane (gateway config).** Map the lane to its adb target and route
   the source to it — JSON env (or the equivalent `config.toml` keys):

   ```bash
   GATEWAY_ANDROID_LANES='{"kagane": "redroid-kagane:5555"}'
   GATEWAY_ANDROID_SOURCE_LANE_MAP='{"kagane": "kagane"}'
   ```

   Empty (the default) ⇒ one shared lane = today.
2. **Add the redroid lane service + its `/data` volume + the sub-profile.** Copy the
   `redroid-kagane` block in `docker-compose.yml` (rename `redroid-<lane>`,
   `redroid-<lane>-data`, and `profiles: ["lane-<lane>"]`). It mirrors the default
   `redroid` exactly — `privileged`, the **shared** host iGPU (`/dev/dri`),
   `androidboot.redroid_gpu_mode=host`, the `getprop sys.boot_completed` healthcheck,
   and adb `5555` **docker-internal only (no host port)** — but gets its **own**
   `/data` volume so its cookie jar is isolated. Add `redroid-<lane>-data:` to the
   top-level `volumes:` block.
3. **Add the lane target to `SOLVER_ADB_TARGETS`** (and optionally scope it):

   ```bash
   SOLVER_ADB_TARGETS=redroid:5555,redroid-kagane:5555
   # optional: lock the lane to only its source's host
   SOLVER_ALLOWED_HOSTS_BY_TARGET='{"redroid-kagane:5555": ["kagane.to"]}'
   ```

   The single sidecar serves every listed target (one serialized worker per device).
4. **Layer the lane sub-profile on `android`:**

   ```bash
   COMPOSE_PROFILES=android,lane-kagane
   ```

   Compose profile membership is **OR**, so the lane redroid lists only its own
   `lane-<lane>` profile; `COMPOSE_PROFILES=android` **alone** still starts exactly
   one redroid.
5. **Redeploy and validate one CF clear on the new lane.** `docker compose -f
   docker-compose.yml up --build -d`, then confirm the source clears Cloudflare end
   to end through its dedicated device (binderfs + the shared iGPU are validated for
   multiple instances by the iGPU GO spike).

**Cost & constraints.** Each lane is **+1 redroid container** — roughly **~800 MiB
RAM + ~2% CPU** — that **shares the host iGPU** (`/dev/dri`) with the default device
(no extra GPU needed). adb stays **docker-internal** on every lane (#215); the lane
never reuses the default `redroid-data` volume (a shared `/data` would re-introduce
the cross-lane `pm clear` clobber the lane exists to prevent). All the lane knobs are
documented in `.env.example`.

## Observability & Metrics

The gateway records a small event for every meaningful outbound action (HTTP
request, Cloudflare solve, rate-limit wait, CBZ package, job transition),
aggregates them in-process, and serves the results as **ready-made JSON** behind
the global API key. A **separate dashboard application** consumes this contract —
it never parses raw logs. The contract of record for these admin endpoints is the
gateway's own runtime **`/openapi.json`** (+ Swagger **`/docs`**, both behind the
API key); they are intentionally **NOT** in `manga-gateway.openapi.yaml`, which
stays Mangarr's contract of record (D-06).

### The `/admin/metrics/v1/*` contract

Six read-only endpoints, all relative to the base path `/admin/metrics/v1/` and
all behind the API key. Reads are **flat-cost** (served from in-memory
rollups/rings), so frequent polling is cheap:

| Endpoint | Params | Returns | What it is |
|----------|--------|---------|------------|
| `GET summary` | — | `Summary` object | Top-line KPIs: `total_calls`, `total_errors`, `error_rate`, `tracked_series`. |
| `GET per-source-endpoint` | — | `RollupRow[]` | One precomputed rollup per (source, endpoint): `count`, `errors`, `error_rate`, `avg_ms`, `p95_ms`, `min_ms`, `max_ms`. The core health table. |
| `GET failures` | `limit` (default 25, max 1000) | `MetricEvent[]` | The most recent **failed** calls, newest first (status, url, error, attempt). |
| `GET slow` | `limit` (default 25, max 1000) | `MetricEvent[]` | The most recent calls flagged slow **relative to their own source+endpoint baseline** (`> max(slow_factor × avg, p95)`), never a fixed ms cutoff (OBS-09). |
| `GET recent` | `limit` (default 25, max 1000) | `MetricEvent[]` | The most recent calls of any outcome — a live activity stream. |
| `GET requests/{request_id}` | path id | `RequestBreakdown` | The drill-down: `request_id`, `surface`, `endpoint`, `ts`, `total_duration_ms`, `outcome`, and the ordered `calls[]` (every child action under one inbound request). A `request_id` aged out of the ring → 404. |

A `MetricEvent` carries: `ts` (epoch seconds), `kind`
(`http`/`solve`/`package`/`limiter-wait`/`job`/`request`), `request_id`,
`surface`, `endpoint`, `source_key`, `op`, `method`, `url` (redacted), `status`,
`outcome` (`ok`/`error`), `duration_ms`, `attempt`, `error` (redacted).

The **`request_id`** is the join key (D-08): it correlates a served metric event
with the structured log lines emitted while handling the same inbound request,
and it is the path id for the `requests/{request_id}` drill-down. Treat it as
opaque.

### Auth, CORS & UrlBase

- **Auth:** every `/admin/metrics/v1/*` endpoint requires the global API key —
  the `X-Api-Key` header (or the `?apikey=` query parameter). No key / wrong key
  → `401`, same as every other gateway endpoint (AUTH-01 / SEC-01).
- **UrlBase-aware:** the router is mounted under the gateway's `root_path`, so if
  the gateway is deployed under a reverse-proxy sub-path (`GATEWAY_URL_BASE`),
  the admin surface sits behind that prefix too — do not hardcode `/admin`.
- **CORS is default-deny:** for a browser dashboard to call the gateway
  cross-origin, set `GATEWAY_METRICS_CORS_ORIGINS` to a **JSON list** of allowed
  dashboard origins (e.g. `'["http://localhost:5173"]'`). When the list is empty
  (the default) the CORS middleware is **not even added** — no origin is
  reflected. Only `GET`/`OPTIONS` are allowed; cookies are not used
  (`allow_credentials=false`); the custom `X-Api-Key` header triggers a CORS
  preflight (expected).

### Redaction (SEC-01 / D-04)

URLs and error text are scrubbed at the ingest boundary before any event reaches
the served JSON or the logs — the gateway is the only thing that should ever hold
secrets. Secret values (`cf_clearance`, `Cookie`, `Authorization`, auth tokens,
`apikey`, `x-csrf-token`) are masked to `***`. Proxy **credentials** are masked,
but the proxy **`host:port` is kept on purpose** (the scoped D-04 relaxation) so
an operator can see *which* proxy a slow/failed call used — a proxied URL appears
as `http://10.0.0.5:8080/...`, never `http://user:pass@10.0.0.5:8080/...`.

### Structured logs (JSON-lines)

The gateway writes **JSON-lines** logs to stdout and to a rotating file on the
state volume at **`/state/logs/gateway.jsonl`**. The dashboard reads this file
directly off the volume (D-09); every line is valid JSON carrying the
`request_id` join key (D-08) and is run through the same redaction as the served
metrics — no raw secret or API key is ever written.

### Configuration (the 11 observability env vars)

All are `GATEWAY_`-prefixed, env > TOML > default (D-11 ops knobs — same treatment
as `host`/`port`, **not** the API-key exclusion):

| Env var | Default | Purpose |
|---------|---------|---------|
| `GATEWAY_LOG_LEVEL` | `INFO` | Root log level for the JSON-lines dictConfig. |
| `GATEWAY_LOG_DIR` | `/state/logs` | Directory for the rotating `gateway.jsonl` file. |
| `GATEWAY_LOG_MAX_BYTES` | `10000000` | `RotatingFileHandler` `maxBytes` per file (≥1). |
| `GATEWAY_LOG_BACKUP_COUNT` | `5` | How many rotated log files to keep (≥1). |
| `GATEWAY_METRICS_CORS_ORIGINS` | `[]` (deny) | JSON list of dashboard origins allowed cross-origin. |
| `GATEWAY_METRICS_RECENT_RING` | `500` | Bounded size of the `recent` event ring (≥1). |
| `GATEWAY_METRICS_FAILURES_RING` | `200` | Bounded size of the `failures` event ring (≥1). |
| `GATEWAY_METRICS_SLOW_RING` | `200` | Bounded size of the `slow` event ring (≥1). |
| `GATEWAY_METRICS_SNAPSHOT_INTERVAL_S` | `45` | Snapshot-loop cadence; the worst-case data lost on a hard crash (≥1). |
| `GATEWAY_METRICS_SLOW_FACTOR` | `3.0` | "Slow" admission multiplier — `duration_ms > max(factor × avg, p95)` per rollup (>0). |
| `GATEWAY_METRICS_DB_PATH` | `/state/metrics.db` | The metrics snapshot DB (a **separate** SQLite file from `gateway.db` so metrics persistence never contends with the job store). |

### Restart survival

Metrics survive a restart: the store is snapshotted to `/state/metrics.db` on the
`GATEWAY_METRICS_SNAPSHOT_INTERVAL_S` cadence (plus a final snapshot on graceful
shutdown) and `rehydrate()`d on startup, so rollups/rings persist across a
gateway restart. On a *hard* crash, at most one snapshot interval (≤45s by
default) of pre-crash data is lost — metrics are diagnostic, so this bound is
accepted (OBS-04).

## Development

Run the full CI gate locally before pushing:

```bash
uv run nox -s gate   # ruff check + ruff format --check + mypy + pytest (whole repo)
```

Live tests (excluded from the gate) exercise the real sources / anti-bot path:

```bash
uv run nox -s live
```
