# Manga Gateway — production container image.
#
# The productionized sibling of docker/exp4a.Dockerfile (which stays a one-off
# diagnostic harness). exp4a proved the Chromium-on-Linux provisioning for THIS
# project (python:3.12-slim-bookworm + `patchright install chromium --with-deps`
# + xvfb/fonts + a uid-10001 non-root user); this image reuses that provisioning
# but installs the FULL app via uv + the committed uv.lock and runs the gateway
# as a SINGLE uvicorn process (R1 — never --workers).
#
# LOCKED decisions (06-TASKBRIEF, 2026-06-01):
#   * LD-1: Chromium only (Patchright). Camoufox (~200 MB) is NOT baked — it
#     stays a runtime `camoufox fetch` only if an operator ever flips
#     GATEWAY_CLOUDFLARE_ENGINE=camoufox.
#   * Single process: CMD is `python -m manga_gateway` (__main__.py → one
#     uvicorn.run, no workers kwarg). There is NO module-level
#     `manga_gateway.app:app` attribute — only a create_app(settings) factory —
#     so `uvicorn manga_gateway.app:app` would fail at import.
#
# SECURITY: no secret/state is baked. config.toml (the auto-generated API key),
# *.db, cloudflare-userdata*/, and *.env are excluded by .dockerignore and live
# ONLY on the /state + /data/manga volumes at runtime.

# ─────────────────────────────────────────────────────────────────────────────
# BUILDER — resolve + install the locked deps and the project into /app/.venv.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

# uv pinned to the project version (CLAUDE.md / pyproject toolchain). Copying the
# static binary from the official image avoids an extra network install step.
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

WORKDIR /app

# UV_LINK_MODE=copy: hardlinks across the image-layer boundary are unreliable;
# copy keeps the venv self-contained for the multi-stage COPY into runtime.
# UV_COMPILE_BYTECODE=1: precompile .pyc for faster cold start.
# UV_PYTHON_DOWNLOADS=0: use the base image's interpreter, never fetch one.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0

# Deps layer first — cached independently of source. --frozen forbids any
# resolution drift from the committed uv.lock (T-06-SC: no new deps introduced).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the source + README (hatchling reads README.md as the wheel's long
# description), then install the project itself into /app/.venv.
COPY src/ /app/src/
COPY README.md /app/README.md
RUN uv sync --frozen --no-dev

# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME — Chromium + xvfb + fonts + curl over the built venv; non-root; the
# single-process gateway CMD + an authenticated HEALTHCHECK.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# PATCHRIGHT/PLAYWRIGHT_BROWSERS_PATH mirror exp4a so the Chromium install lands
# at a known, chown-able path. PATH puts the venv first so `patchright` and
# `python -m manga_gateway` resolve to the installed project.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATCHRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Bring in the self-contained venv + the application source from the builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Provision Chromium + its Linux system libraries (libnss3/libgbm/fonts/...) the
# clean, documented way — `patchright install chromium --with-deps` shells out to
# apt, so it MUST run as root at build time (mirrors exp4a). LD-1: chromium only,
# never `camoufox fetch`.
RUN patchright install chromium --with-deps

# xvfb + realistic fonts power the HEADED datacenter path (GATEWAY_CLOUDFLARE_HEADLESS=false:
# the solver auto-starts an Xvfb display via pyvirtualdisplay) — reused from exp4a.
# curl is added for the authenticated HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb \
        fonts-liberation \
        fonts-noto-color-emoji \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (uid 10001, matching exp4a). State + output dirs are
# created and handed to it so the app can write the auto-generated config.toml,
# the SQLite DB, cf_clearance, and CBZ output without root.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /state /data/manga \
    && chown -R app:app /app /ms-playwright /state /data/manga

# Baked env defaults (06-TASKBRIEF "Env defaults"):
#  * GATEWAY_HOST=0.0.0.0 is LOAD-BEARING — the app default is 127.0.0.1
#    (AUTH-02), unreachable through a published port; 0.0.0.0 binds the port.
#  * The state-routed paths put config.toml (the key), the DB, and cf_clearance
#    on the /state volume so they survive container recreation (verified_facts #3).
#  * GATEWAY_CLOUDFLARE_HEADLESS=false is the image default (the solver runs HEADED
#    Chromium under the baked-in xvfb display, pyvirtualdisplay auto-starts it).
#    Headless was the historical residential default, but comix.to's Cloudflare now
#    fingerprints/blocks the headless Chromium UA ("HeadlessChrome") even from a
#    residential IP — a headless solve no longer earns cf_clearance, so /recent and
#    /search 403 (debug comix-recent-403, 2026-06-07). HEADED clears it on every
#    host the image runs on (residential + datacenter), so it is the safe default;
#    xvfb is already present (no rebuild needed to change this knob via .env).
# NOT set on purpose: GATEWAY_API_KEY (ignored by design, D-01) and
# GATEWAY_CLOUDFLARE_ENGINE (the app default `patchright` is correct; LD-1 keeps
# Camoufox out of the image entirely).
ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_CONFIG=/state/config.toml \
    GATEWAY_DB_PATH=/state/gateway.db \
    GATEWAY_HANDLE_DB_PATH=/state/handles.db \
    GATEWAY_CLOUDFLARE_USER_DATA_DIR=/state/cloudflare-userdata \
    GATEWAY_OUTPUT_ROOT=/data/manga \
    GATEWAY_CLOUDFLARE_HEADLESS=false

USER app

EXPOSE 9191
VOLUME ["/state", "/data/manga"]

# Authenticated liveness probe (verified_facts #2/#3): GET /api/v1/version
# requires the API key (global require_api_key dependency). Read the key from the
# state-volume config.toml and curl with X-Api-Key. The server binds 0.0.0.0, so
# the in-container loopback probe works. --start-period covers first-run key
# generation + the non-blocking solver warm.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD sh -c 'K=$(sed -n "s/^api_key = \"\(.*\)\"/\1/p" "$GATEWAY_CONFIG"); curl -fsS -H "X-Api-Key: $K" http://127.0.0.1:9191/api/v1/version || exit 1'

# Single uvicorn process via the module entry (R1). __main__.main() calls
# load_settings() → create_app(settings) → uvicorn.run(app, host, port) with NO
# workers kwarg. Never add --workers anywhere.
CMD ["python", "-m", "manga_gateway"]
