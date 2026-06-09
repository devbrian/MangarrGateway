"""Kagane LiveSmokeProfile (D-49 / D-50).

Kagane is registered in ``sources/__init__.py``; D-50 requires every registered
source to ship a ``LiveSmokeProfile`` in the same PR (else live collection fails).
Cross-reference ``src/manga_gateway/sources/kagane.py`` for the production metadata —
this profile is the TEST-ONLY mirror (D-49).

Anti-bot expectations
---------------------
* ``expected_caps_antibot = "cloudflare"`` — matches ``KaganeSource.antibot``. Only
  ``POST https://kagane.to/api/integrity`` (the download-path token mint) is actually
  Cloudflare-gated; ``yuzuki.kagane.to`` (data API) and ``kstatic.to`` (images) answer
  cold. The framework still injects ``cf_clearance`` + the bound UA on every call.
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
Komga-fork JSON-REST on ``yuzuki.kagane.to``: ``POST /api/v2/search/series`` (JSON body
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

Rate limit
----------
``rate_limit_per_minute = 30`` CONSERVATIVE start; ``max_concurrent_jobs`` left default.
BOTH are set from the Step-5.5 ``scripts/probe_rate_limits.py`` measurement (Task 4),
not the guess (precedent: clean httpx sources landed on 480 + 3 — trust the measure).

LIVE-TUNE items
--------------
* **rate_limit_per_minute / max_concurrent_jobs** — probe-tune (Step 5.5).
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
)
