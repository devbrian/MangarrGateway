---
name: run-live-tests-locally-before-push
description: "Before pushing a PR or merging changes that could affect the live test surface (anything in src/manga_gateway/sources/, framework/antibot.py, framework/solver_lifecycle.py, framework/fanout.py, the comix/mangadex sources, or anything that touches the warm browser / Cloudflare path), run the relevant live tests locally and inspect the results. The CI nox -s gate excludes -m live, so the deterministic gate is silent on these changes — local live runs are the only way to catch regressions BEFORE the nightly does."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 5d547995-c78a-4512-b972-f13ce05bb4fb
---

For Mangarr Gateway, when a change touches anything that could affect live-test behavior — comix.py, mangadex.py, framework/antibot.py (fetch_via_browser, the dead-driver shim, Semaphore/lock changes), framework/solver_lifecycle.py, framework/fanout.py, or anything in the warm-browser / Cloudflare path — **run the relevant live tests locally before pushing the PR, and inspect the captured log for regressions**.

**Why:** The deterministic gate (`uv run nox -s gate`) excludes `-m live` by design, so it stays fast and offline. That means the gate is *silent* on changes that affect the actual live behavior. Before this preference was set, code shipped to `main` and then waited for the 03:00 UTC nightly to surface regressions — a 6–24h feedback loop. Running live locally before push catches the same regressions in 4–8 min.

**How to apply:**
- **At minimum**, run the test most likely to fire on the changed code path. For comix changes that's typically `tests/live/test_search_smoke.py::test_search_returns_releases -k comix` or `tests/live/test_recent_download_smoke.py::test_recent_download_full_cycle -k comix`. For framework/antibot changes that affect all sources, the broader `uv run nox -s live` is correct.
- Set `GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1` if the change affects the warm-browser path so we capture the same diagnostic stream the nightly does.
- Inspect the log even if the test passed — wall-clock close to the 20s `fanout.py` budget, dead-driver retries firing, TargetClosedError future leaks, and similar are all things the test exit code may not reveal but the log will.
- Camoufox is the dev default; first time on a new dev box requires `uv run camoufox fetch` (~200 MB).
- Live tests against comix.to are real network hits — fine in dev, but skip if you're on a metered or rate-limited connection.
- The `→` arrow encoding bug in `test_recent_download_smoke.py:137` makes that specific test report FAILED on Windows cp1252 consoles even when the download succeeds — read the log, not just the exit code (see [[windows-cp1252-test-print-bug]] if/when that's tracked separately).

**When to skip:**
- Pure doc / planning / `.gitignore` / CI-workflow-only changes.
- Config-default-flip changes that are also covered by deterministic unit tests (e.g. the engine-default flip — unit tests check the wiring; live tests would only confirm Camoufox launches, which is gated by the existing live nightly).
- Changes scoped entirely to tests/ that don't change source behavior.
