"""MangaBall LiveSmokeProfile (D-49 / D-50).

MangaBall is registered in Plan 07-03, and D-50 requires every registered source
to ship a ``LiveSmokeProfile`` in the same PR (else the live-collection hook fails
at collection time). Plan 07-03 shipped a minimal stub to satisfy that same-PR
guard; THIS (Plan 07-04) is the finalized profile that declares the real traits
and documents the live-verify / live-tune items for the deploy-host smoke.

Cross-reference ``src/manga_gateway/sources/mangaball.py`` for the production
source metadata — this profile is the TEST-ONLY mirror; production data stays out
of this file (D-49 keeps profiles structurally separate from the Source class).

Anti-bot expectations (ESCALATED 2026-06-15 — debug mangaball-cloudflare-csrf-243)
----------------------------------------------------------------------------------
* ``expected_caps_antibot = "cloudflare"`` — matches ``MangaBallSource.antibot``.
  MangaBall ORIGINALLY served passive Cloudflare only (``antibot="none"``), but on
  2026-06-15 it enabled a site-wide managed challenge (``cf-mitigated: challenge``,
  HTTP 403 on /, /manga, /search). The escalation documented below was executed: the
  source is now ``cloudflare`` with ``solver_engine = "android"`` and the
  search/recent/manifest API rides the cf_clearance seam UNDER the httpx
  ``csrf-bootstrap`` session-prep.
* ``needs_solver_warm = True`` — MangaBall now needs a cleared Cloudflare session
  (cf_clearance) BEFORE the csrf-bootstrap GET, so the harness must warm the solver.
  The bootstrap GET itself carries the captured cf_clearance cookie + bound UA
  (framework ``session_prep.py:CsrfBootstrap``), then the X-CSRF-Token + PHPSESSID
  ride on top — the cf + csrf-bootstrap union.

CF-CLEARABILITY (RESOLVED 2026-06-15 — Android solver, NOT desktop Chromium)
---------------------------------------------------------------------------
Desktop Patchright/Chromium could NOT clear MangaBall's managed challenge from our
headed-Xvfb-Linux fingerprint — BOTH the branch nightly AND the 192.168.0.246 deploy
timed out at 60s (``cf_clearance not captured``), the same wall as kagane/mangadot.
``solver_engine = "android"`` routes clearance to the redroid-WebView sidecar, which
DID mint clearance for mangaball on the deploy (verified: ``AndroidSolver minted
clearance for source 'mangaball'`` → search returned 50 releases). CI has no redroid
(no binder kernel module), so mangaball joins ``GATEWAY_DISABLED_SOURCES`` in
``.github/workflows/nightly-live-smoke.yml`` (precedent: kagane,mangadot) AND carries a
``ci_skip_reason``; the production fix stands on the deploy's Android solver.

ESCALATION HISTORY (D-12): ``MangaBallSource.antibot`` flipped ``"none"`` →
``"cloudflare"`` + a ``cloudflare_challenge_url``, then ``solver_engine = "android"``
once desktop Chromium proved unable to clear it (above). The only glue beyond those
attrs was threading cf_clearance into the bootstrap GET (the framework already owned
the clearance path) — and adding ``mangaball.net`` to the sidecar SSRF allowlist
(``SOLVER_ALLOWED_HOSTS`` / ``android_solver/config.py``), without which the sidecar
422s the solve.

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
* **download_timeout_s** — currently 180.0. NOTE (2026-06-15 escalation): the
  Cloudflare warm is now eager at startup, so by download time clearance is cached and
  the per-chapter wall-clock is still the plaintext CDN ``.jpg`` fetch (far shorter
  than Comix's 480s; matches MangaDex's 180s). Re-size against the real end-to-end
  download wall-clock; bump if a cold CF re-solve mid-download pushes past 180s.
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
    # ESCALATED 2026-06-15 (debug mangaball-cloudflare-csrf-243): site-wide managed
    # challenge → MangaBallSource.antibot is now "cloudflare" + needs solver warm.
    expected_caps_antibot="cloudflare",
    needs_solver_warm=True,
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
    # #243: mangaball.net's managed challenge is cleared on the deploy via the redroid/
    # android-solver sidecar (Android WebView) — desktop Chromium times out from Linux.
    # CI has no `binder` kernel module so redroid cannot boot there; gated like
    # kagane/mangadot. expected_caps_antibot stays "cloudflare" (engine is an internal
    # solver detail, not a /caps classification).
    ci_skip_reason=(
        "mangaball.net Cloudflare cleared on the deploy via the redroid/"
        "android-solver sidecar (Android WebView); CI has no binder kernel "
        "module — gated, #243"
    ),
)
