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
uv run uvicorn manga_gateway.app:app
```

Configuration is TOML-first (`config.toml`, source of truth) with
`GATEWAY_`-prefixed env vars overriding ops knobs. See `config.example.toml`.

Every endpoint requires the configured API key — sent as the `X-Api-Key` header
(or the `?apikey=` query parameter). The service binds `127.0.0.1` (localhost)
by default.

## Anti-bot engine & parallel Comix search

Cloudflare-gated sources (Comix) are cleared by a single shared anti-bot solver
driving a stealth browser. The engine is a config flip
(`GATEWAY_CLOUDFLARE_ENGINE`); both engines back the same `AntiBotSolver`
interface:

- **`camoufox`** (Firefox-based) — the **default everywhere** (dev, CI, prod).
- **`patchright`** (Chromium-based) — opt-in escalation.

Comix `/search` fans a series-candidate query out to one browser navigation per
candidate. Those navigations run **concurrently** (`asyncio.gather`), bounded by
`GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY` (the `CloudflareSolver._browser_lock`
Semaphore). The default is **1**, which serializes the fan-out — behaviourally
identical to a sequential loop and the safe default for every engine and host.

**Parallelism (`fetch_concurrency > 1`) is OPT-IN and ENGINE-SPECIFIC.** It is
only safe on `engine=patchright` (Chromium):

| Engine | `fetch_concurrency > 1` | Why |
|--------|-------------------------|-----|
| `camoufox` (Firefox) | **NOT safe — keep at 1** | Firefox stalls N>1 concurrent Cloudflare navigations on one warm context at goto-commit; the chapter-list DOM never renders. |
| `patchright` (Chromium) | **Safe** | Chromium runs N concurrent CF navigations on one shared warm context cleanly (4/4 proven on residential-IP Windows + Linux, and through a residential proxy). |

This is **engine-specific behaviour, not a Cloudflare per-IP burst limit** — the
earlier "never exceed 1" diagnosis (issue #59) was an artifact of testing only
Camoufox and is refuted (debug session `comix-parallel-engine-probe`,
2026-06-01).

To enable parallel Comix search:

```bash
GATEWAY_CLOUDFLARE_ENGINE=patchright \
GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY=5 \
  uv run uvicorn manga_gateway.app:app
```

### Residential-IP requirement (and the proxy mitigation)

`engine=patchright` (Chromium) needs a **residential-reputation egress IP** to
clear Comix's Cloudflare encrypted challenge. It clears cold on a residential-IP
host (Windows or Linux). A cloud **datacenter-IP** host will likely be flagged
by Cloudflare (the root of issue #35) — the mitigation is to route the Chromium
egress through a **residential proxy**, which has been verified to clear CF and
run the parallel path. CLAUDE.md's day-one proxy-ready/injectable transport is
the seam for wiring a production proxy pool (tracked as future work).

`docker/exp4a.Dockerfile` is the documented minimal basis for a residential-IP
Linux Chromium deploy (`python:3.12-slim-bookworm` + `patchright==1.60.0` +
`patchright install chromium --with-deps`).

## Development

Run the full CI gate locally before pushing:

```bash
uv run nox -s gate   # ruff check + ruff format --check + mypy + pytest (whole repo)
```

Live tests (excluded from the gate) exercise the real sources / anti-bot path:

```bash
uv run nox -s live
```
