---
name: prefer-merge-commits-no-squash
description: Merge PRs with a merge commit — do NOT squash (preserve individual commits + merge structure in history)
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 37d11507-849c-4d50-9019-f2f550e3d9fc
---

When merging PRs for MangarrGateway, use a **merge commit** — do NOT squash. Preserve the individual commits and the merge structure in `main`'s history. Only squash (or rebase) if the user explicitly asks for it on a specific PR.

**Why:** The user values real git history for traceability (they even opened a dev→main PR purely for history tracking). Squashing collapses commits and loses the merge structure. I previously advised against squash for one PR, then inconsistently used `gh pr merge --squash` on the two Dependabot bumps (#5, #6) — the user called this out.

**How to apply:** Merge with `gh pr merge <n> --merge` (merge commit), never `--squash`. Note this is a *merge-time* choice, not a PR-creation choice. Relates to [[branch-and-pr-never-commit-to-main]].
