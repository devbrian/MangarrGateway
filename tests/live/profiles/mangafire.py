"""MangaFire LiveSmokeProfile (D-49 / D-50 / D-16).

MangaFire is registered in ``sources/__init__.py``; D-50 requires every registered
source to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook
fails at collection time). This profile declares the live traits + the live-tune
items for the deploy-host smoke. Cross-reference
``src/manga_gateway/sources/mangafire.py`` for the production source metadata — this
profile is the TEST-ONLY mirror (D-49 keeps profiles structurally separate).

Anti-bot expectations (D-16)
----------------------------
* ``expected_caps_antibot = "cloudflare"`` — matches ``MangaFireSource.antibot``.
  Interior listing pages (chapter list / ``/filter``) answer cold over plain httpx,
  but the source declares ``cloudflare`` anyway (D-05) so the framework keeps a warm
  browser + clearance for the browser-DOM manifest path (the reader page mints the
  per-request vrf) and degrades gracefully on a datacenter host that trips a managed
  challenge.
* ``needs_solver_warm = True`` — the manifest (``fetch_via_browser``) path requires a
  warm Patchright context, so the harness must ``await solver.warm()`` before issuing
  requests. Search no longer touches the browser: the per-query vrf is now derived
  in-process via ``compute_vrf`` (pure Python, PR #313), not minted by real-keyboard
  typing in a warm browser context.

RESIDENTIAL-ONLY caveat + ONE-ATTRIBUTE escalation path (D-12)
--------------------------------------------------------------
The MangaFire recon (issue #192) was live-verified from a RESIDENTIAL IP; the listing
pages answered cold and headless Chromium passed Cloudflare. A DATACENTER-IP run MAY
surface a managed challenge on the listing pages too. The source already declares
``antibot = "cloudflare"`` so the clearance path is wired; if Chromium's fingerprint
is flagged on the deploy host, the escalation is the one-attribute engine flip
(``solver_engine = "android"``) — no networking/glue change, since the framework owns
the clearance path (CLAUDE.md: Patchright is the gateway's only desktop engine).

Release shape (D-08): MangaFire releases carry the ``{slug}.{id}`` manga token as the
leading guid segment (``mangafire:{mangaToken}:ch-{number}:{lang}``) and expose it as
``ids.mangafireMangaId``; the smoke modules key on ``id_field = "mangafireMangaId"``.

Default-query selection
-----------------------
``default_query = "blue lock"`` — a high-traffic, long-running title pinned from the
issue #192 recon (``/manga/blue-lockk.kw9j9``, live-verified 2026-06-09) with a
deterministic leading hit so ``release[0]`` is well-defined. The first live smoke
confirms the catalog still returns it; swap if the catalog shifts.

Rate limit (PROBE-MEASURED 2026-06-10 — D-14, Plan 03)
------------------------------------------------------
``rate_limit_per_minute = 60`` + ``max_concurrent_jobs = 3`` are now MEASURED, not
guessed (``scripts/probe_rate_limits.py``, residential proxies). The mangafire.to
text/AJAX host enforces a real HTTP-429 ceiling — clean at 120/min, 429 onset at
300/min — so 60 is the conservative ~50%-of-clean-ceiling value governing the limited
``get_json`` chapter-list path. The image CDN had NO limit (clean to 960/min at
concurrency 8); images ride the unlimited ``get_bytes`` path bounded by
``max_concurrent_jobs = 3``. The browser-manifest path was unmeasurable by the httpx
probe — the live nightly below re-confirms it end-to-end. See the source's
reconciliation block for the full per-endpoint breakdown.

LIVE-TUNE items (refine from the first deploy-host smoke)
---------------------------------------------------------
* **default_query stability** — confirm "blue lock" still returns a deterministic
  leading hit with an available short chapter; swap if the catalog shifts.
* **typed-search vrf capture** (D-13) — the search vrf is minted ONLY by real keyboard
  typing into ``.search-inner input[name=keyword]``; confirm the captured vrf works on
  ``/filter`` live and that the LRU cache reuses it within a query.
* **manifest performance-capture** (D-08) — the reader auto-fires
  ``/ajax/read/chapter/{itemId}?vrf=…``; confirm the ``performance``-entry poll catches
  it within the ≤60×500ms budget on the deploy host.
* **image descramble** (D-12) — confirm ``offset>0`` pages descramble correctly and
  ``offset==0`` pages pass through unchanged on real CDN images.
* **rate_limit_per_minute / max_concurrent_jobs** — set to 60 / 3 from the 2026-06-10
  D-14 probe (real 429 ceiling on the text host; image CDN unlimited). Re-confirm the
  browser-manifest path (probe-unmeasurable) against the real end-to-end download.
* **download_timeout_s** — 480.0 (Cloudflare warm + browser-DOM manifest + per-page
  descramble, matching the comix CF-warm budget). Re-size against the real end-to-end
  download wall-clock.
* **fixture_drift_paths** — empty until the first live smoke pins the real chapter-list
  / card / manifest shapes; add anchors then (mirrors comix.py).
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="mangafire",
    default_query="blue lock",
    expected_caps_antibot="cloudflare",
    needs_solver_warm=True,
    # CF warm + browser-DOM manifest capture + per-page descramble (cf comix 480.0).
    download_timeout_s=480.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "mangafire", "id_field": "mangafireMangaId"},
    # External tracker links (Phase 13, D-08 / R7). REPINNED to the live
    # ``default_query="blue lock"`` title (USER DECISION, 2026-06-19): MangaFire's
    # ``<script id="syncData">`` JSON exposes only AniList + MyAnimeList
    # (``manga_id`` is its OWN internal id and is dropped, R3). Captured live
    # 2026-06-19 via a headed Patchright CF clear — the Blue Lock release returns
    # these two keys consistently. (The prior Berserk pins were for a DIFFERENT
    # title than default_query and could never pass — dropped.)
    expected_external_links={
        "anilist": "106130",
        "myAnimeList": "114745",
    },
    fixture_drift_paths=[],
    perf_budget_s=None,
)
