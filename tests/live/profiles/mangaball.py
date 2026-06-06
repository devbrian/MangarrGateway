"""MangaBall LiveSmokeProfile (D-49 / D-50).

MangaBall is registered in Plan 07-03, and D-50 requires every registered source
to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook fails
at collection time). Plan 07-03 shipped a minimal stub to satisfy that same-PR
guard; THIS (Plan 07-04) is the finalized profile that declares the real traits
and documents the live-verify / live-tune items for the deploy-host smoke.

Cross-reference ``src/manga_gateway/sources/mangaball.py`` for the production
source metadata — this profile is the TEST-ONLY mirror; production data stays out
of this file (D-49 keeps profiles structurally separate from the Source class).

Anti-bot expectations
----------------------
* ``expected_caps_antibot = "none"`` — matches ``MangaBallSource.antibot`` (D-12).
  MangaBall serves passive Cloudflare only; the search/recent/manifest API is plain
  JSON/HTML over the httpx ``csrf-bootstrap`` session-prep seam.
* ``needs_solver_warm = False`` — MangaBall rides the httpx ``csrf-bootstrap``
  session-prep (PHPSESSID + X-CSRF-Token), NOT the Patchright browser warm. The
  Cloudflare solver is never engaged, so the harness must not ``await solver.warm()``.

RESIDENTIAL-ONLY caveat + ONE-ATTRIBUTE escalation path (D-12)
--------------------------------------------------------------
The ``antibot="none"`` classification was established from a RESIDENTIAL IP only.
Running this smoke from a DATACENTER IP MAY surface a managed Cloudflare challenge
that this profile does not account for — that is the unproven #65/#82 datacenter-IP
gap, NOT a profile or source regression. Phase completion is deliberately NOT gated
on a datacenter-IP CF check.

If a datacenter-IP run does surface a managed challenge, escalation is a
ONE-ATTRIBUTE flip on the production source — set
``MangaBallSource.antibot = "cloudflare"`` in ``src/manga_gateway/sources/mangaball.py``
(which routes it through the shared CloudflareSolver, exactly like Comix) and then
flip this profile's ``expected_caps_antibot`` to ``"cloudflare"`` and
``needs_solver_warm`` to ``True`` to match. No networking/glue code changes — the
framework already owns the clearance path.

Release shape (D-08): MangaBall releases carry ``title_id`` as the leading guid
segment (``mangaball:{title_id}:ch-{number}:{language}:{translation_id}``); the
smoke modules key on ``id_field = "title_id"``.

Default-query selection
-----------------------
``default_query = "one piece"`` — chosen as a high-traffic, long-running title that
reliably returns at least one hit from ``POST /api/v1/title/search-advanced/``
(``search_input=one piece`` is the literal recon-probed example, 07-RECON-mangaball.md
§1 — ``name="One Piece"``, a stable ``_id``). Selection criteria, mirroring
mangadex.py's discipline:

* stable / long-running (won't disappear or get de-listed mid-test)
* a deterministic leading search hit so ``release[0]`` is well-defined
* at least one short, available chapter for the download leg to finish inside
  ``download_timeout_s``

LIVE-TUNE items (refine from the first deploy-host smoke; A2/A3/A5/A7)
---------------------------------------------------------------------
The first real ``uv run pytest -m live -k mangaball`` from the deploy host
confirms / tunes:

* **default_query stability** — confirm "one piece" still returns a deterministic
  leading hit with an available short chapter; swap if the catalog shifts.
* **download_timeout_s** — currently 180.0 (no Cloudflare warm + plaintext CDN
  ``.jpg`` fetch → far shorter than Comix's 480s; matches MangaDex's 180s). Re-size
  against the real end-to-end download wall-clock.
* **Referer on the CDN image GET (A5; RECON §4 / Open Q4)** — the
  ``chikorita.red-and-blue.net/storage/...`` CDN likely enforces hotlink
  protection. If the bare image GET 403s live, ``MangaBallSource.fetch_image`` must
  add ``Referer: https://mangaball.net/`` (the fetch_image comment flags this).
* **rate_limit_per_minute / search + recent shapes (A2/A3)** — confirm the
  form-POST ``search-advanced`` + ``getRecentlyUpdatedChapter`` envelopes and the
  ``chapter-listing-by-title-id`` flat shape match the recon (A7 fixture anchors).
* **fixture_drift_paths** — empty until the first live smoke pins the real
  chapter-detail / search shapes; add anchors then (mirrors comix.py).

Alt-title live smoke (#139)
---------------------------
``alt_title_query`` / ``alt_title_expected_substring`` populated (#139): a
2026-06-05 live recon (CSRF-bootstrap + ``POST /api/v1/title/search-advanced/``)
confirmed mangaball matches native/alt names server-side and the ``alternateName``
``/``-separated HTML blob carries them — querying the Korean native title of Solo
Leveling (``나 혼자만 레벨업``) returns the "Solo Leveling" series (its
``alternateName`` leads with that exact string). High-traffic, stable → a
deterministic alt-title smoke. The query matches ONLY via the alt name (the
English main name "Solo Leveling" does not contain the Korean string), so a
release whose title contains "Solo Leveling" proves the ``_split_alt`` +
alt-title-aware prune path resolves end-to-end through the gateway.
"""

from __future__ import annotations

from ._base import LiveSmokeProfile

LIVE_SMOKE = LiveSmokeProfile(
    source_key="mangaball",
    # High-traffic, long-running title; literal recon-probed search_input
    # (07-RECON-mangaball.md §1). Live-tune for a deterministic short-chapter
    # leading hit if the catalog shifts (see docstring "Default-query selection").
    default_query="one piece",
    expected_caps_antibot="none",
    needs_solver_warm=False,
    # No Cloudflare warm + plaintext CDN images → far shorter than Comix's 480s;
    # 180s matches MangaDex's plain-CDN budget. Refined against the real
    # end-to-end download wall-clock on the first deploy-host smoke.
    download_timeout_s=180.0,
    max_releases_to_try=3,
    min_releases_returned=1,
    expected_release_pattern={"sourceKey": "mangaball", "id_field": "title_id"},
    # No fixture-drift anchors captured yet — added after the first live smoke
    # pins the real chapter-detail / search shapes (mirrors comix.py).
    fixture_drift_paths=[],
    perf_budget_s=None,
    # Alt-title live smoke (#139) — recon-verified 2026-06-05 (see module docstring).
    alt_title_query="나 혼자만 레벨업",
    alt_title_expected_substring="Solo Leveling",
)
