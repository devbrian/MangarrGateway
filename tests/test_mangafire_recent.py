"""Unit tests for ``MangaFireSource.recent`` — /filter cards → DIRECT newest chapter.

``GET /filter?sort=recently_updated&language[]={lang}&page=1`` (HTML, NO vrf) returns
title-only cards; ``recent`` fans out the per-title chapter list
(``GET /ajax/manga/{slugId}/chapter/{lang}``, HTML-in-``result``) under a bounded
semaphore and mints the NEWEST chapter DIRECT (D-07). No ``:DEFERRED`` — the read href
(the resolve unit) is always present.

No network: a fake ``SourceContext`` routes ``get_bytes`` (filter HTML) + ``get_json``
(chapter-list result) by URL and mints real handles via a ``HandleStore``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangafire import MangaFireSource

_GUID_RE = re.compile(r"^mangafire:[\w.-]+:ch-[\d.?]+:[a-z-]+$")


def _cards_html(cards: list[tuple[str, str]]) -> bytes:
    """Build a /filter page: one .original.card-lg .unit .inner per (href, title)."""
    inners = "\n".join(
        f"""
        <div class="inner">
          <a href="{href}" class="poster">
            <img src="https://mangafire.to/thumb/{title.replace(" ", "-")}.jpg">
          </a>
          <div class="info"><a href="{href}">{title}</a></div>
        </div>
        """
        for href, title in cards
    )
    return (
        f"<div class='original card-lg'><div class='unit'>{inners}</div></div>".encode()
    )


def _chapter_list_html(chapters: list[dict[str, Any]]) -> str:
    """Build a chapter-list `result` fragment (newest-first), one <li> per chapter."""
    rows = "".join(
        f"""
        <li class="item" data-number="{ch["number"]}">
          <a href="{ch["href"]}">
            <span>Chapter {ch["number"]}: </span>
            <span>{ch.get("date", "May 21, 2026")}</span>
          </a>
        </li>
        """
        for ch in chapters
    )
    return f"<ul>{rows}</ul>"


class _FakeCtx:
    def __init__(self, *, filter_html: bytes, chapter_lists: dict[str, str]) -> None:
        self.handle_store = HandleStore()
        self._filter_html = filter_html
        self._chapter_lists = chapter_lists
        self.get_bytes_calls: list[str] = []
        self.get_json_calls: list[str] = []

    async def get_bytes(self, url: str) -> bytes:
        self.get_bytes_calls.append(url)
        return self._filter_html

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.get_json_calls.append(url)
        slug_id = url.split("/ajax/manga/")[1].split("/")[0]
        return {"status": 200, "result": self._chapter_lists.get(slug_id, "")}


@pytest.mark.asyncio
async def test_recent_mints_direct_newest_chapter_per_title() -> None:
    cards = [
        ("/manga/blue-lockk.kw9j9", "Blue Lock"),
        ("/manga/one-piece.abcd", "One Piece"),
    ]
    chapter_lists = {
        "kw9j9": _chapter_list_html(
            [
                {"number": "346.2", "href": "/read/blue-lockk.kw9j9/en/chapter-346.2"},
                {"number": "346.1", "href": "/read/blue-lockk.kw9j9/en/chapter-346.1"},
            ]
        ),
        "abcd": _chapter_list_html(
            [{"number": "1120", "href": "/read/one-piece.abcd/en/chapter-1120"}]
        ),
    }
    ctx = _FakeCtx(filter_html=_cards_html(cards), chapter_lists=chapter_lists)
    releases = await MangaFireSource().recent(
        languages=None, limit=50, since=None, ctx=ctx
    )

    assert len(releases) == 2
    by_title = {r.manga_title: r for r in releases}
    # Newest chapter (the first/newest-first row) is the DIRECT mint.
    assert by_title["Blue Lock"].chapter_number == Decimal("346.2")
    assert by_title["One Piece"].chapter_number == Decimal("1120")
    for rel in releases:
        assert _GUID_RE.match(rel.guid), rel.guid
        assert rel.download_handle
        assert ":" not in rel.download_handle  # opaque, not a structured composite
        record = ctx.handle_store.resolve(rel.download_handle)
        assert record is not None
        # The chapter_id is the read href (the resolve unit, D-09).
        assert record.chapter_id.startswith("/read/")
        assert rel.language == "en"
    # The /filter recent feed was fetched with the recently_updated sort.
    assert any("recently_updated" in u for u in ctx.get_bytes_calls)


@pytest.mark.asyncio
async def test_recent_skips_title_with_empty_chapter_list() -> None:
    cards = [
        ("/manga/blue-lockk.kw9j9", "Blue Lock"),
        ("/manga/empty.zzzz", "Empty Title"),
    ]
    chapter_lists = {
        "kw9j9": _chapter_list_html(
            [{"number": "1", "href": "/read/blue-lockk.kw9j9/en/chapter-1"}]
        ),
        "zzzz": "",  # no chapters → skipped, never crashes the whole poll
    }
    ctx = _FakeCtx(filter_html=_cards_html(cards), chapter_lists=chapter_lists)
    releases = await MangaFireSource().recent(
        languages=["en"], limit=50, since=None, ctx=ctx
    )
    assert [r.manga_title for r in releases] == ["Blue Lock"]


@pytest.mark.asyncio
async def test_recent_bounds_fanout_by_limit() -> None:
    cards = [(f"/manga/t{i}.s{i}", f"Title {i}") for i in range(5)]
    chapter_lists = {
        f"s{i}": _chapter_list_html(
            [{"number": "1", "href": f"/read/t{i}.s{i}/en/chapter-1"}]
        )
        for i in range(5)
    }
    ctx = _FakeCtx(filter_html=_cards_html(cards), chapter_lists=chapter_lists)
    releases = await MangaFireSource().recent(
        languages=None, limit=2, since=None, ctx=ctx
    )
    # limit=2 bounds the per-title fan-out → at most 2 chapter-list GETs + 2 releases.
    assert len(releases) == 2
    assert len(ctx.get_json_calls) == 2
