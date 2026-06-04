---
name: branch-and-pr-never-commit-to-main
description: "Every unit of work (phase, debug, fix, chore) goes on its own branch and reaches main via a PR — never commit/push directly to main"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 37d11507-849c-4d50-9019-f2f550e3d9fc
---

For MangarrGateway, **never commit or push directly to `main`** for any change with code, test, or contract impact. Every such unit of work — GSD phases, debug sessions, ad-hoc fixes, refactors, config/doc changes that affect runtime behavior — goes on its own branch and merges to `main` only through a reviewed PR.

**Exception — tracking-only bookkeeping commits go straight to `main`** (clarified 2026-05-31). These are commits whose entire diff is metadata bookkeeping with zero functional impact:
- `.planning/STATE.md` "Quick Tasks Completed" Status column updates (e.g. marking a row `Merged (PR #N)`)
- `.planning/STATE.md` "Last activity" line bumps
- Session/phase status moves to resolved/completed
- Equivalent metadata-only edits inside `.planning/`

For these, commit on `main` (or fast-forward / cherry-pick a one-off branch and push) — no PR. CodeRabbit + CI gate add no value to a row-status flip, and the prior pattern (chore branch → PR → merge → delete branch, e.g. PRs #49 and #51) was pure churn.

**Why the main rule:** The user wants `main`'s history to reflect reviewed, PR-gated changes (CodeRabbit + CI run on PRs). Phase 1 was pushed directly to `main` and had to be retroactively PR'd for review — that's what the main rule prevents. The exception is narrow: tracking metadata that records *that a PR happened*, not what the PR did.

**How to apply:**
- Phases: GSD is configured with `git.branching_strategy: phase` (config.json), so `/gsd-execute-phase` auto-branches `gsd/phase-{phase}-{slug}`. Quick tasks branch via `gsd/quick-{slug}`.
- Debug/fix/chore with real changes: create a descriptive branch first (`debug/{slug}`, `fix/{slug}`, `chore/{slug}`) before any edits, then open a PR to `main` with `gh pr create --base main`.
- Tracking-only STATE.md updates after a PR merges: `git checkout main && git pull --rebase && edit STATE.md && git commit && git push origin main`.
- Before pushing PR branches, run the full gate (see [[ci-gate-run-full-nox-before-push]]). Tracking-only STATE.md commits don't need the full gate — STATE.md is pure markdown.
- After review, address comments per [[pr-comment-reply-and-resolve-individually]] and track deferrals as issues per [[github-issues-single-source-of-truth]].
- If GitHub branch protection is enabled on `main`, even the tracking-only path will be rejected — check with the user before retrying.
- Verify CLAUDE.md "Branching & PR workflow" against this rule before quoting it; the codified version there may not yet reflect the 2026-05-31 exception (offer to update if the user wants it codified).
