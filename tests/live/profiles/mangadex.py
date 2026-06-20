"""MangaDex LiveSmokeProfile (D-49).

Cross-reference: ``src/manga_gateway/sources/mangadex.py:39-48`` for the
production source's metadata — ``antibot = "none"`` and the published
rate limit. This profile is the TEST-ONLY mirror: production data stays
out of this file (D-49 keeps profiles structurally separate from the
production Source class).

Default-query selection — picked by the executor during Phase 5 Plan 03
and pending end-of-phase human verification (RESEARCH Open Question #5).
Selection criteria (from 05-03-PLAN.md task 1 checkpoint):

* completed status (won't disappear / mid-release)
* short (≤20 pages) so the live download surface finishes inside
  ``download_timeout_s = 180.0``
* ≥6 months old (stable scanlation, no licensing takedown surprises)
* unambiguous title (a single search hit so the test's release[0] is
  deterministic)

Candidate: **Chainsaw Man - The Hayakawa Family (Doujinshi)**
* MangaDex manga id: ``268f5da0-d158-4c95-bc2b-b4d962c2f325``
* MangaDex chapter id: ``0e85d6b6-25e0-4526-941f-17efff8c92f0``
* author: Tatsuki Fujimoto (2023 New Year doujinshi)
* status: completed | year: 2023 | language: en | published: 2024-03-26
* pages: 12 (well under the ≤20 ceiling)
* search probe (2026-05-30): ``GET https://api.mangadex.org/manga
  ?title=Hayakawa Family`` returned ONE hit (id starts with ``268f5da0``)
  matching the target — narrow, stable, deterministic.

This sits inside Phase 5's end-of-phase ``human-verify`` harvest (per
project ``human_verify_mode = end-of-phase``). If reviewed and rejected,
replace ``default_query`` and the comment block above with the approved
alternate; the rest of the profile is unchanged.
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="mangadex",
    default_query="Hayakawa Family",
    expected_caps_antibot="none",
    needs_solver_warm=False,
    # 180s is slack for a 12-page oneshot through MangaDex at-home — the
    # framework is per-host rate-limited but image fetch is parallel-fanned
    # (D-31 image_fetch_concurrency=6). Bumped from research suggestion
    # 120s after observing real Phase-3 e2e timings.
    download_timeout_s=180.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "mangadex"},
    # External tracker links (Phase 13, D-08 / R7). DECOUPLED canary (USER
    # DECISION, 2026-06-19): ``default_query="Hayakawa Family"`` (the doujinshi
    # download-leg canary) legitimately exposes NO ``attributes.links`` live
    # (a 2026-06-19 capture observed ``externalLinks={}``), so it cannot anchor
    # the external-links assertion. Instead this profile points the links smoke at
    # a dedicated ``expected_external_links_query="Berserk"`` while leaving
    # ``default_query`` (download smoke) UNCHANGED. The IDs below were captured
    # LIVE from the gateway's own ``POST /api/v1/search`` for "Berserk" on
    # 2026-06-19 (search source=mangadex query='Berserk' results=421; all 50
    # returned releases are the Berserk series and carry identical canonical
    # ``externalLinks``). Observed live union also included animePlanet="berserk"
    # and bookwalker="16664"; we pin the four canonical TRACKER IDs (the same key
    # convention the other profiles use) — every value below was read from a real
    # live response, never invented. Bare IDs only, no URLs (T-13-03).
    expected_external_links={
        "anilist": "30002",
        "myAnimeList": "2",
        "mangaUpdates": "njeqwry",
        "kitsu": "8",
    },
    expected_external_links_query="Berserk",
    fixture_drift_paths=[],
    perf_budget_s=None,
    # Alt-title live smoke (#139): INTENTIONALLY DISABLED for MangaDex (left None
    # like mangadot/mangaball). Unlike the title-only sources — which return a
    # broad keyword candidate list that the client-side prune narrows — MangaDex's
    # ``GET /manga?title=`` is a real server-side search engine that ALREADY does
    # alt-title matching itself (a native-script query returns romanized/alt
    # matches). A 2026-06-05 live probe of the Korean Solo Leveling native title
    # (나 혼자만 레벨업) returned only spin-offs (Ragnarok/Arise/Book Version), not
    # the canonical series, so the chapter-feed enumeration produced 0 English
    # releases — i.e. the result set is governed by MangaDex's own relevance
    # ranking + spin-off catalog, not by our prune, which makes a deterministic
    # native-query live assertion infeasible. The mangadex alt-title PRUNE WIRING
    # is correct and conservative (byte-identical fallback when no unambiguous
    # match) and is covered by the offline integration test in
    # tests/test_candidate_relevance.py (alt→1, main→1, ambiguous→fan-out). The
    # main-title live smoke (default_query above) exercises the live search path.
    alt_title_query=None,
    alt_title_expected_substring=None,
)
