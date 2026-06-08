"""Atsumaru LiveSmokeProfile (D-49 / D-50).

Atsumaru is registered in ``sources/__init__.py``; D-50 requires every registered
source to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook
fails at collection time). This profile declares the live traits + the live-tune
items for the deploy-host smoke. Cross-reference
``src/manga_gateway/sources/atsumaru.py`` for the production source metadata — this
profile is the TEST-ONLY mirror (D-49 keeps profiles structurally separate).

Anti-bot expectations
----------------------
* ``expected_caps_antibot = "none"`` — matches ``AtsumaruSource.antibot``. The
  search (Typesense proxy) + read endpoints answer cold over plain httpx with any
  User-Agent; no Cloudflare challenge, cookie, or CSRF token is engaged.
* ``needs_solver_warm = False`` — Atsumaru never engages the Patchright solver, so
  the harness must not ``await solver.warm()``.

RESIDENTIAL-ONLY caveat + ONE-ATTRIBUTE escalation path (D-12)
--------------------------------------------------------------
The ``antibot="none"`` classification was established from a RESIDENTIAL IP. A
DATACENTER-IP run MAY surface a managed Cloudflare challenge (atsu.moe fronts
Cloudflare passively); that is the unproven datacenter-IP gap, NOT a regression.
Escalation is a one-attribute flip on the production source
(``AtsumaruSource.antibot = "cloudflare"`` + ``cloudflare_challenge_url``), then
flip this profile's ``expected_caps_antibot`` → ``"cloudflare"`` and
``needs_solver_warm`` → ``True`` to match. No networking/glue change.

Release shape (D-08): Atsumaru releases carry the mangaId as the leading guid
segment (``atsumaru:{mangaId}:ch-{number}:{chapterId}``); the smoke modules key on
``id_field = "manga_id"``.

Default-query selection
-----------------------
``default_query = "one piece"`` — a high-traffic, long-running title that returns a
deterministic leading Typesense hit (``id=sVC2A``, ``title="One Piece"``, recon
2026-06-08). Selection mirrors mangadex.py's discipline: stable/long-running, a
deterministic leading hit so ``release[0]`` is well-defined, and a short newest
chapter (≈13 pages) so the download leg finishes inside ``download_timeout_s``.

LIVE-TUNE items (refine from the first deploy-host smoke)
---------------------------------------------------------
* **rate_limit_per_minute** — currently a conservative 120/min (the Cloudflare
  ceiling is unprobed). Raise it with ``scripts/probe_rate_limits.py`` once the real
  ceiling is measured.
* **recent fan-out cost** — ``recent`` fetches a full ``allChapters`` feed per
  recently-updated title (no chapter-level recent feed exists). Confirm the bounded
  fan-out (``_RECENT_TITLE_CAP``) stays comfortably under the rate budget.
* **download_timeout_s** — 180.0 (no Cloudflare warm + plaintext same-origin
  ``.webp`` fetch, matching MangaDex's plain-CDN budget). Re-size against the real
  end-to-end download wall-clock.
* **fixture_drift_paths** — empty until the first live smoke pins the real
  Typesense / read-chapter shapes; add anchors then (mirrors comix.py).
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="atsumaru",
    default_query="one piece",
    expected_caps_antibot="none",
    needs_solver_warm=False,
    download_timeout_s=180.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "atsumaru", "id_field": "manga_id"},
    fixture_drift_paths=[],
    perf_budget_s=None,
)
