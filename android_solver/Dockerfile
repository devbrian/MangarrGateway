# android-solver sidecar — thin control-API image.
#
# Build context is THIS directory (`android_solver/`). The image carries adb +
# the solver package ONLY; it never contains gateway source. That boundary is
# what keeps R1 — all Android/adb machinery lives in this sidecar, out of the
# gateway process. (The gateway image is built separately from the repo root.)
#
# SECURITY (SEC-01, threat T-10-10): NO secret/state is baked. The api key and
# the redroid adb target arrive at runtime via env only (SOLVER_API_KEY /
# SOLVER_ADB_TARGET); .dockerignore keeps any *.env / caches out of the build
# context so nothing sensitive enters a layer.

FROM python:3.12-slim-bookworm

# The adb CLI — the platform-tools SYSTEM package (apt `android-tools-adb`). The
# solver drives redroid by shelling out to `adb` (device.py); there is NO pip adb
# dependency (adbutils was rejected at the 10-01 supply-chain gate).
RUN apt-get update \
    && apt-get install -y --no-install-recommends android-tools-adb \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Approved supply-chain pins first (cached independently of the source layer).
# requirements.txt carries websocket-client==1.9.0 (10-01 gate T-10-SC) and
# pproxy==2.7.9 — the auth-injecting CONNECT hop dep (11-01 gate T-11-SC); both
# flow in automatically through this unchanged install layer on rebuild.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The sidecar package code ONLY (the adb driver + CDP extractor + Turnstile
# locator + control API). Copied into an `android_solver/` package dir so the
# absolute `android_solver.*` imports resolve under `python -m`.
COPY *.py ./android_solver/

# Non-root runtime user (uid 10002, distinct from the gateway image's 10001).
RUN useradd --create-home --uid 10002 solver \
    && chown -R solver:solver /app
USER solver

# Control API port — docker-internal only; the compose service publishes NO host
# port (enforced in 10-05), so redroid/adb is never reachable from outside the
# docker network (threat T-10-12).
EXPOSE 8080

# Dependency-free liveness probe: GET /healthz (unauthenticated) is 200 when the
# redroid adb target answers. Uses stdlib urllib so no curl needs installing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz', timeout=4).status==200 else 1)"]

# Launch the control API (stdlib http.server — no web framework dependency).
CMD ["python", "-m", "android_solver.service"]
