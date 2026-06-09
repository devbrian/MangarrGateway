"""Unit tests for ``WeebCentralSource.recent`` — DIRECT mint from /latest-updates.

``GET /latest-updates/1`` is an ID-BEARING feed (live recon 2026-06-08): one
``<article data-tip="<Title>">`` per update carrying BOTH the series link and the
newest ``/chapters/{ULID}`` link + ``Chapter N`` + ``<time datetime>``. ``recent``
mints a DIRECT release straight from each row — NO per-title fan-out (no
full-chapter-list call), NO ``:DEFERRED`` late-bind (the id is always present).

No network: a fake ``SourceContext`` returns a canned latest-updates HTML fixture
via ``get_bytes`` and records calls for assertions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.weebcentral import WeebCentralSource

_BASE = "https://weebcentral.com"


def _recent_html(rows: list[dict[str, Any]]) -> bytes:
    """Build a /latest-updates page from row dicts (mirrors the live markup)."""
    articles = []
    for r in rows:
        sid = r["series_id"]
        cid = r["chapter_id"]
        title = r["title"]
        num = r.get("number")
        label = f"Chapter {num}" if num is not None else "Oneshot"
        date = r.get("date", "2026-06-08T23:52:15.380Z")
        articles.append(
            f"""
            <article class="bg-base-100 tooltip" data-tip="{title}">
              <a class="aspect-square" href="{_BASE}/series/{sid}/{title}" preload>
                <picture>
                  <img src="https://temp.compsci88.com/cover/fallback/{sid}.jpg"
                       alt="{title} cover">
                </picture>
              </a>
              <a class="min-w-0" href="{_BASE}/chapters/{cid}" preload>
                <div class="flex-1 truncate font-semibold text-lg">{title}</div>
                <div class="flex items-center gap-2">
                  <span>{label}</span>
                </div>
                <div class="flex items-center gap-2">
                  <time class="text-datetime" datetime="{date}">{date}</time>
                </div>
              </a>
            </article>
            """
        )
    return f"<div>{''.join(articles)}</div>".encode()


class _FakeCtxForRecent:
    """``SourceContext`` stand-in: returns the canned latest-updates HTML."""

    def __init__(self, html: bytes) -> None:
        self.handle_store = HandleStore()
        self._html = html
        self.calls: list[str] = []

    async def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        return self._html


def _row(
    *,
    series_id: str = "01J76XYE8SG4HH0A8VP3XQHAFR",
    chapter_id: str = "01KTMTDD8MWBD0CTR8QKGNB5JM",
    title: str = "Getsuyoubi no Tawawa",
    number: Any = 128,
    date: str = "2026-06-08T23:52:15.380Z",
) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "chapter_id": chapter_id,
        "title": title,
        "number": number,
        "date": date,
    }


@pytest.mark.asyncio
async def test_recent_mints_direct_releases_from_feed() -> None:
    ctx = _FakeCtxForRecent(_recent_html([_row()]))
    releases = await WeebCentralSource().recent(
        languages=None, limit=50, since=None, ctx=ctx
    )
    assert len(releases) == 1
    rel = releases[0]
    assert rel.guid == (
        "weebcentral:01J76XYE8SG4HH0A8VP3XQHAFR:ch-128:01KTMTDD8MWBD0CTR8QKGNB5JM"
    )
    assert rel.chapter_number == Decimal("128")
    # RFC3339 publishDate normalized to an aware ``…+00:00`` string.
    assert rel.publish_date == "2026-06-08T23:52:15.380000+00:00"
    # DIRECT mint: the handle resolves to the BARE chapter ULID (not :DEFERRED).
    record = ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == "01KTMTDD8MWBD0CTR8QKGNB5JM"
    assert ":" not in rel.download_handle


@pytest.mark.asyncio
async def test_recent_calls_only_latest_updates_no_fanout() -> None:
    ctx = _FakeCtxForRecent(_recent_html([_row(), _row(number=129)]))
    await WeebCentralSource().recent(languages=None, limit=50, since=None, ctx=ctx)
    # Exactly ONE call — the feed; NO per-title full-chapter-list fan-out.
    assert len(ctx.calls) == 1
    assert urlparse(ctx.calls[0]).path == "/latest-updates/1"
    assert not any("/full-chapter-list" in u for u in ctx.calls)


@pytest.mark.asyncio
async def test_recent_non_english_language_filter_returns_empty() -> None:
    ctx = _FakeCtxForRecent(_recent_html([_row()]))
    releases = await WeebCentralSource().recent(
        languages=["ja"], limit=50, since=None, ctx=ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call
