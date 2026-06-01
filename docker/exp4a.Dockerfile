# EXP-4a — comix-parallel-engine-probe / Linux parity probe.
#
# Minimal, clean, reproducible image to answer ONE question:
#   Can Patchright/Chromium HEADLESS clear comix.to's Cloudflare encrypted+JS
#   challenge AT ALL on a residential-IP Linux host (docker context
#   `local-remote`, Ubuntu 22.04 @ 192.168.0.246)? — the #35 failure point,
#   but on residential IP (#35 was a GitHub Actions DATACENTER IP, the confound
#   this experiment isolates).
#
# It runs scripts/diagnose_comix_parallel_contention.py --mode engine-probe
# --engine patchright --headless against comix.to. The harness logs the resolved
# engine + chromium executable_path INSIDE the container (engine-swap proof).
#
# This Dockerfile (gitignored harness tooling, NOT application src) may become
# the prod container basis if EXP-4a passes. patchright==1.60.0 matches the
# project pin (pyproject.toml). Chromium + system libs installed via
# `patchright install chromium --with-deps` per the session directive.
#
# Build context is filtered by docker/exp4a.dockerignore (passed via
# --file + a copied .dockerignore) to keep the SSH-transferred context small.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATCHRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# patchright pinned to the project version (pyproject.toml). No other runtime
# dep is needed: the harness imports only manga_gateway.framework.antibot, whose
# transitive imports are stdlib + the lazy patchright import.
RUN pip install --no-cache-dir patchright==1.60.0

# Install Chromium + ALL its system libraries (fonts, libnss3, libgbm, etc.).
# `--with-deps` is the clean, documented way to provision the Linux system libs
# Chromium needs (it shells out to apt). This is exactly the prod-provisioning
# command from the session directive / CLAUDE.md install notes.
RUN patchright install chromium --with-deps

WORKDIR /app

# Only the package + the harness (build context already filtered). The package
# is placed on PYTHONPATH via the harness's own sys.path.insert(src), but we
# also export it so `import manga_gateway...` works regardless of cwd.
COPY src/ /app/src/
COPY scripts/diagnose_comix_parallel_contention.py /app/scripts/diagnose_comix_parallel_contention.py
COPY docker/exp4a-entrypoint.sh /app/exp4a-entrypoint.sh

ENV PYTHONPATH=/app/src

# Make the entrypoint executable and drop root privileges: create an
# unprivileged user and hand it the app tree + the Chromium install. Reduces
# blast radius given this image is the documented residential-IP deploy basis.
# Runtime writes go to /app/.planning/debug (chowned) and /tmp (world-writable).
RUN chmod +x /app/exp4a-entrypoint.sh \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app /ms-playwright
USER app

# Default: run the engine-probe (warm + 4-wide parallel) headless under Chromium.
ENTRYPOINT ["/app/exp4a-entrypoint.sh"]
