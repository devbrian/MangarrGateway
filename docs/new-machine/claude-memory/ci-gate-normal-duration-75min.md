---
name: ci-gate-normal-duration-75min
description: "The GitHub Actions CI gate normally takes ~75-82 min (not minutes) — slow is normal, not stuck"
metadata: 
  node_type: memory
  type: project
  originSessionId: 05e8bbac-ceea-4ba0-b4b1-d91f631a1267
---

The `ci.yml` `gate` job on GitHub `ubuntu-latest` normally runs **~75–82 minutes** end-to-end (confirmed across many successful runs on `main` and feature branches). This is NOT a hang — do not assume a long-running gate is stuck.

Why it's ~5× the local `uv run nox -s gate` (~16 min on the dev machine):
- pytest runs **serially** (no `pytest-xdist`/`-n auto` configured).
- `tests/test_contract.py` is a CPU-heavy **schemathesis** property test that drives the full app through every generated case — the dominant cost.
- A few tests block on real `asyncio.sleep` (e.g. `test_solver_wiring.py`/`test_live_warm_best_effort.py` `sleep(10)`, `test_search_e2e.py` `sleep(5)`) — a fixed wall-clock floor.
- CI-only setup the local run skips: cold `uv sync --dev` + `patchright install --with-deps chromium` (~150–200 MB Chromium download).
- ubuntu-latest is a slow 2-vCPU runner.

**How to tell slow-vs-stuck:** `gh run view <id> --json jobs --jq '.jobs[].steps[]|select(.status!="completed")'` — if the `pytest` step is `in_progress` and the run started <~85 min ago, it's just slow. Failed runs that die in <2 min are *collection crashes* (e.g. an unwritable `/state` path), not slowness.

Potential speedups if ever worth it (deferred, not done): cache the Chromium + uv downloads in the workflow, add `pytest-xdist` (`-n auto`) after confirming the process-global collector/logging tests are worker-safe, and cap Hypothesis `max_examples` on the contract test. See [[run-live-tests-locally-before-push]] and [[ci-gate-run-full-nox-before-push]].
