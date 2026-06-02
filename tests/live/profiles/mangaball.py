"""MangaBall LiveSmokeProfile (D-49 / D-50).

MangaBall is registered in Plan 07-03, and D-50 requires every registered source
to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook fails
at collection time). This is the minimal profile that satisfies D-50; the live-tune
fields (a confirmed stable ``default_query``, the real ``download_timeout_s``, any
fixture-drift anchors, and the ``Referer``/rate-limit findings) are refined in Plan
07-04 once the live smoke runs from the deploy host.

Anti-bot expectations (cross-ref ``src/manga_gateway/sources/mangaball.py``):

* ``expected_caps_antibot = "none"`` — ``MangaBallSource.antibot`` (D-07/D-12:
  passive Cloudflare only on a residential IP; a datacenter IP MAY escalate, which
  is the Plan-04 live-verify item).
* ``needs_solver_warm = False`` — MangaBall rides the httpx ``csrf-bootstrap``
  session-prep seam, not the Patchright browser warm. No solver warm is needed.

Release shape (D-08): MangaBall releases carry ``title_id`` as the leading guid
segment; the smoke modules key on ``id_field = "title_id"``.

RESIDENTIAL-ONLY caveat (D-12): the antibot classification was established from a
residential IP. Running this smoke from a datacenter IP MAY surface a managed
Cloudflare challenge that this profile does not yet account for — re-validate from
the deploy host (Plan 04).
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="mangaball",
    # Live-tune in Plan 04 — a high-traffic title that reliably returns results.
    default_query="one piece",
    expected_caps_antibot="none",
    needs_solver_warm=False,
    # No Cloudflare warm + plaintext CDN images → far shorter than Comix's 480s.
    # Refined against the real end-to-end download wall-clock in Plan 04.
    download_timeout_s=180.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "mangaball", "id_field": "title_id"},
    # No fixture-drift anchors captured yet — Plan 04 adds them after the first
    # live smoke pins the real chapter-detail / search shapes.
    fixture_drift_paths=[],
    perf_budget_s=None,
)
