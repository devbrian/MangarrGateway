"""Opt-in Comix live smoke + D-44 fixture recorder (Plan 04-04 Task 3, D-43/D-44).

This is the ONLY test that touches the live Comix site through a REAL Patchright
browser + real Cloudflare. It is marked ``@pytest.mark.live`` and therefore EXCLUDED
from ``uv run nox -s gate`` (the ``addopts = "-m 'not live'"`` in pyproject.toml) —
it never runs in CI / the deterministic gate (D-42/D-43).

Option A (Plan 04-04, 2026-05-30) — browser-driven manifest fetch
================================================================

The original D-44 plan (capture the encrypted ``/api/v1/chapters/{id}`` body and
prove static decryption against a known plaintext) was superseded after the
direct probe established that the encrypted-pages endpoint requires a ``_=``
query token minted by the same VM-obfuscated ``secure-*.js`` that does the
``comix-v1`` cipher. The token is bound to the chapter id and cannot be reliably
minted statically — even with a fresh cf_clearance + Laravel session and the
right Accept/X-Requested-With/Referer headers, the API returns
``403 {"message":"Missing token."}``.

Option A instead lets the live page's own JS do token-mint + API call + decrypt
+ image-tag render, then reads the page list off the rendered DOM via
``CloudflareSolver.fetch_via_browser`` (Commit 1). The D-44 fixture pair shifts:

* ``chapter_9001596.bin`` (the original ciphertext) stays committed as a
  historical artifact — it pins what the API returns under live recon
  conditions for any future ``comix-v1`` cipher work.
* ``chapter_9001596_pages.json`` becomes the NEW canonical D-44 plaintext
  fixture: a real-captured list of CDN page URLs that the browser-DOM read
  produces. This is the byte-exact assertion target — the same chapter id
  decoded by the same ``CloudflareSolver.fetch_via_browser`` extractor used
  by ``ComixSource.fetch_manifest`` should yield the same list.

What this smoke now does (a human runs it after `uv run patchright install
chromium`):

1. Drive the REAL :class:`CloudflareSolver` against the real Comix challenge
   URL, solve Cloudflare, and capture a ``cf_clearance`` cookie + the issuing
   UA + the Laravel ``session`` cookie. Headless is the default now that the
   recon-time HEADED solve has been transposed to HEADLESS by the solver
   cookie-polling + host-scope fixes (set ``COMIX_LIVE_HEADLESS=0`` if the
   live site escalates and headless ever stops passing).
2. Call ``solver.fetch_via_browser(chapter_url, extract=..., wait_for=...)``
   with the same JS extractor ``ComixSource.fetch_manifest`` uses — exercises
   the full Option A path end-to-end through one warm Patchright context.
3. Assert the returned URL list contains the recon-pinned token
   ``bEqPbYfoPT0GmlnlAkqfoBJE4oENYsQ`` (the chapter is stable on the live
   site as of 2026-05-30; if comix.to rotates the page-list token later, this
   assertion will be the bright-red flag).
4. Persist the URL list into ``tests/fixtures/comix/chapter_9001596_pages.
   json`` as the new D-44 plaintext canon (REAL captured browser-DOM read,
   never synthetic).

Reusability (D-43 / GH #17): path-agnostic (fixtures resolved relative to this
file), marker-gated, and self-contained, so the future nightly job reuses it
unchanged. Knobs come from env vars (``COMIX_LIVE_*``) with sane defaults — no
hard-coded local paths.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from manga_gateway.framework.antibot import CloudflareSolver
from manga_gateway.sources.comix import (
    _CHAPTER_PAGES_EXTRACT_JS,
    _CHAPTER_PAGES_WAIT_FOR,
    ComixSource,
)

pytestmark = pytest.mark.live  # excluded from the gate (D-43)

# Fixtures live beside the deterministic gate's consumers (tests/fixtures/comix),
# resolved RELATIVE to this file so the smoke is path-agnostic (D-43 / GH #17).
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "comix"


def _headless() -> bool:
    """Open Q2 knob: default headless; ``COMIX_LIVE_HEADLESS=0`` runs headed.

    Default flipped to ``True`` (was provisional during recon-era headed
    debugging) now that the solver's cookie-polling + host-scope cookie filter
    let Patchright pass comix.to Cloudflare in headless mode.
    """
    return os.environ.get("COMIX_LIVE_HEADLESS", "1") != "0"


def _challenge_url() -> str:
    """The real Comix challenge URL (defaults to ``https://comix.to/``).

    Pinned by the 2026-05-30 live recon (CONTEXT.md ``<live_recon>``); override
    with ``COMIX_LIVE_CHALLENGE_URL`` if comix.to ever migrates.
    """
    return os.environ.get("COMIX_LIVE_CHALLENGE_URL", ComixSource.base_url + "/")


def _chapter_url() -> str:
    """The real Comix chapter HTML page to drive Option A against.

    Defaults to chapter 9001596 ("The Forgotten Field" ch.20) — the same
    chapter the recon-captured ``chapter_9001596.bin`` ciphertext corresponds
    to. Override with ``COMIX_LIVE_CHAPTER_URL`` to capture a different
    chapter (e.g. for fixture regeneration after a site change).
    """
    return os.environ.get(
        "COMIX_LIVE_CHAPTER_URL",
        f"{ComixSource.base_url}/title/mr3m0-the-forgotten-field/9001596-chapter-20",
    )


# The page-list token observed in the recon DOM as the prefix of every page-
# image URL (https://jloo.wowpic3.store/si/bEqPbYfoPT0GmlnlAkqfoBJE4oENYsQ/NN.
# webp). Bound to the chapter id (NOT to a session): a token captured weeks
# ago on a different IP still validates today on a fresh session, so we can
# pin it as the structural signal that the Option A round-trip reached the
# real live page-list and not a Cloudflare interstitial.
_KNOWN_CDN_TOKEN = "bEqPbYfoPT0GmlnlAkqfoBJE4oENYsQ"


async def test_comix_live_browser_fetch_records_d44_plaintext() -> None:
    """Solve real Cloudflare → browser-DOM read of a chapter page → record."""
    await asyncio.to_thread(_FIXTURE_DIR.mkdir, parents=True, exist_ok=True)

    # Production lifespan reads ``cloudflare_keys`` off the source registry by
    # walking antibot levels; the live test stands the solver up directly so we
    # pass ComixSource.key explicitly (mirrors what app.py derives).
    solver = CloudflareSolver(
        headless=_headless(),
        challenge_url=_challenge_url(),
        decrypt_url=_challenge_url(),
        cloudflare_keys=(ComixSource.key,),
    )
    try:
        # 1. Solve the REAL Cloudflare challenge (Open Q2 headless viability).
        clearance = await solver.get_clearance("comix")
        assert clearance is not None, (
            "Cloudflare solve returned no clearance — if headless was blocked, "
            "set COMIX_LIVE_HEADLESS=0 and re-run, or escalate to Camoufox "
            "(deferred)."
        )
        assert clearance.cookies.get("cf_clearance"), "no cf_clearance captured"
        assert clearance.user_agent, "no issuing UA captured (D-40 binding)"

        # 2. Drive the SAME browser-fetch primitive ComixSource.fetch_manifest
        #    uses, with the SAME extractor + wait_for. The warm context shares
        #    the cf_clearance + session cookies the solve captured; the page's
        #    own JS handles the ``_=`` token mint + encrypted-API call + decrypt
        #    + image-tag render. We just read the result.
        urls = await solver.fetch_via_browser(
            _chapter_url(),
            extract=_CHAPTER_PAGES_EXTRACT_JS,
            wait_for=_CHAPTER_PAGES_WAIT_FOR,
            timeout=45.0,  # generous: the SPA may need to download + execute
            # secure-*.js + run the cipher VM + hit the CDN before the first
            # /si/ img renders.
        )

        # 3. Assert the round-trip reached the real chapter page (not a CF
        #    interstitial, not a 404 SPA shell).
        assert isinstance(urls, list) and urls, (
            "browser fetch returned no page URLs — likely the CSS-selector "
            f"wait_for ({_CHAPTER_PAGES_WAIT_FOR!r}) never matched, meaning "
            "the SPA did not render image tags. Either the chapter page "
            "structure changed or Cloudflare escalated."
        )
        assert all(isinstance(u, str) and u for u in urls), (
            "extractor returned non-string entries — JS shape drift"
        )
        # Structural plaintext signal — proves the page-list cipher actually
        # ran end-to-end against the live cipher VM.
        token_hits = [u for u in urls if _KNOWN_CDN_TOKEN in u]
        assert token_hits, (
            f"none of the {len(urls)} returned URLs contained the recon-pinned "
            f"CDN token {_KNOWN_CDN_TOKEN!r} — either comix.to rotated the "
            "page-list token since the recon, the chapter id was changed via "
            "COMIX_LIVE_CHAPTER_URL, or the extractor selected the wrong "
            "images (cross-site ads / avatars). First three URLs sampled: "
            f"{urls[:3]!r}"
        )

        # 4. Persist the page list as the new D-44 plaintext fixture. The
        #    historical chapter_9001596.bin ciphertext stays committed as the
        #    original D-44 artifact; this JSON is the Option A successor.
        await asyncio.to_thread(
            (_FIXTURE_DIR / "chapter_9001596_pages.json").write_text,
            json.dumps(urls, indent=2) + "\n",
            encoding="utf-8",
        )

        # Transient breadcrumb (ignored by .gitignore) so the human knows the
        # fixture was rewritten on this live run.
        await asyncio.to_thread(
            (_FIXTURE_DIR / "CAPTURE_NOTES.txt").write_text,
            "Live smoke wrote chapter_9001596_pages.json (Option A page list,\n"
            "real-captured via solver.fetch_via_browser against the live\n"
            "chapter HTML page). The historical chapter_9001596.bin\n"
            "ciphertext is preserved unchanged as a future-comix-v1-cipher\n"
            f"reference artifact. Headless: {'yes' if _headless() else 'no'}.\n"
            f"Pages returned: {len(urls)}.\n",
            encoding="utf-8",
        )
    finally:
        # Tear the browser down — no orphan Chromium (Pitfall 4).
        await solver.aclose()
