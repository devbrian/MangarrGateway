---
name: ci-gate-run-full-nox-before-push
description: "CI gate is `uv run nox -s gate` over the WHOLE repo — run it before pushing, not scoped ruff/mypy"
metadata: 
  node_type: memory
  type: project
  originSessionId: 37d11507-849c-4d50-9019-f2f550e3d9fc
---

The CI gate for MangarrGateway is `uv run nox -s gate`, which runs FOUR steps over the **whole repo** (not just `src/`):
1. `ruff check .`
2. `ruff format --check .`  ← formatting drift fails CI
3. `mypy src`
4. `pytest -q`

**Why this matters:** Scoped commands like `ruff check src/manga_gateway` and `mypy src/manga_gateway` (what GSD executors and ad-hoc verification tend to run) are NOT sufficient — they miss (a) lint errors in `tests/` (e.g. E501) and (b) `ruff format --check` entirely. Pushing after only scoped checks caused CI to fail on PR #4 (E501 in tests + format drift in 4 files that prior Phase 2 commits never caught).

**How to apply:** Before pushing or opening/repushing a PR, run `uv run nox -s gate` locally and get it fully green. If you only have time for ruff, run `ruff check .` AND `ruff format .` over the whole repo, not a scoped path. Outstanding CI failures still get tracked per [[github-issues-single-source-of-truth]].
