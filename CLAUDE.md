<!-- GSD:project-start source:PROJECT.md -->
## Project

**Manga Gateway**

A single external service that **Mangarr** (a manga library manager — a Sonarr fork) talks to over
HTTP/JSON. The gateway is **one process exposing two JSON-REST API surfaces** that share **one
authenticated secure-site session** and **one anti-bot/Cloudflare solver**:

- a **search/indexer surface** (the Jackett/Torznab analog) — fans out to many manga aggregator
  sites and returns normalized releases, each carrying an opaque `downloadHandle`; and
- a **download surface** (the SABnzbd/qBittorrent analog) — resolves a handle into a page manifest,
  fetches the chapter's images through the same session, packages them, and reports
  queue/progress/completion.

The gateway owns everything Mangarr must NOT: site sessions, Cloudflare/headless-browser challenge
solving, per-source rate limiting, release→manifest resolution, and image fetching. This repo is the
**gateway only** — there is no Mangarr code here.

**Core Value:** **Mangarr submits the same opaque release handle that search returned and gets back a packaged
chapter — without ever touching a site session, an anti-bot challenge, or a page URL.** The session
that *finds* a release is the session that *downloads* it (R1 + R6). If everything else fails, this
single-process search→handle→download→package flow must work.

### Constraints

- **Tech stack**: Python + FastAPI — chosen for typed JSON-REST + auto-OpenAPI, Playwright-Python
  for headless-browser anti-bot, httpx for async fan-out/image fetch, Pillow for image/CBZ handling.
- **Contract**: `manga-gateway.openapi.yaml` is the contract of record. When prose and the OpenAPI
  file disagree, the OpenAPI file wins. Implement it faithfully — it is the exact API Mangarr already
  expects to call.
- **Security (defensive)**: bind localhost by default; require the API key on every endpoint; treat
  `releaseHandle`/`downloadUrl` as gateway-issued tokens, never Mangarr-supplied URLs to fetch
  blindly (SSRF avoidance); never reflect arbitrary client-supplied `outputPath` writes.
- **Delivery default**: gateway archives to **CBZ** by default (contract still advertises
  `cbz`/`cbt`/`folder`). The CBZ holds **page images only** — no `ComicInfo.xml`; Mangarr adds
  metadata to the archive after hand-off.
- **Source framework**: every source extends one base class and registers via a decorator; all
  networking, anti-bot/Cloudflare, decryption, rate-limiting, and pagination live in the shared
  framework. Designed for **50+ sources** — adding one must not require custom networking/interface
  code. This is foundational and built before/with the first source.
- **Proxy-ready networking**: the framework's outbound transport is abstracted/injectable from day
  one so per-source/global proxy pools + rotation can be added later (to spread load / bypass rate
  limits) without touching source subclasses. No proxy config ships in v1 — design constraint only.
- **TTLs**: `/caps` cache 12h; `downloadHandle` TTL 60 min (≥ the 30-min floor — Mangarr caches
  interactive-search releases 30 min).
- **Poll-friendliness**: `GET /downloads` must be cheap under frequent polling (~1 min cadence,
  debounced 5s) — return cached state, don't re-scan disk per poll.
- **Build order**: search surface first — the download surface consumes the `downloadHandle` that
  search returns.
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Recommended Stack
### Core Technologies
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| **Python** | 3.12 (floor 3.11) | Runtime | 3.12 is the safe production default in 2026; `asyncio.TaskGroup` (3.11+) and per-task timeouts are exactly what the in-process job queue needs. Avoid 3.13 free-threading for v1 — Playwright/Pillow native wheels are most battle-tested on 3.12. |
| **FastAPI** | 0.136.3 | HTTP framework, auto-OpenAPI | The contract is the product here. FastAPI generates OpenAPI 3.1 from Pydantic v2 models, so the implementation and `manga-gateway.openapi.yaml` stay coupled. 0.119+ is Pydantic-v2-only; 0.136.x widened the Starlette pin to allow Starlette 1.x. |
| **Pydantic** | 2.13.4 | Contract models (request/response DTOs) | Rust-core validation; these models ARE the wire contract (`Capabilities`, `Release`, `DownloadJob`, etc.). Use `model_config = ConfigDict(populate_by_name=True)` + `Field(alias=...)` to match the OpenAPI camelCase field names (`downloadHandle`, `etaSeconds`) while keeping snake_case in Python. |
| **Uvicorn** | 0.48.0 | ASGI server | Standard FastAPI server. Run as a **single process** (`uvicorn app:app`) — this service must NOT be multi-worker (see "What NOT to Use"): the shared anti-bot session, in-memory caches, and job queue all assume one process. Install `uvicorn[standard]` for uvloop + httptools. |
### Supporting Libraries
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| **httpx** | 0.28.1 | Async HTTP client for search fan-out + parallel image fetch | One `AsyncClient` per source (or one shared with per-host limits), HTTP/2 enabled, connection pooling via `limits=httpx.Limits(...)`. Native async, same API for sync tests, integrates cleanly with anyio/asyncio. Reuse the browser-captured cookies + UA on this client for Cloudflare-cleared sources (see anti-bot below). |
| **aiolimiter** | 1.2.1 | Per-source rate limiting (token-bucket `AsyncLimiter`) | One `AsyncLimiter(rate_per_minute, time_period=60)` per `sourceKey`, keyed to the `rateLimitPerMinute` from `/caps` (MangaDex ~30, Comix ~10). `async with limiter:` around each outbound request. See the httpx caveat under "What NOT to Use" — gate at the *call site*, not via a transport hook. |
| **tenacity** | 9.1.4 | Retry/backoff for transient fetch failures | Decorate per-image and per-search outbound calls: exponential backoff + jitter, retry on 5xx/timeouts/connect errors, **stop** on 401/403/404. httpx has no built-in retry policy, so this is required, not optional. |
| **Playwright (Python)** | 1.60.0 | Headless-browser engine for anti-bot (Comix Cloudflare + encrypted-response) | The base automation layer. Drives a real browser to pass the Cloudflare challenge, then you **extract** the `cf_clearance` cookie + User-Agent and hand them to httpx for the bulk image fetch. Do NOT do image fetch through the browser. **Plain Playwright alone is now fingerprinted/blocked by current Cloudflare** — pair it with a stealth layer below. |
| **Patchright** | 1.60.0 | Drop-in stealth Playwright (Chromium, patched CDP leaks) | **Primary anti-bot recommendation.** Same API as `playwright` (`from patchright.async_api import async_playwright`), so no rewrite if you start on plain Playwright. Patches the CDP/`navigator.webdriver` leaks that get vanilla Playwright blocked, and persists the `cf_clearance` cookie across sessions. Lowest-friction path for the Comix phase. |
| **Camoufox** | 0.4.11 | Firefox-based anti-detect browser (fallback/escalation) | **Escalation option if Patchright stops passing Comix's Cloudflare.** Custom Firefox build with C++-level fingerprint spoofing — currently the strongest open-source stealth (≈0% headless detection in 2026 tests). Heavier (bundles its own Firefox); adopt only if Patchright proves insufficient. Keep the browser layer behind an interface so swapping is cheap. |
| **Pillow** | 12.2.0 | Image validation / normalization before packaging | Verify each fetched image decodes (`Image.open(...).verify()`), detect truncated/HTML-error-page "images", optionally normalize format/extension. Do NOT recompress by default (lossy; manga readers want originals). Use it as a guard, not a transcoder. |
| **zipstream-ng** | 1.9.2 | Streaming CBZ (zip) writer | Optional but recommended for large chapters: stream pages into the `.cbz` without holding the whole archive in memory. For v1 the stdlib `zipfile` (ZIP_STORED — images are already compressed, so no deflate) is sufficient and dependency-free; reach for zipstream-ng only if memory under concurrent jobs becomes an issue. |
| **lxml** | 6.1.1 | ComicInfo.xml generation | Generate the baseline `ComicInfo.xml` (series, number, language, scanlation group → `Writer`/`Translator`). stdlib `xml.etree.ElementTree` also works and needs no dependency; prefer it unless you want lxml's nicer serialization. Note PROJECT.md flags richer ComicInfo as a revisit (needs Mangarr metadata) — v1 is a minimal baseline. |
| **aiosqlite** | 0.22.1 | Async SQLite driver for job/handle persistence | Persists job state across restarts (edge case in 001 §9: "jobs SHOULD survive restart"). Async-native, no ORM overhead, fits a single-process service with modest write volume. **Recommended over SQLModel/SQLAlchemy for v1** — the schema is tiny (jobs, handles) and hand-written SQL keeps the async story simple. |
| **cachetools** | 7.1.4 | In-memory TTL caches for `/caps` and the `downloadHandle` token store | `TTLCache(maxsize=..., ttl=43200)` for the 12h `/caps` cache; `TTLCache(ttl=3600)` for the 60-min `downloadHandle` store. Synchronous and simple; guard with an `asyncio.Lock` for the caps refresh. Handle store should ALSO be mirrored to SQLite if handles must survive restart (otherwise a restart invalidates in-flight handles — acceptable per the 30-min floor, document the choice). |
| **anyio** | 4.13.0 | Structured concurrency primitives (transitive via FastAPI) | Already present (Starlette dependency). Use `anyio`/`asyncio.TaskGroup` + `Semaphore` for the bounded per-chapter fan-out. You likely don't add it explicitly — call out that the job engine is built on `asyncio.TaskGroup` + bounded semaphores, not a third-party queue. |
### Development Tools
| Tool | Version | Purpose | Notes |
|------|---------|---------|-------|
| **uv** | 0.11.17 | Dependency + venv management, lockfile, runner | **Recommended over Poetry.** Astra's uv is the 2026 default: fast resolver, `uv.lock`, `uv run`, `uv sync`, manages Python versions. `pyproject.toml`-native. |
| **ruff** | 0.15.15 | Linter + formatter | Replaces flake8 + isort + black in one tool. Enable the `F`, `E`, `I`, `UP`, `B`, `ASYNC` rule sets — `ASYNC` (flake8-async) catches blocking calls in async code, directly relevant here (e.g. sync `zipfile`/Pillow on the event loop → must offload via `asyncio.to_thread`). |
| **mypy** | 2.1.0 | Static type checking | Run in strict mode. Pydantic v2 ships its own mypy plugin; FastAPI is fully typed. Catches contract drift early. (ty/pyright are alternatives; mypy is the conservative choice with the best Pydantic plugin story.) |
| **pytest** | 8.x | Test runner | Standard. |
| **pytest-asyncio** | 1.x (≥0.24) | Async test support | `asyncio_mode = "auto"` so async test functions just work. Use httpx `ASGITransport` + `AsyncClient(transport=...)` to test the app in-process without binding a port. |
| **schemathesis** | 4.20.2 | Contract testing against the OpenAPI file | **The headline testing win.** Point it at `manga-gateway.openapi.yaml` (it supports OpenAPI 3.1 / JSON Schema 2020-12) and it property-generates requests, asserting every response conforms to the schema. Run it against the live ASGI app in CI — this is how you prove "faithful implementation of the contract of record." Wire the API key into its auth config so it hits authenticated endpoints. |
| **respx** | latest | Mock httpx in tests | Mock MangaDex/Comix HTTP responses so search/fetch logic is testable without the network. Pairs with httpx specifically. |
## Installation
# Project + deps managed by uv (pyproject.toml)
# Stealth browser binary — Camoufox (the default everywhere since #40):
#   uv run camoufox fetch    # downloads its bundled Firefox, ~200 MB, one-time
# Patchright is the opt-in escalation only (GATEWAY_CLOUDFLARE_ENGINE=patchright)
# Dev
## Alternatives Considered
| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| **httpx** | aiohttp 3.13.5 | aiohttp is faster at *very* high concurrency (1000+ simultaneous requests). This service is per-host rate-limited (10–30/min per source), so it never approaches that regime — httpx's nicer API, HTTP/2, and test ergonomics win. Switch to aiohttp only if profiling shows the client is the bottleneck under massive parallel image fetch. |
| **Camoufox** | Patchright 1.60.0 | Camoufox is the default everywhere since #40 (dev + CI + prod) — keeps Firefox-only failure modes like #54 visible in local repro. Patchright (Chromium) is the opt-in escalation when residential-IP Cloudflare evasion or a Chromium-specific page feature matters; flip via `GATEWAY_CLOUDFLARE_ENGINE=patchright`. Both back the same `AntiBotSolver` interface — swap is a config flip, not a rewrite. |
| **Patchright/Camoufox (in-process)** | FlareSolverr / Byparr (sidecar solver) | A separate HTTP-API solver service. The 002 spec mentions a `CloudflareClearanceService` (FlareSolverr/Byparr client) moving into the gateway. **Generally avoid for v1**: FlareSolverr relies on undetected-chromedriver and is widely reported as no longer passing current Cloudflare (2026); it also adds a second process, contradicting R1's single-process model. Consider only as an optional pluggable backend if you later want to offload solving. |
| **aiosqlite (raw SQL)** | SQLModel 0.0.38 / SQLAlchemy | Use SQLModel if the persisted schema grows beyond jobs+handles (e.g. per-source stats, history with rich queries) or you want Pydantic-model-as-table reuse. For v1's tiny schema, raw aiosqlite is less machinery. SQLModel is still pre-1.0 (0.0.38) — weigh that. |
| **uv** | Poetry | Poetry if the team already standardizes on it. uv is faster and is the 2026 momentum choice; no reason to pick Poetry for greenfield. |
| **stdlib zipfile** | zipstream-ng | zipfile (ZIP_STORED) is fine for v1 and dependency-free. Move to zipstream-ng when memory pressure from concurrent large-chapter archiving appears. |
| **cachetools TTLCache** | Redis / aiocache | A single-process gateway does not need an external cache. Redis only if you ever scale to multiple processes — which this service explicitly should not (R1). |
## What NOT to Use
| Avoid | Why | Use Instead |
|-------|-----|-------------|
| **Gunicorn / uvicorn with `--workers N`** | Multiple workers = multiple processes, each with its own anti-bot session, caps cache, handle store, and job queue. Breaks R1 (one shared session), idempotency on `releaseHandle` (R5), and the in-memory caches. A `downloadHandle` issued by worker A is unknown to worker B. | Single uvicorn process. Scale concurrency *within* the process via asyncio. |
| **Celery / RQ / Dramatiq / arq** | Heavyweight distributed task queues requiring a broker (Redis/RabbitMQ) and separate worker processes. Overkill and architecturally wrong for an in-process, single-process job model that must share the browser session and report progress from the same memory. | `asyncio.TaskGroup` + bounded `asyncio.Semaphore` for the job engine; SQLite for restart persistence. |
| **Plain Playwright (no stealth) for Cloudflare** | Current Cloudflare fingerprints and blocks vanilla Playwright/Chromium (`navigator.webdriver`, CDP leaks). It will work on MangaDex (antibot: none) but fail on Comix (`cloudflare+encrypted`). | Camoufox (primary, default everywhere since #40); Patchright opt-in escalation. |
| **FlareSolverr (as the v1 anti-bot core)** | Built on undetected-chromedriver; broadly reported failing current Cloudflare in 2026; adds a second process. | In-process Patchright/Camoufox. Treat any sidecar solver as an optional pluggable backend, not the default. |
| **Selenium / undetected-chromedriver** | Older automation stack, slower, weaker async story, same detection problems as vanilla Playwright. | Patchright/Camoufox. |
| **requests / aiohttp-for-everything mixing** | requests is sync and would block the event loop; mixing two HTTP clients doubles the cookie/UA/session-sharing surface (the captured `cf_clearance` must live on ONE client). | One async client (httpx) for all outbound HTTP. |
| **Running blocking work on the event loop** | Pillow decode, `zipfile` writes, and disk I/O are synchronous and CPU/IO-bound; calling them directly in an async handler stalls all jobs and stalls `GET /downloads` polling. | Offload to `asyncio.to_thread(...)` (or a `ThreadPoolExecutor`); ruff's `ASYNC` rules help catch slips. |
| **pydantic v1 / `pydantic.v1` shim** | FastAPI 0.119+ is v2-native; the shim is migration-only and will be removed. New code has no reason to touch it. | Pydantic v2 models throughout. |
| **Recompressing images by default** | Lossy transcoding degrades manga scans; readers expect originals. | Pillow only to *validate*; package originals as-is into CBZ. |
## Stack Patterns by Variant
- Pure httpx + aiolimiter + tenacity. No browser at all.
- This is the path that proves the contract end-to-end before any headless work — matches PROJECT.md's "MangaDex first, Comix later."
- Patchright launches a (persistent-context) browser, solves the challenge, captures `cf_clearance` + User-Agent.
- Inject those into the shared httpx client; do the search HTML/JSON parse and the bulk image fetch over httpx, NOT the browser (browser is only for challenge-solving + token capture + decrypting the encrypted-response payload).
- Re-solve/rotate when a request starts returning challenge pages again (detect 403 + Cloudflare markers → trigger re-clearance, back off that source via `warnings[]`).
- Keep the browser layer behind a small `AntibotSolver` interface (`get_clearance(source) -> {cookies, ua}`) so Patchright→Camoufox is a swap.
- Bounded `asyncio.Semaphore` on concurrent chapters (`maxConcurrentChapters` from `/status`) and a second semaphore on concurrent image fetches per job.
- Switch CBZ writing from stdlib `zipfile` to `zipstream-ng` to cap memory.
- Persist job rows + handle records to SQLite via aiosqlite on every state transition; rehydrate the in-memory `TTLCache`/queue on startup. Jobs interrupted mid-fetch resume as `queued` or surface as `failed` (document which — the spec allows either).
## Version Compatibility
| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| fastapi 0.136.3 | pydantic 2.13.4 | FastAPI ≥0.119 requires pydantic ≥2.7; v2-only. |
| fastapi 0.136.3 | starlette 1.2.0 | FastAPI 0.136.x widened the Starlette pin to allow Starlette 1.x (earlier 0.11x docs said `<1.0.0` — verify the resolved pin in `uv.lock`). |
| patchright 1.60.0 | playwright 1.60.0 | Patchright tracks Playwright's API/version; the captured cookies/UA feed httpx 0.28.1 unchanged. |
| schemathesis 4.20.2 | OpenAPI 3.1 | Supports OAS 2.0/3.0/3.1 (JSON Schema 2020-12). The contract file is 3.1.0 — fully supported. |
| pytest-asyncio ≥1.0 | pytest 8.x | Set `asyncio_mode = "auto"`; older 0.21.x had different fixture semantics. |
| uvicorn[standard] 0.48.0 | uvloop | uvloop unsupported on Windows — uvicorn falls back to the asyncio loop there. Relevant since this repo's dev host is Windows 11; production Linux gets uvloop. |
| camoufox 0.4.11 | playwright API | Camoufox wraps Playwright's API but ships its own Firefox build (`camoufox fetch`); heavier install than Patchright. |
## Sources
- PyPI JSON API (live, 2026-05-28) — verified current versions: fastapi 0.136.3, pydantic 2.13.4, uvicorn 0.48.0, starlette 1.2.0, httpx 0.28.1, aiohttp 3.13.5, aiolimiter 1.2.1, tenacity 9.1.4, playwright 1.60.0, patchright 1.60.0, camoufox 0.4.11, pillow 12.2.0, zipstream-ng 1.9.2, lxml 6.1.1, aiosqlite 0.22.1, cachetools 7.1.4, anyio 4.13.0, sqlmodel 0.0.38, ruff 0.15.15, mypy 2.1.0, uv 0.11.17 — **HIGH confidence**.
- Context7 `/fastapi/fastapi` (versions surfaced: 0.115–0.128 range) + FastAPI release notes — Pydantic v2-only since 0.119, Starlette pin — **HIGH**.
- FastAPI release notes (fastapi.tiangolo.com/release-notes) — Pydantic v2 default, starlette range — **HIGH**.
- Scrapfly / ZenRows / BrowserStack / techinz (Medium) Cloudflare-2026 guides — vanilla Playwright detected; Patchright (Chromium CDP-leak patch) vs Camoufox (Firefox C++ fingerprint spoof, ≈0% detection); FlareSolverr/undetected-chromedriver no longer reliable; `cf_clearance` capture/persist pattern — **MEDIUM** (fast-moving domain; re-validate at Comix phase).
- httpx GitHub discussions #1933 / #2989 — aiolimiter + httpx rate-limiting caveat (gate at call site, not transport hook) — **MEDIUM**.
- schemathesis.io / OpenAPI 3.1 guides — OAS 3.1 / JSON Schema 2020-12 support — **HIGH**.
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

- **Spike findings for MangarrGateway** (implementation patterns, constraints, gotchas) → `Skill("spike-findings-mangarrgateway")`
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.

### Branching & PR workflow (required)

`main` is reserved for reviewed, PR-gated changes. Every unit of work with code, test, or contract impact goes on its own branch and reaches `main` only through a reviewed PR:

- **Phases** — `/gsd-execute-phase` auto-creates a branch (`git.branching_strategy: phase` → `gsd/phase-{phase}-{slug}`). Open a PR from that branch back to `main` when the phase verifies.
- **Quick tasks** — `/gsd-quick` branches via `gsd/quick-{slug}`; PR back to `main`.
- **Debug sessions / ad-hoc fixes / chores with real changes** — create a descriptive branch first (e.g. `debug/{slug}`, `fix/{slug}`, `chore/{slug}`), then PR back to `main`.

**Exception — tracking-only bookkeeping commits go straight to `main`.** Commits whose entire diff is metadata bookkeeping with zero functional impact (`.planning/STATE.md` "Quick Tasks Completed" Status column updates, "Last activity" line bumps, session/phase status moves to resolved/completed, equivalent metadata-only `.planning/` edits) should be committed directly on `main` — no chore branch, no PR. CodeRabbit + CI add no value to a row-status flip, and the prior pattern (chore branch → PR → merge → delete) was pure churn.

Run the full CI gate locally before pushing PR branches (`uv run nox -s gate` — runs ruff check + ruff format --check + mypy + pytest over the whole repo, not a scoped path). Tracking-only STATE.md commits don't need the gate (pure markdown). Track any deferred/declined items as GitHub issues, the single source of truth for outstanding work.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
