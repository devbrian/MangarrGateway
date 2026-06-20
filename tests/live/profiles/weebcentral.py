"""Weeb Central LiveSmokeProfile (D-49 / D-50).

Weeb Central is registered in ``sources/__init__.py``; D-50 requires every
registered source to ship a ``LiveSmokeProfile`` in the same PR (else the
live-collection hook fails at collection time). This profile declares the live
traits + the live-tune items for the deploy-host smoke. Cross-reference
``src/manga_gateway/sources/weebcentral.py`` for the production source metadata —
this profile is the TEST-ONLY mirror (D-49 keeps profiles structurally separate).

Anti-bot expectations
----------------------
* ``expected_caps_antibot = "none"`` — matches ``WeebCentralSource.antibot``. The
  search (``/search/data``), chapter-list, recent, and manifest endpoints answer cold
  over plain httpx with any User-Agent; no Cloudflare challenge, cookie, or CSRF token
  is engaged (live recon 2026-06-08, residential IP — every GET returned 200).
* ``needs_solver_warm = False`` — Weeb Central never engages the Patchright solver, so
  the harness must not ``await solver.warm()``.

RESIDENTIAL-ONLY caveat + ONE-ATTRIBUTE escalation path (D-12)
--------------------------------------------------------------
The ``antibot="none"`` classification was established from a RESIDENTIAL IP. A
DATACENTER-IP run MAY surface a managed Cloudflare challenge (weebcentral.com fronts
Cloudflare passively); that is the unproven datacenter-IP gap, NOT a regression.
Escalation is a one-attribute flip on the production source
(``WeebCentralSource.antibot = "cloudflare"`` + ``cloudflare_challenge_url``), then
flip this profile's ``expected_caps_antibot`` → ``"cloudflare"`` and
``needs_solver_warm`` → ``True`` to match. No networking/glue change.

Release shape (D-08): Weeb Central releases carry the seriesId as the leading guid
segment (``weebcentral:{seriesId}:ch-{number}:{chapterId}``); the smoke modules key
on ``id_field = "series_id"``.

Default-query selection
-----------------------
``default_query = "one piece"`` — a high-traffic, long-running title that returns a
deterministic leading hit (``id=01J76XY7E9FNDZ1DBBM6PBJPFK``, ``title="One Piece"``,
recon 2026-06-08). Selection mirrors mangadex.py's discipline: stable/long-running, a
deterministic leading hit so ``release[0]`` is well-defined, and a short newest
chapter (≈13 pages) so the download leg finishes inside ``download_timeout_s``.

Rate limit (probe-measured 2026-06-09)
--------------------------------------
``scripts/probe_rate_limits.py`` measured a REAL server-side rate limit on the HTML
endpoints — UNLIKE the mangaball/mangadot/atsumaru precedent (all "no hard limit" →
480): the manifest endpoint 429s at ~120/min PER IP (even at concurrency 1, ~36 %
blocked), and search 429s at concurrency >=8 / 120/min (clean parallelism = 4); the
image CDN is unlimited (clean to concurrency 32, 120/min FLOOR). The source is set to
``rate_limit_per_minute = 60`` (the conservative ~50 %-of-onset advisory) +
``max_concurrent_jobs = 3``. CRITICAL: Weeb Central is an ALL-HTML source (every
search/chapter-list/recent/manifest call uses the UNLIMITED ``get_bytes`` byte path),
so ``rate_limit_per_minute`` is advisory for /caps display and does NOT actually gate
the HTML calls — real load is bounded by ``max_concurrent_jobs`` + the per-job image
semaphore. See the source module docstring's bulk-load caveat + recommended
follow-up (route HTML GETs through a limited primitive).

LIVE-TUNE items (refine from the first deploy-host smoke)
---------------------------------------------------------
* **default_query stability** — confirm "one piece" still returns a deterministic
  leading hit with an available short chapter; swap if the catalog shifts.
* **search title selector** (RECON 7.2) — the production source reads the title from
  the cover ``<img alt="<Title> cover">`` (the " cover" suffix stripped); confirmed
  live 2026-06-08. If the markup shifts, the truncate ``<div>`` is the fallback.
* **special chapter-number parsing** (RECON 7.3) — ``Chapter N`` / ``Chapter N.M``
  parse to Decimal (``10.5`` preserved); non-numeric specials (volume/oneshot) yield
  ``None`` → a ``ch-?`` guid. Confirm the catalog has no surprising labels.
* **large-chapter manifest completeness** (RECON 7.4) —
  ``reading_style=long_strip`` returns ALL pages in one call (verified 13/13 for One
  Piece 1184); re-assert on a ~100-page manhwa strip.
* **rate_limit_per_minute / max_concurrent_jobs** — 60 / 3 from the 2026-06-09 probe
  (60 is advisory only — the HTML byte path is unlimited; see the Rate limit section).
  Re-probe the manifest clean ceiling on a faster proxy pool if a precise number is
  needed (the low-rate manifest sweep was cut short by slow proxies).
* **download_timeout_s** — 180.0 (no Cloudflare warm + plaintext same-origin-class
  CDN ``.png`` fetch, matching MangaDex's plain-CDN budget). Re-size against the real
  end-to-end download wall-clock.
* **fixture_drift_paths** — empty until the first live smoke pins the real
  search / chapter-list / manifest shapes; add anchors then (mirrors comix.py).
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="weebcentral",
    default_query="one piece",
    expected_caps_antibot="none",
    needs_solver_warm=False,
    download_timeout_s=180.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "weebcentral", "id_field": "series_id"},
    # External tracker links (Phase 13, D-08 / R7). RESEARCH-frozen canary IDs
    # for *Solo Leveling*: WeebCentral's Track section exposes only AniList +
    # MangaUpdates as full URLs (normalized to bare ids); ``Official Source`` is
    # dropped (no canonical map entry, R3). NOTE: ``default_query`` above is
    # "one piece" (download-leg canary), NOT Solo Leveling — Task 3 (nightly)
    # must reconcile: repoint the links-canary or confirm/repin these IDs
    # against whatever ``default_query`` returns live.
    expected_external_links={
        "anilist": "105398",
        "mangaUpdates": "6z1uqw7",
    },
    fixture_drift_paths=[],
    perf_budget_s=None,
)
