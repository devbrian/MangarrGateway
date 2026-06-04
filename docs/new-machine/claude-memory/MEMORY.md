# Memory Index

- [GitHub issues are single source of truth](github-issues-single-source-of-truth.md) — track all deferred/outstanding items as GH issues, not just GSD planning docs
- [Reply to PR comments individually and resolve](pr-comment-reply-and-resolve-individually.md) — never bucket multiple review-comment responses into one top-level comment
- [Run the full nox gate before pushing](ci-gate-run-full-nox-before-push.md) — CI is `uv run nox -s gate` (ruff check + format --check + mypy + pytest) over the whole repo; scoped checks miss failures
- [Branch + PR, never commit to main](branch-and-pr-never-commit-to-main.md) — every phase/debug/fix/chore goes on its own branch and reaches main via a reviewed PR
- [Prefer merge commits, no squash](prefer-merge-commits-no-squash.md) — merge PRs with `--merge`, never `--squash`, to preserve commit + merge history
- [Cloudflare engine default = Patchright/Chromium](cloudflare-engine-default-patchright.md) — REVERSED from camoufox (2026-06-01) so Comix /search runs parallel (concurrency 3); camoufox is now the opt-in fallback for fingerprint-flagged hosts, pinned to concurrency 1
- [Run live tests locally before push](run-live-tests-locally-before-push.md) — when a change touches sources/, antibot, solver_lifecycle, fanout, or the warm-browser path, run live locally before pushing — the gate excludes -m live so it's silent on these regressions
- [Live conftest solver-kwargs mirror drifts](live-conftest-solver-kwargs-mirror-drift.md) — tests/live/conftest.py duplicates app.py's CloudflareSolver kwargs by hand; add new solver knobs to BOTH or live tests silently bypass them
- [Deployed gateway docker topology](deployed-gateway-docker-topology.md) — runs on REMOTE host via docker context local-remote (192.168.0.246:9191, not localhost); code baked in image (rebuild to deploy); redeploy with `docker compose -f docker-compose.yml up --build -d gateway`
- [CI gate normally takes ~75-82 min](ci-gate-normal-duration-75min.md) — slow ≠ stuck; ~5× the local 16-min gate (serial pytest + schemathesis + Chromium download + slow 2-core runner); failed runs that die <2min are collection crashes, not slowness
