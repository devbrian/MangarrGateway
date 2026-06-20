"""Unit tests for ``MangadotSource.recent`` — title feed → newest-chapter mint (#94).

The recent feed seeds from the ``sortBy=latest`` title feed (``GET /api/search``,
``search=""``, ``page=1`` — manga-level, TITLE-ONLY), then fans out exactly ONE
``GET /api/manga/{id}/chapters/list`` per recently-updated title and mints that
title's SINGLE newest chapter as a DIRECT release (``1 + N`` calls, no ``:DEFERRED``;
the minted handle's ``ResolutionRecord.chapter_id`` is the ``{id}|{source}`` composite).

The KEY difference from atsumaru/mangafire (whose feeds are pre-sorted newest-first):
mangadot's ``chapters/list`` is NOT newest-first, so the newest chapter is selected by
parsed ``date_added`` (``_parse_ts``), NOT the array head.

No network: a fake ``SourceContext`` routes ``get_json`` (the search-feed envelope)
vs ``get_json_array`` (the per-title bare chapter array) by URL and records calls.
recent() does NOT resolve external links, so no links seam is needed.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangadot import (
    _CHAPTERS_FANOUT_CONCURRENCY,
    _RECENT_TITLE_CAP,
    MangadotSource,
)

# guid contract (D-08): mangadot:{manga_id}:ch-{number}:{lang}:{chapter_id}
_GUID_RE = re.compile(r"^mangadot:[^:]+:ch-[\d.?]+:[a-z-]{2,}:[^:]+$")

_BASE = "https://mangadot.net"
_SEARCH = f"{_BASE}/api/search"


def _chapters_url(manga_id: str) -> str:
    return f"{_BASE}/api/manga/{manga_id}/chapters/list"


class _FakeCtxForRecent:
    """``SourceContext`` stand-in: routes ``get_json`` vs ``get_json_array`` by URL.

    ``GET /api/search`` returns the title-only ``{manga_list:[...]}`` envelope (and
    records its params so the feed call can be asserted); ``GET /api/manga/{id}/
    chapters/list`` returns the bare chapter array keyed by the manga id parsed out
    of the URL. A real ``HandleStore`` backs the mint so handles resolve.
    """

    def __init__(
        self,
        *,
        manga_list: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.handle_store = HandleStore()
        self._manga_list = manga_list
        self._listings = listings
        self.json_calls: list[tuple[str, dict[str, Any]]] = []
        self.array_calls: list[str] = []

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.json_calls.append((url, params))
        if url == _SEARCH:
            return {
                "manga_list": self._manga_list,
                "pagination": {"current_page": 1, "total_pages": 1},
            }
        raise AssertionError(f"unexpected get_json url: {url}")

    async def get_json_array(self, url: str, **params: Any) -> list[Any]:
        self.array_calls.append(url)
        m = re.match(rf"{re.escape(_BASE)}/api/manga/(.+)/chapters/list$", url)
        if not m:
            raise AssertionError(f"unexpected get_json_array url: {url}")
        return self._listings.get(m.group(1), [])


def _ctx(
    *,
    manga_list: list[dict[str, Any]],
    listings: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    return _FakeCtxForRecent(manga_list=manga_list, listings=listings or {})


def _manga(*, manga_id: str, title: str = "Murim Psychopath") -> dict[str, Any]:
    """A TITLE-ONLY ``sortBy=latest`` feed hit — NO chapters embedded (RESEARCH)."""
    return {
        "id": manga_id,
        "title": title,
        "chapter_count": 0,  # manga-level, unreliable — ignored (we fan out per title)
        "status": "ongoing",
    }


def _row(
    *,
    chapter_id: str,
    chapter_number: Any = 1,
    language: str = "en",
    group_name: str | None = "Stick",
    date_added: str = "2026-05-12 12:21:39+00",
    page_count: int = 75,
    source: str = "user",
    comment_count: int = 0,
) -> dict[str, Any]:
    """One bare chapter-array row (mirrors test_mangadot_search.py ``_row``)."""
    return {
        "id": chapter_id,
        "chapter_number": chapter_number,
        "volume_number": None,
        "chapter_title": None,
        "language": language,
        "group_name": group_name,
        "scanlator_name": "Stick",
        "date_added": date_added,
        "page_count": page_count,
        "source": source,
        "comment_count": comment_count,
    }


@pytest.mark.asyncio
async def test_recent_mints_newest_chapter_per_title() -> None:
    ctx = _ctx(
        manga_list=[
            _manga(manga_id="m1", title="One Piece"),
            _manga(manga_id="m2", title="Naruto"),
        ],
        listings={
            "m1": [
                _row(
                    chapter_id="op1",
                    chapter_number=1183,
                    date_added="2026-05-01 00:00:00+00",
                ),
                _row(
                    chapter_id="op2",
                    chapter_number=1184,
                    date_added="2026-05-10 00:00:00+00",
                ),
            ],
            "m2": [_row(chapter_id="nr1", chapter_number=700)],
        },
    )
    releases = await MangadotSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    assert len(releases) == 2
    by_title = {r.manga_title: r for r in releases}
    op = by_title["One Piece"]
    # Newest by date_added (op2 @ 2026-05-10), NOT op1.
    assert op.guid == "mangadot:m1:ch-1184:en:op2"
    assert _GUID_RE.match(op.guid), op.guid
    assert by_title["Naruto"].guid == "mangadot:m2:ch-700:en:nr1"
    # Opaque, non-composite handle.
    assert op.download_handle
    assert ":" not in op.download_handle
    # The minted ResolutionRecord.chapter_id is the {id}|{source} composite.
    record = await ctx.handle_store.resolve(op.download_handle)
    assert record is not None
    assert record.chapter_id == "op2|user"
    assert MangadotSource._parse_resolve_id(record.chapter_id) == ("op2", "user")


@pytest.mark.asyncio
async def test_recent_selects_newest_by_date_not_array_order() -> None:
    """REGRESSION lock for the KEY mangadot difference.

    mangadot's ``chapters/list`` is NOT newest-first (unlike atsumaru/mangafire), so
    recent() must pick the row with the latest parsed ``date_added`` — NOT ``rows[0]``.
    The array below is deliberately out of date order: the head is the OLDEST.
    """
    ctx = _ctx(
        manga_list=[_manga(manga_id="m1")],
        listings={
            "m1": [
                _row(
                    chapter_id="old",
                    chapter_number=1,
                    date_added="2026-01-01 00:00:00+00",
                ),
                _row(
                    chapter_id="new",
                    chapter_number=2,
                    date_added="2026-06-01 00:00:00+00",
                ),
                _row(
                    chapter_id="mid",
                    chapter_number=3,
                    date_added="2026-03-01 00:00:00+00",
                ),
            ]
        },
    )
    releases = await MangadotSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    assert len(releases) == 1
    # "new" (2026-06-01) is the latest date_added — NOT the array head "old".
    assert releases[0].guid == "mangadot:m1:ch-2:en:new"


@pytest.mark.asyncio
async def test_recent_seeds_feed_with_sortby_latest() -> None:
    ctx = _ctx(
        manga_list=[_manga(manga_id="m1")],
        listings={"m1": [_row(chapter_id="c1")]},
    )
    await MangadotSource().recent(languages=None, limit=10, since=None, ctx=ctx)
    # Exactly one /api/search call, seeding the feed with sortBy=latest + empty search.
    feed_calls = [c for c in ctx.json_calls if c[0] == _SEARCH]
    assert len(feed_calls) == 1
    _url, params = feed_calls[0]
    assert params["sortBy"] == "latest"
    assert params["search"] == ""
    assert params["page"] == 1


@pytest.mark.asyncio
async def test_recent_bounds_fanout_to_limit() -> None:
    manga_list = [_manga(manga_id=f"m{i}") for i in range(10)]
    listings = {f"m{i}": [_row(chapter_id=f"c{i}")] for i in range(10)}
    ctx = _ctx(manga_list=manga_list, listings=listings)
    releases = await MangadotSource().recent(
        languages=None, limit=3, since=None, ctx=ctx
    )
    # limit=3 → exactly 3 chapters/list calls + 3 releases.
    assert len(ctx.array_calls) == 3
    assert len(releases) == 3


@pytest.mark.asyncio
async def test_recent_title_cap() -> None:
    n = _RECENT_TITLE_CAP + 5
    manga_list = [_manga(manga_id=f"m{i}") for i in range(n)]
    listings = {f"m{i}": [_row(chapter_id=f"c{i}")] for i in range(n)}
    ctx = _ctx(manga_list=manga_list, listings=listings)
    # A large limit must still be clamped to the title cap.
    await MangadotSource().recent(languages=None, limit=1000, since=None, ctx=ctx)
    assert len(ctx.array_calls) == _RECENT_TITLE_CAP


@pytest.mark.asyncio
async def test_recent_non_english_returns_empty() -> None:
    ctx = _ctx(
        manga_list=[_manga(manga_id="m1")],
        listings={"m1": [_row(chapter_id="c1")]},
    )
    releases = await MangadotSource().recent(
        languages=["ja"], limit=10, since=None, ctx=ctx
    )
    assert releases == []
    # Short-circuits BEFORE any network call (no feed, no fan-out).
    assert ctx.json_calls == []
    assert ctx.array_calls == []


@pytest.mark.asyncio
async def test_recent_empty_feed_noop() -> None:
    ctx = _ctx(manga_list=[])
    releases = await MangadotSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    assert releases == []
    # The feed call fired; no candidate to enumerate → no chapters/list call.
    assert len(ctx.json_calls) == 1
    assert ctx.array_calls == []


@pytest.mark.asyncio
async def test_recent_skips_titles_with_no_chapters() -> None:
    ctx = _ctx(
        manga_list=[_manga(manga_id="m1"), _manga(manga_id="empty")],
        listings={"m1": [_row(chapter_id="c1", chapter_number=1)], "empty": []},
    )
    releases = await MangadotSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    # The empty title is dropped (no crash); the sibling still yields its release.
    assert len(releases) == 1
    assert releases[0].guid == "mangadot:m1:ch-1:en:c1"


def test_recent_fanout_concurrency_is_single_wave() -> None:
    """recent() must fan out wide enough to clear the title cap in ONE wave.

    Mirrors the atsumaru regression-lock: the source-wide
    ``_CHAPTERS_FANOUT_CONCURRENCY`` must be >= the title cap so a recent poll's
    per-title chapters/list fan-out completes in a single wave under fanout.py's 30s
    per-source budget.
    """
    assert _CHAPTERS_FANOUT_CONCURRENCY >= _RECENT_TITLE_CAP
