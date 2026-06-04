---
name: github-issues-single-source-of-truth
description: "All issues and deferred items must be tracked in GitHub issues, not just GSD planning docs"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 37d11507-849c-4d50-9019-f2f550e3d9fc
---

GitHub issues are the **single source of truth** for outstanding/deferred items. Any bug, code-review finding, deferred item, or follow-up that is not being fixed immediately MUST be tracked as a GitHub issue — not only recorded in GSD planning docs (REVIEW.md, VERIFICATION.md, deferred-items.md, HUMAN-UAT.md, etc.).

**Why:** GSD planning docs are project-internal and easy to lose track of across phases; the user treats the GitHub issue tracker as the canonical, reviewable list of what's still open. Items buried only in `.planning/` are effectively invisible.

**How to apply:** When something is deferred or declined (code review findings, advisory warnings, future-phase work, design wrinkles), open a GitHub issue (`gh issue create`) capturing it. Still record it in the relevant planning doc for traceability, but the GH issue is what makes it "tracked." Reference the issue number in planning docs and in PR/comment replies. See [[pr-comment-reply-and-resolve-individually]].
