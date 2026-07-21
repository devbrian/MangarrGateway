"""Kagane LiveSmokeProfile (D-49 / D-50).

Kagane is registered in ``sources/__init__.py``; D-50 requires every registered
source to ship a ``LiveSmokeProfile`` in the same PR (else live collection fails).
Cross-reference ``src/manga_gateway/sources/kagane.py`` for the production metadata —
this profile is the TEST-ONLY mirror (D-49).

Anti-bot expectations
---------------------
* ``expected_caps_antibot = "cloudflare"`` — matches ``KaganeSource.antibot``. Only
  the whole ``kagane.to`` origin — ``POST /api/integrity`` AND the same-origin
  ``/api/v2/...`` data API (#360) — is Cloudflare-gated; only ``kstatic.to`` (images)
  answers cold. The framework injects ``cf_clearance`` + the bound UA on every call.
* ``needs_solver_warm = True`` — the source engages the Patchright solver so the
  framework can earn ``cf_clearance`` for the ``/api/integrity`` POST.

HEADED-solver open question (deploy-env item)
---------------------------------------------
kagane.to uses the same Cloudflare MANAGED challenge as comix (headless Chromium is
fingerprint-blocked). Recon earned ``cf_clearance`` HEADED; the deploy host runs the
solver with ``GATEWAY_CLOUDFLARE_HEADLESS=false``. A "captured clearance (N cookies)"
log line followed by ``upstream 403`` on ``/api/integrity`` is the stale-cookie false
positive — the solver could not earn a fresh cookie headless. (Headless not separately
proven; env knob, not code.)

Topology (recon 2026-06-09)
---------------------------
Komga-fork JSON-REST same-origin on ``kagane.to`` (#360).
``POST /api/v2/search/series`` (JSON body
``{"title": q, "content_rating": ["Safe","Suggestive"]}``) is TITLE-ONLY, so ``search``
fans out ``GET /api/v2/series/{id}`` (complete ``series_books``) per candidate. The
download path is a 3-call token flow (integrity → book-token → page URL), all clean
httpx — NO comix browser-DOM read. Pages are plaintext JPEG (no decrypt) from the
response-provided ``cache_url`` (``kstatic.to``).

Release shape (D-08): guid ``kagane:{series_id}:ch-{chapter_no}:{book_id}``; the
``ResolutionRecord.chapter_id`` is the bare ``book_id``.

Default-query selection
-----------------------
``default_query = "solo leveling"`` — a high-traffic, long-running title with a
deterministic leading relevance hit, so ``release[0]`` is well-defined for the
download leg.

Rate limit (probe-measured 2026-06-09)
--------------------------------------
``scripts/probe_rate_limits.py --source kagane --no-proxy`` (local residential IP)
found the BINDING limit is the **manifest token path** — ``POST /api/integrity`` /
``POST /api/v2/books/{id}`` HARD-block above ~30/min with ``Retry-After: 30`` (30 ok
then 30 blocked at 60/min, c=1). The ``search`` endpoint is slow (~3-4s/call) but never
429s; ``image`` bytes are clean to 240/min (the probe budget cap) and ``get_bytes`` is
limiter-exempt anyway. Unlike the no-limit clean-httpx sources (atsumaru 480+3), Kagane
has a genuine tight token bucket, so the source is set to
``rate_limit_per_minute = 24`` (~80% of the 30/min hard ceiling — the per-source limiter
gates BOTH token POSTs) + ``max_concurrent_jobs = 2``. NOT the 480+3 precedent — trust
the measurement.

LIVE-TUNE items
--------------
* **rate_limit_per_minute / max_concurrent_jobs** — 24 / 2 (probe-measured; the
  manifest token path is the bottleneck). Re-probe with a residential proxy + larger
  ``--max-requests`` if the token limit changes or to map parallelism past c=1.
* **download_timeout_s** — 480.0 (CF warm + token flow + plaintext JPEG fetch, mirrors
  comix's CF-warm budget). Re-size against the real end-to-end download wall-clock.
* **Referer on the CDN image GET** — the ``?token=`` query is the gate; a bare GET is
  expected to return 200 (confirm; add a ``Referer`` only if the smoke shows a hotlink
  block).
* **cache_url host drift** — the SSRF allowlist pins ``kstatic.to`` + ``*.kagane.to``;
  widen if ``cache_url`` moves.
* **fixture_drift_paths** — empty until the first live smoke pins the real shapes.
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="kagane",
    default_query="solo leveling",
    expected_caps_antibot="cloudflare",
    needs_solver_warm=True,
    download_timeout_s=480.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "kagane", "id_field": "series_id"},
    fixture_drift_paths=[],
    perf_budget_s=None,
    # No ci_skip_reason (#215 Model A): kagane is NO LONGER unconditionally
    # CI-skipped. Its strict Cloudflare Turnstile is still unclearable by desktop
    # browser automation from Linux and is cleared via the redroid + android-solver
    # sidecar (Android WebView) — but the nightly now reaches that home android-solver
    # over Tailscale. So this source RUNS when the tailnet-reachable home solver
    # answers /healthz and is SKIPPED (not failed) by the conftest reachability gate
    # when the solver is unreachable. `expected_caps_antibot="cloudflare"` is
    # unchanged (the Android engine is an internal solver detail, not a /caps class).
)
