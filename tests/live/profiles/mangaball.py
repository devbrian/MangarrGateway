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
  source is now ``cloudflare`` with ``solver_engine = "android"`` and on-demand
  clearance (``cloudflare_challenge_optional``). The v2 API (261003-mangaball-api-v2)
  needs NO CSRF token or session cookie — the csrf-bootstrap prep is retired.
* ``needs_solver_warm = True`` — the harness warms the android solver so a cleared
  session is available if the intermittent challenge is live.

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
attrs was adding ``mangaball.com`` to the sidecar SSRF allowlist
(``SOLVER_ALLOWED_HOSTS`` / ``android_solver/config.py``), without which the sidecar
422s the solve.

Release shape (D-08): MangaBall releases carry ``title_id`` as the leading guid
segment (``mangaball:{title_id}:ch-{number}:{lang}:{row_id}``); the
smoke modules key on ``id_field = "title_id"``.

Default-query selection
-----------------------
``default_query = "one piece"`` — chosen as a high-traffic, long-running title that
reliably returns at least one hit from ``POST /api/v1/title/search-advanced``
(``keyword=one piece`` — the v2 query key, 261003-mangaball-api-v2; the title is
``name="One Piece"`` with a stable ``_id``). Selection criteria, mirroring
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
* **Referer on the CDN image GET (A5) — RESOLVED** (261003-mangaball-api-v2): the
  CDN hotlink-blocks a bare GET, so ``MangaBallSource.fetch_image`` sends
  ``Referer: https://mangaball.com/``. Known limit: the
  ``*.poke-black-and-white.net`` zone still CF-challenges non-browser fetches.
* **rate_limit_per_minute / search + recent shapes (A2/A3)** — confirm the
  form-POST ``search-advanced`` + ``getRecentlyUpdatedChapter`` envelopes and the
  v2 ``chapter-listing-by-title-id`` JSON-body flat rows match the recon.
* **fixture_drift_paths** — empty until the first live smoke pins the real
  chapter-detail / search shapes; add anchors then (mirrors comix.py).

Alt-title live smoke (#139)
---------------------------
``alt_title_query`` / ``alt_title_expected_substring`` populated (#139): a
2026-06-05 live recon (``POST /api/v1/title/search-advanced``) confirmed mangaball
matches native/alt names server-side and ``alternateName`` carries them (a list of
plain strings in v2) — querying the Korean native title of Solo
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
    # High-traffic, long-running title; posted as the v2 ``keyword``
    # (261003-mangaball-api-v2). Live-tune for a deterministic short-chapter
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
    # No ci_skip_reason (#215 Model A): mangaball is NO LONGER unconditionally
    # CI-skipped. Its managed challenge is still unclearable by desktop Chromium from
    # Linux and is cleared via the redroid + android-solver sidecar (Android WebView) —
    # but the nightly now reaches that home android-solver over Tailscale. So this
    # source RUNS when the tailnet-reachable home solver answers /healthz and is
    # SKIPPED (not failed) by the conftest reachability gate when the solver is
    # unreachable. expected_caps_antibot stays "cloudflare" (the Android engine is an
    # internal solver detail, not a /caps classification).
)
