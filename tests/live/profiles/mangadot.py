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
RESEARCH.md observed ``chapters/list`` ids that resolve to OTHER manga / 404 and
mis-attributed it to junk/aggregated entries. Debug mangadot-resolve-404 (2026-06-11)
found the real cause: mangadot has TWO manifest namespaces with OVERLAPPING ids —
``/api/uploads/{id}/images`` (``source=="user"``) and ``/api/chapters/{id}/images``
(scraped). ``MangadotSource.fetch_manifest`` now ROUTES by the row's ``source``, so a
user-source chapter resolves on ``/api/uploads`` (the reader's own choice) instead of
404-ing or silently returning a different manga off ``/api/chapters``. The
``default_query`` should still resolve to a REAL manga whose ``chapters/list`` ids
resolve back to the SAME manga with a matching ``page_count`` — that invariant now
holds BECAUSE of the source-routing, and is the live guard against a routing
regression (a wrong-namespace fetch would resurface as a page_count/manga-id mismatch).

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
    # External tracker links (Phase 13, D-08 / R7). DECOUPLED canary (USER
    # DECISION, 2026-06-19): mangadot's ``default_query="Murim Psychopath"``
    # (download-leg canary) returns all-empty ``externalLinks`` live (a nightly
    # 27858894114 capture saw 50 releases, all ``{}`` — that title legitimately
    # carries no tracker links), so the links smoke uses a dedicated
    # ``expected_external_links_query="Solo Leveling"`` while ``default_query``
    # (download smoke) stays UNCHANGED. The IDs below are RESEARCH-frozen for
    # *Solo Leveling* (manga 118): anilist=105398, myAnimeList=121496,
    # mangaUpdates=6z1uqw7, mangaBaka=3397, kitsu=54114 (canonical camelCase keys
    # exactly as the normalizer emits). ``mangaDex`` is OMITTED — null/dropped for
    # Solo Leveling (RESEARCH Open Question 3). These CANNOT be captured locally
    # (the android-solver sidecar that clears mangadot's Cloudflare Turnstile is
    # unreachable from this host) — they are RESEARCH-frozen and will be CONFIRMED
    # by the re-run nightly (the orchestrator owns nightly dispatch).
    expected_external_links={
        "anilist": "105398",
        "myAnimeList": "121496",
        "mangaUpdates": "6z1uqw7",
        "mangaBaka": "3397",
        "kitsu": "54114",
    },
    expected_external_links_query="Solo Leveling",
    # No fixture-drift anchors captured yet — added after the first live smoke pins
    # the real search / chapters-list / images shapes (mirrors comix.py).
    fixture_drift_paths=[],
    perf_budget_s=None,
    # Alt-title live smoke (#139) — recon-verified 2026-06-05 (see module docstring).
    alt_title_query="나 혼자만 레벨업",
    alt_title_expected_substring="Solo Leveling",
    # No ci_skip_reason (#215 Model A): mangadot is NO LONGER unconditionally
    # CI-skipped. Its strict Cloudflare Turnstile is still unclearable by desktop
    # browser automation from Linux and is cleared via the redroid + android-solver
    # sidecar (Android WebView) — but the nightly now reaches that home android-solver
    # over Tailscale. So this source RUNS when the tailnet-reachable home solver
    # answers /healthz and is SKIPPED (not failed) by the conftest reachability gate
    # when the solver is unreachable. `expected_caps_antibot="cloudflare"` is
    # unchanged (the Android engine is an internal solver detail, not a /caps class).
)
