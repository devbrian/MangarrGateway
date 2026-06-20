"""ProjectSuki LiveSmokeProfile (D-49 / D-50).

ProjectSuki is registered in ``sources/__init__.py``; D-50 requires every registered
source to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook
fails at collection time). This profile declares the real traits and documents the
live-tune items for the deploy-host smoke.

Cross-reference ``src/manga_gateway/sources/projectsuki.py`` for the production source
metadata — this profile is the TEST-ONLY mirror; production data stays out of this
file (D-49 keeps profiles structurally separate from the Source class).

Anti-bot expectations (none-prep, residential-IP classification)
----------------------------------------------------------------
* ``expected_caps_antibot = "none"`` — matches ``ProjectSukiSource.antibot``.
  Cloudflare fronts projectsuki.com in CDN-only/passive mode: a direct GET returned
  200 with no challenge from the residential recon IP (2026-06-20). So no solver is
  wired and ``needs_solver_warm = False`` — the source RUNS in the CI nightly (no
  ``ci_skip_reason``: it needs no redroid/Android sidecar).
* One-line CF escalation (D-12): the ``none`` classification was made from a
  residential IP. If a datacenter IP (or a future site change) trips a MANAGED
  challenge, escalate ``ProjectSukiSource`` with the documented one-attribute flip
  (``antibot = "cloudflare"`` + ``cloudflare_challenge_url`` + a ``solver_engine``)
  and bump ``expected_caps_antibot`` here to ``"cloudflare"`` — the framework owns the
  clearance path, so no networking glue changes.

Release shape (D-08): ProjectSuki releases carry ``projectsukiBookId`` in their
``ids`` (and as the leading guid segment ``projectsuki:{bookId}:ch-{N}:{chapterId}``);
the smoke modules key on ``id_field = "projectsukiBookId"`` to match the Release.ids
key the source emits.

Default-query selection
-----------------------
``default_query = "solo leveling"`` — a high-traffic, long-running title with a clean
deterministic leading ``/book`` hit (recon 2026-06-20: 221 chapters in one /book
page). Selection criteria mirror mangadex.py's discipline:

* stable / long-running (won't disappear or get de-listed mid-test)
* a deterministic leading search hit so ``release[0]`` is well-defined
* at least one short, available chapter for the download leg to finish inside
  ``download_timeout_s``

LIVE-TUNE items (refine from the first deploy-host smoke)
--------------------------------------------------------
The first real ``uv run pytest -m live -k projectsuki`` from the deploy host
confirms / tunes:

* **Referer on the page-image GET** — ``ProjectSukiSource.fetch_image`` issues a bare
  GET (200 in recon). If the ``/images/gallery/...`` CDN starts hotlink-protecting,
  add ``Referer: https://projectsuki.com/`` (the fetch_image comment flags this).
* **page filename padding (``00{n}``)** — page 1 is ``001``, page 10 is ``0010``,
  page 24 is ``0024`` (NO file extension, a ``?{ts}`` query). The manifest EXTRACTS
  every URL from the read page + the ``/callpage`` page-walk; confirm the page-walk
  exhausts (``super`` → false) and the extracted count matches the chapter length.
* **default_query stability** — confirm "solo leveling" still returns a deterministic
  leading ``/book`` hit with an available short chapter; swap if the catalog shifts.
* **download_timeout_s** — currently 120.0 (plaintext CDN ``.jpg``, no CF warm).
  Re-size against the real end-to-end download wall-clock.
* **rate_limit_per_minute / max_concurrent_jobs** — both are conservative starts
  (30 / 3); probe-tune them (Step 5.5) once live behaviour is observed.
* **date rendering** — the chapter-row/recent-card date format was a recon gap
  (``_date_to_iso`` parses ISO + common absolute shapes, else falls back to now).
  Confirm the live shape and extend the parser if needed.
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="projectsuki",
    # High-traffic, long-running title; deterministic leading /book hit (recon
    # 2026-06-20). Live-tune for a short-chapter leading hit if the catalog shifts.
    default_query="solo leveling",
    # CDN-only/passive Cloudflare from the residential recon IP → antibot "none";
    # no solver warm needed, so the source runs in the CI nightly.
    expected_caps_antibot="none",
    needs_solver_warm=False,
    # Plaintext CDN images, no CF warm → shorter than the cloudflare sources. Refined
    # against the real end-to-end download wall-clock on the first deploy-host smoke.
    download_timeout_s=120.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    # The Release.ids key the source emits (projectsuki:{bookId}:... guid leads with
    # the bookId; ids carries projectsukiBookId + projectsukiChapterId).
    expected_release_pattern={
        "sourceKey": "projectsuki",
        "id_field": "projectsukiBookId",
    },
    # No fixture-drift anchors captured yet — added after the first live smoke pins
    # the real search / book / read-page / callpage shapes (mirrors comix.py).
    fixture_drift_paths=[],
    perf_budget_s=None,
)
