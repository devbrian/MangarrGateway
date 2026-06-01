#!/usr/bin/env bash
# EXP-4a entrypoint — runs the engine-probe harness inside the container and
# echoes the full evidence JSON to stdout so the host (Windows dev box) can
# capture it from `docker run` output without a bind mount over the SSH context.
set -uo pipefail

echo "=================== EXP-4a CONTAINER ENV PROOF ==================="
echo "uname: $(uname -a)"
echo "python: $(python --version 2>&1)"
echo "patchright: $(python -c 'import patchright, importlib.metadata as m; print(m.version("patchright"))' 2>&1)"
echo "chromium executable_path (pre-launch):"
python - <<'PY'
import asyncio
from patchright.async_api import async_playwright
async def main():
    pw = await async_playwright().start()
    try:
        print("  ", pw.chromium.executable_path)
    finally:
        await pw.stop()
asyncio.run(main())
PY
echo "egress public IP (residential-IP confound check):"
python - <<'PY'
import urllib.request
try:
    ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
    print("  ", ip)
except Exception as exc:
    print("   <ip lookup failed:", repr(exc), ">")
PY
echo "================================================================="
echo

# The harness derives its repo root from the script location (/app/scripts/..)
# => writes evidence to /app/.planning/debug/. Ensure the dir exists.
mkdir -p /app/.planning/debug

echo "=================== EXP-4a HARNESS RUN ==================="
python /app/scripts/diagnose_comix_parallel_contention.py \
  --mode engine-probe \
  --engine patchright \
  --headless \
  --user-data-dir /tmp/cf-userdata-patchright
RC=$?
echo "=================== HARNESS EXIT CODE: $RC ==================="
echo

EVIDENCE=/app/.planning/debug/comix-parallel-engine-probe-evidence.log
if [[ -f "$EVIDENCE" ]]; then
  echo "=================== EVIDENCE FILE (container) BEGIN ==================="
  cat "$EVIDENCE"
  echo "=================== EVIDENCE FILE (container) END ==================="
else
  echo "!!! no evidence file was written at $EVIDENCE (warm likely failed pre-experiment) !!!"
fi

exit $RC
