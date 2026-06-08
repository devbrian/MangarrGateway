---
name: cloudflare-engine-default-patchright
description: Cloudflare engine default REVERSED to Patchright/Chromium (was Camoufox) so Comix /search runs in parallel; Camoufox is now the opt-in fallback for fingerprint-flagged hosts.
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 383e136c-4888-43c2-9b73-a33b7644f9f0
---

For Mangarr Gateway, the Cloudflare engine default is now **Patchright/Chromium** with `cloudflare_fetch_concurrency=3` (decided 2026-06-01). This REVERSES the earlier "Camoufox everywhere" preference (old [[dev-uses-camoufox-not-patchright]], issue #40). Camoufox is now the opt-in fallback via `GATEWAY_CLOUDFLARE_ENGINE=camoufox` (and it MUST pin `cloudflare_fetch_concurrency=1`).

**Why:** The `comix-parallel-engine-probe` investigation proved the concurrent-CF-navigation stall is engine-specific: only Chromium/Patchright can run N>1 concurrent Cloudflare navigations on one warm context (camoufox/Firefox stalls at goto-commit). Comix `/search` fans out in parallel only on Chromium — so Chromium is the default. Trade-off accepted: the old #40 rationale (Camoufox-only failures like #54 surface in dev) is given up for parallelism.

**DATACENTER RESOLVED (the key finding):** The user pushed back on #35's "Chromium can't do datacenter." A datacenter probe (GitHub Actions) proved the block is **HEADLESS-specific**, not the IP or Chrome channel: headless Chromium is fingerprinted by CF at the binary level and times out, but **HEADED Chromium under Xvfb clears CF on the datacenter runner** (probe: headless=46s FAIL, headed-xvfb=2-4s PASS). The full nightly live suite then went **green on the datacenter runner under headed+Xvfb** (15 passed). So a residential proxy (#65) is NOT required for datacenter — headed+Xvfb is the fix.

**How to apply:**
- `Settings.cloudflare_engine` default = `"patchright"`, `cloudflare_fetch_concurrency` default = `3` (src/manga_gateway/config.py). A `_reject_camoufox_parallel` model validator fails fast on `camoufox + concurrency>1` (closed #64).
- `cloudflare_headless` default stays True (residential dev/prod = headless works, windowless). On a DATACENTER host (cloud/CI) set `GATEWAY_CLOUDFLARE_HEADLESS=false`: the solver auto-starts an Xvfb virtual display via `pyvirtualdisplay` when headed on display-less Linux (host needs the `xvfb` package). nightly-live-smoke.yml runs patchright + headed + xvfb.
  - **SUPERSEDED 2026-06-07 (debug `comix-recent-403`):** headless no longer works against Comix on a **residential** IP either — Comix's CF now blocks headless Chromium everywhere. The **Docker image default is now `GATEWAY_CLOUDFLARE_HEADLESS=false`** (headed). The bare-app `Settings` default stays True only for local dev on a real display; the image overrides it. Headed is the safe default on every host.
- Camoufox is still the opt-in fallback (engine=camoufox AND fetch_concurrency=1) for any host where headed Chromium can't be used.
- Residential proxy (#65) is now an ALTERNATIVE datacenter mitigation, not a requirement.
- SHIPPED + MERGED: PR #63 (parallel fan-out) and PR #66 (chromium default + concurrency 3 + guard + headed-Xvfb), both on `main` 2026-06-01. Open follow-ups: #65 (proxy transport, optional), #70 (failed-job diagnosability: engine swallows the cause + release identity). Related: [[github-issues-single-source-of-truth]], [[run-live-tests-locally-before-push]].
