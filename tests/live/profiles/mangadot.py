"""Mangadot LiveSmokeProfile (D-49 / D-50).

Mangadot is registered in quick task 260602-mcn, and D-50 requires every registered
source to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook
fails at collection time, exercised via ``REGISTERED_KEYS`` auto-discovery).

Cross-reference ``src/manga_gateway/sources/mangadot.py`` for the production source
metadata — this profile is the TEST-ONLY mirror; production data stays out of this
file (D-49 keeps profiles structurally separate from the Source class).

Anti-bot expectations
----------------------
* ``expected_caps_antibot = "cloudflare"`` — matches ``MangadotSource.antibot``.
  Mangadot RE-ENABLED its Cloudflare managed-challenge interstitial on 2026-06-09 (debug
  mangadot-live-smoke-403, #200) — the REVERSE of #127/#128 which had recorded it
  dropping the interstitial. Every ``/api`` endpoint now 403s the gateway's plain-httpx
  UA with the Cloudflare "Just a moment..." JS challenge (verified live through a fresh
  residential proxy IP → host-level gating, not proxy-IP reputation). It is a clean-JSON
  ``cloudflare`` source like kagane — plain JSON once cleared (NOT ``+encrypted``).
* ``needs_solver_warm = True`` — mangadot again needs a Patchright warm to capture the
  ``cf_clearance`` cookie before the search/download legs. The #196 fix scoped the
  per-test warm to ``get_clearance(source_key)``, so re-adding mangadot to the CF warm
  set warms ONLY mangadot — it does not re-contaminate comix/kagane.
* OPEN RISK (same as kagane #197/#198): whether the headless solver clears mangadot's
  CURRENT challenge in CI is unverified — it cannot be confirmed from a local host and
  must come from a fresh nightly ``workflow_dispatch``.

Release shape (D-08): Mangadot releases carry ``mangaId`` as the leading id
(``mangadot:{manga_id}:ch-{number}:{language}:{chapter_id}``); the smoke modules key
on ``id_field = "mangaId"``.

Default-query selection — provenance-verify requirement (RESEARCH ⚠️)
--------------------------------------------------------------------
RESEARCH.md CONFIRMED a chapter-id provenance oddity: some junk/aggregated manga
entries (e.g. manga 101) list ``chapters/list`` ids that resolve to OTHER manga
(manga 101's ``id:388872`` resolves to manga 5296). So the ``default_query`` MUST
resolve to a REAL manga whose ``chapters/list`` ids resolve back to the SAME manga
with a matching ``page_count``, and the download leg MUST pick a ``source:"user"``
chapter with present (non-404) image files (recon §4: ``scraper`` chapters can 404
their page files).

``default_query = "Murim Psychopath"`` (manga 20277) — a real title seen live in
latest-updates (RESEARCH §provenance). The live-smoke loop verifies the leading hit's
``chapters/list`` ids resolve back to the same manga with a matching ``page_count``
before relying on it for the download leg. Alternate: "Star-Embracing Swordmaster"
(manga 182).

LIVE-TUNE items (refine from the first deploy-host smoke)
---------------------------------------------------------
* **default_query stability + chapter-id provenance** — confirm the leading hit's
  chapter ids resolve to the SAME manga (matching page_count); swap the query if the
  catalog shifts or the leading hit is a junk/aggregated entry.
* **languages** — currently ``["en"]`` on the source; widen only if the live loop
  shows other languages present in the chapter array.
* **download_timeout_s** — 480.0 now covers only the per-page same-origin image
  fetch (no Cloudflare warm anymore); re-size against the real end-to-end wall-clock.
* **Referer on the image GET** — RESEARCH §image: same-origin, Referer likely
  unneeded. If a bare GET 403s live, add ``Referer: https://mangadot.net/`` in
  ``MangadotSource.fetch_image`` (the fetch_image docstring flags this).
* **fixture_drift_paths** — empty until the first live smoke pins the real
  search / chapters-list / images shapes (mirrors comix.py / mangaball.py).

Alt-title live smoke (#139)
---------------------------
``alt_title_query`` / ``alt_title_expected_substring`` populated (#139): a
2026-06-05 live recon confirmed mangadot's ``GET /api/search`` matches native/alt
titles server-side and ``alt_titles`` carries them — querying the Korean native
title of Solo Leveling (``나 혼자만 레벨업``) returns the "Solo Leveling" series
(``alt_titles`` includes that exact string). High-traffic, stable, long-running →
a deterministic alt-title smoke. The query matches ONLY via the alt title (the
English main title "Solo Leveling" does not contain the Korean string), so the
release[*] title containing "Solo Leveling" proves the alt-title-aware path
resolves end-to-end through the gateway.
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="mangadot",
    # Real title (manga 20277) seen live in latest-updates (RESEARCH §provenance).
    # The live loop verifies chapter-id -> images provenance resolves back to the
    # SAME manga with a matching page_count before relying on it for the download leg.
    default_query="Murim Psychopath",
    expected_caps_antibot="cloudflare",
    needs_solver_warm=True,
    # Cloudflare warm + per-page same-origin image fetch (mangadot re-gated CF, #200).
    download_timeout_s=480.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "mangadot", "id_field": "mangaId"},
    # No fixture-drift anchors captured yet — added after the first live smoke pins
    # the real search / chapters-list / images shapes (mirrors comix.py).
    fixture_drift_paths=[],
    perf_budget_s=None,
    # Alt-title live smoke (#139) — recon-verified 2026-06-05 (see module docstring).
    alt_title_query="나 혼자만 레벨업",
    alt_title_expected_substring="Solo Leveling",
)
