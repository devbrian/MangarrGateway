"""Live scramble-quality check for MangaFire (#218 tripwire + regression guard).

MangaFire's per-page geometric scrambling (``offset>0`` → ``#scr_{offset}`` →
``_descramble_image``) is a defensive port of Keiyoushi ``ImageInterceptor.kt`` and has
**never been observed live** here (a 2026-06-10 probe found 0 scrambled pages across 20
Blue Lock chapters + 80 catalog titles). So this test:

* scans Blue Lock's recent few chapter manifests for an ``offset>0`` page;
* if none → ``pytest.skip`` (the normal case today — the nightly stays green, and a
  skip→pass transition is itself the "scrambling came back" signal);
* if found → fetches the raw scrambled page and asserts ``_descramble_image`` produced a
  quality-preserving result (source qtables kept — the #218 guarantee the packaging
  ``is_valid_image`` guard does not check).

Bounded to a handful of chapters (no heavy catalog scan) — catching a never-observed
event is not worth a multi-minute nightly. Complements the zero-cost piggyback probe in
``test_download_smoke.py`` which watches whatever chapter the full-cycle smoke fetches.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urldefrag

import pytest

from manga_gateway.sources.mangafire import _descramble_image

from ._helpers import (
    app_of,
    assert_descramble_preserves_quality,
    live_client_for,
)
from .profiles.mangafire import LIVE_SMOKE

pytestmark = pytest.mark.live

# Blue Lock — the pinned MangaFire recon title (#192); the id half of its slug.id token.
_BLUE_LOCK_SLUG_ID = "kw9j9"
# Recent chapters to probe for scrambling — bounded; scrambling is per-title-consistent
# (all 20 probed Blue Lock chapters agreed), so a small window is a fair signal.
_SCAN_CHAPTERS = 8


async def test_mangafire_descramble_quality_on_live_scrambled_pages() -> None:
    """If MangaFire serves a scrambled page, descramble must keep its quality (#218)."""
    async with live_client_for(LIVE_SMOKE) as client:
        app = app_of(client)
        source = app.state.registry.get("mangafire")()
        # The same SourceContext the download route builds (anti-bot seams wired).
        ctx = app.state.job_manager._engine._build_context(source)

        chapters = await source._chapter_list(_BLUE_LOCK_SLUG_ID, "en", ctx)
        scrambled_url: str | None = None
        scanned = 0
        for chapter in chapters[:_SCAN_CHAPTERS]:
            href = chapter.get("href")
            if not href:
                continue
            scanned += 1
            try:
                manifest = await source.fetch_manifest(href, ctx)
            except Exception:  # noqa: BLE001 — a bad chapter must not stop the scan
                continue
            hits = [url for url in manifest if "#scr_" in url]
            if hits:
                scrambled_url = hits[0]
                break

        if scrambled_url is None:
            pytest.skip(
                f"MangaFire scrambling dormant — 0 offset>0 pages in {scanned} chapters"
            )

        clean, frag = urldefrag(scrambled_url)
        offset = int(frag[4:]) if frag.startswith("scr_") else 0
        raw = await ctx.get_bytes(clean)
        out = await asyncio.to_thread(_descramble_image, raw, offset)
        assert_descramble_preserves_quality(raw, out)
        print(
            f"\n[live mangafire] descramble quality verified on a live scrambled page "
            f"(offset={offset}) — #218 guard ACTIVE"
        )
