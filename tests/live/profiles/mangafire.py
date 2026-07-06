"""MangaFire LiveSmokeProfile (D-49 / D-50 / D-16).

MangaFire is registered in ``sources/__init__.py``; D-50 requires every registered
source to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook
fails at collection time). This profile declares the live traits for the deploy-host
smoke. Cross-reference ``src/manga_gateway/sources/mangafire.py`` for the production
source metadata — this profile is the TEST-ONLY mirror (D-49 keeps profiles
structurally separate).

New JSON API (260706-hgu)
-------------------------
MangaFire rewrote its frontend to a React SPA backed by a plain, unsigned ``GET /api/*``
JSON REST API. Search (``/api/titles``), the chapter list
(``/api/titles/{hid}/chapters``), and the manifest (``/api/chapters/{id}``) all answer
cold over plain httpx — no vrf, no browser, no descramble. The image CDN
(``mfcdnN.xyz``) is unchanged and still IP-bans datacenter egress, so the download smoke
exercises the proxy-pool image fetch.

Anti-bot expectations (D-16)
----------------------------
* ``expected_caps_antibot = "cloudflare"`` — matches ``MangaFireSource.antibot``. The
  JSON API answers cold, but the source declares ``cloudflare`` (D-05) so the framework
  keeps a lazily-solved clearance and degrades gracefully on a datacenter host that
  trips a managed challenge.
* ``needs_solver_warm = True`` — kept as a Cloudflare CLEARANCE FALLBACK only; NO path
  drives the browser. The harness still ``await solver.warm()``s so a datacenter host
  that trips a managed challenge degrades gracefully; once the nightly confirms the cold
  JSON path holds on the deploy host this can drop to ``False``.

Release shape: MangaFire releases carry the title ``hid`` as the leading guid segment
(``mangafire:{hid}:ch-{number}:{lang}:{chapterId}``) and expose it as
``ids.mangafireHid`` (plus the numeric ``ids.mangafireTitleId``); the smoke modules key
on ``id_field = "mangafireHid"``.

Default-query selection
-----------------------
``default_query = "blue lock"`` — a high-traffic, long-running title with a
deterministic leading hit so ``release[0]`` is well-defined. The first live smoke
confirms the catalog still returns it; swap if the catalog shifts.

Rate limit (PROBE-MEASURED 2026-06-10 — D-14)
---------------------------------------------
The mangafire.to API host enforces a real HTTP-429 ceiling (clean at 120/min, 429 onset
at 300/min), so ``rate_limit_per_minute = 100`` governs the limited ``get_json``
search/chapter-list path. The image CDN had NO limit (clean to 960/min at concurrency
8); images ride the unlimited ``get_bytes`` path bounded by ``max_concurrent_jobs = 3``.

LIVE-TUNE items (refine from the first deploy-host smoke)
---------------------------------------------------------
* **default_query stability** — confirm "blue lock" still returns a deterministic
  leading hit with an available short chapter; swap if the catalog shifts.
* **chapter-list completeness** — confirm the ``meta.lastPage`` pagination returns the
  COMPLETE list for a long series (limit=200 cap).
* **proxy image fetch** — confirm the ``mfcdnN`` zone-retry + residential proxy returns
  bytes for real CDN images from the deploy host.
* **rate_limit_per_minute / max_concurrent_jobs** — set to 100 / 3 from the 2026-06-10
  D-14 probe; re-confirm against the real end-to-end download.
* **download_timeout_s** — 480.0 (Cloudflare warm fallback + proxy image fetch);
  re-size against the real end-to-end download wall-clock.
* **fixture_drift_paths** — empty until the first live smoke pins the real JSON shapes.
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="mangafire",
    default_query="blue lock",
    expected_caps_antibot="cloudflare",
    needs_solver_warm=True,
    # CF warm fallback + proxy image fetch (cf comix 480.0).
    download_timeout_s=480.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "mangafire", "id_field": "mangafireHid"},
    # External tracker links: the new ``/api/titles/{hid}`` detail carries NO
    # anilist/mal id (the SPA's tracker refs are library-import only), so the source no
    # longer populates ``externalLinks``. Empty map ⇒ test_external_links_smoke SKIPS.
    expected_external_links={},
    fixture_drift_paths=[],
    perf_budget_s=None,
)
