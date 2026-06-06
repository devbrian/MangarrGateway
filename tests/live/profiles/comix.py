"""Comix LiveSmokeProfile (D-49) — migrates env-knob constants from the
existing Comix live tests per the D-61 refactor map.

Constants source-of-truth refs (Phase 5 PATTERNS.md):

* ``default_query``: ``tests/live/test_comix_e2e_live.py:49``
  (``_DEFAULT_QUERY = "Forgotten Field"``)
* ``download_timeout_s``: ``tests/live/test_comix_e2e_live.py:54``
  (``_TERMINAL_TIMEOUT_S = 480.0``)
* ``max_releases_to_try``: ``tests/live/test_comix_e2e_live.py:57``
  (``_MAX_RELEASES_TO_TRY = 3``)
* fixture paths: ``tests/fixtures/comix/`` — the D-44 plaintext anchors
  captured during Plan 04-04 (chapter_9001596 pages JSON + the search
  recon JSON for the same chapter).

Anti-bot expectations (cross-ref ``src/manga_gateway/sources/comix.py``):

* ``expected_caps_antibot = "cloudflare+encrypted"`` — ``ComixSource.antibot``.
* ``needs_solver_warm = True`` — Comix requires a Patchright warm so the
  framework can inject ``cf_clearance`` + matching UA per request
  (``test_comix_e2e_live.py:146`` calls ``await solver.warm()``).

Fixture-path climb:
This profile file lives at ``tests/live/profiles/comix.py``; the fixtures
live at ``tests/fixtures/comix/``. The relative climb is therefore
``parents[2]`` — one deeper than ``tests/live/test_comix_live.py``'s
``parents[1]`` climb (PATTERNS.md flags this explicitly). The
gate-deterministic test ``test_comix_fixture_paths_exist_on_disk``
catches a parents[1] vs parents[2] regression.
"""

from __future__ import annotations

from pathlib import Path

from ._base import LiveSmokeProfile

# Profile file lives one directory deeper than test_comix_live.py, so the
# relative climb is parents[2] (NOT parents[1]). PATTERNS.md flags this.
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "comix"

LIVE_SMOKE = LiveSmokeProfile(
    source_key="comix",
    default_query="Forgotten Field",
    expected_caps_antibot="cloudflare+encrypted",
    needs_solver_warm=True,
    # 480s = the migrated _TERMINAL_TIMEOUT_S from test_comix_e2e_live.py;
    # accounts for Cloudflare warm + browser-DOM page-list read + per-page
    # image fetch through the at-home CDN.
    download_timeout_s=480.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    # D-46 — Comix releases carry hid as the canonical series identifier;
    # the release pattern doc-flags the field for the smoke modules.
    expected_release_pattern={"sourceKey": "comix", "id_field": "hid"},
    fixture_drift_paths=[
        _FIXTURE_DIR / "chapter_9001596_pages.json",
        _FIXTURE_DIR / "search_the_forgotten_field.json",
    ],
    # D-61: perf budget owned by test_comix_perf_live.py for v1.
    perf_budget_s=None,
    # Alt-title live smoke (#139): the Korean native title of "The Forgotten
    # Field" (hid mr3m0, altTitles ["잊혀진 들판"] in the search payload). A live
    # search for the native name must resolve to the same series the English
    # ``default_query="Forgotten Field"`` does — proving alt-title matching works
    # end-to-end against the real source, not just the offline fixture.
    alt_title_query="잊혀진 들판",
    alt_title_expected_substring="Forgotten Field",
)
