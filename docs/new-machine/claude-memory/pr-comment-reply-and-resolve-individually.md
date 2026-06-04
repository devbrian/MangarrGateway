---
name: pr-comment-reply-and-resolve-individually
description: Reply to each PR review comment individually and resolve the thread; never bucket replies into one top-level comment
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 37d11507-849c-4d50-9019-f2f550e3d9fc
---

When addressing PR review comments (e.g. from CodeRabbit or human reviewers): reply to **each comment individually** on its own thread, then **resolve that thread** after addressing it. Do NOT post a single top-level PR comment that buckets multiple responses together.

**Why:** Reviewers (and automated reviewers) track resolution per-thread. A single bucketed top-level response leaves every inline thread unresolved/open, so it reads as "not addressed" and breaks the reviewer's per-comment tracking. Individual reply + resolve gives a clean, auditable thread-by-thread record.

**How to apply:** For each inline review comment, post a reply via `gh api repos/{owner}/{repo}/pulls/{pr}/comments/{comment_id}/replies` (REST) stating the fix commit OR the reason for not changing (with the tracking issue # if deferred — see [[github-issues-single-source-of-truth]]). Then resolve the thread via GraphQL `resolveReviewThread(input:{threadId})`. Map threads with the `reviewThreads` GraphQL query (gives both the thread `id` for resolving and `comments[0].databaseId` for the REST reply). Resolve every thread, whether fixed or declined.
