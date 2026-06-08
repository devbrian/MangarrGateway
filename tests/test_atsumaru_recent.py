"""Unit tests for ``AtsumaruSource.recent`` — title feed → newest-chapter mint.

The recent feed (``GET /api/infinite/recentlyUpdated``) is TITLE-ONLY (no embedded
chapter), so ``recent`` fans out a bounded ``allChapters`` per title (newest-first)
and mints the newest chapter as a DIRECT release — the chapter id is always present,
so no ``:DEFERRED`` late-bind. The minted handle carries the COMPOSITE
``{mangaId}:{chapterId}`` chapter id (read/chapter needs both).

No network: a fake ``SourceContext`` routes ``get_json`` by URL.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.atsumaru import AtsumaruSource, _ms_to_iso

_RECENT = "https://atsu.moe/api/infinite/recentlyUpdated"
_ALLCHAPTERS = "https://atsu.moe/api/manga/allChapters"


class _FakeCtxForRecent:
    def __init__(
        self,
        *,
        items: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.handle_store = HandleStore()
        self._items = items
        self._listings = listings
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append((url, params))
        if url == _RECENT:
            return {"items": self._items}
        if url == _ALLCHAPTERS:
            return {"chapters": self._listings.get(str(params.get("mangaId")), [])}
        raise AssertionError(f"unexpected get_json url: {url}")


def _item(*, manga_id: str, title: str = "One Piece") -> dict[str, Any]:
    return {"id": manga_id, "title": title, "type": "Manga"}


def _chapter(
    *, chapter_id: str, number: Any, created: int = 1780084797472
) -> dict[str, Any]:
    return {
        "id": chapter_id,
        "title": f"Chapter {number}",
        "number": number,
        "createdAt": created,
        "index": int(number),
        "pageCount": 13,
    }


@pytest.mark.asyncio
async def test_recent_mints_newest_chapter_per_title() -> None:
    ctx = _FakeCtxForRecent(
        items=[
            _item(manga_id="m1", title="One Piece"),
            _item(manga_id="m2", title="Naruto"),
        ],
        listings={
            # allChapters is newest-first: chapters[0] is the latest.
            "m1": [
                _chapter(chapter_id="op2", number=1184),
                _chapter(chapter_id="op1", number=1183),
            ],
            "m2": [_chapter(chapter_id="nr1", number=700)],
        },
    )
    releases = await AtsumaruSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    assert len(releases) == 2
    by_title = {r.manga_title: r for r in releases}
    assert by_title["One Piece"].guid == "atsumaru:m1:ch-1184:op2"  # newest, not op1
    assert by_title["Naruto"].guid == "atsumaru:m2:ch-700:nr1"
    # Composite chapter_id on the minted record.
    record = ctx.handle_store.resolve(by_title["One Piece"].download_handle)
    assert record is not None
    assert record.chapter_id == "m1:op2"
    assert by_title["One Piece"].publish_date.endswith("+00:00")


@pytest.mark.asyncio
async def test_recent_bounds_fanout_to_limit() -> None:
    items = [_item(manga_id=f"m{i}") for i in range(10)]
    listings = {f"m{i}": [_chapter(chapter_id=f"c{i}", number=i)] for i in range(10)}
    ctx = _FakeCtxForRecent(items=items, listings=listings)
    await AtsumaruSource().recent(languages=None, limit=3, since=None, ctx=ctx)
    allchapters_calls = [c for c in ctx.calls if c[0] == _ALLCHAPTERS]
    assert len(allchapters_calls) == 3  # bounded by limit


@pytest.mark.asyncio
async def test_recent_skips_titles_with_no_chapters() -> None:
    ctx = _FakeCtxForRecent(
        items=[_item(manga_id="m1"), _item(manga_id="empty")],
        listings={"m1": [_chapter(chapter_id="c1", number=1)], "empty": []},
    )
    releases = await AtsumaruSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    assert len(releases) == 1
    assert releases[0].guid == "atsumaru:m1:ch-1:c1"


@pytest.mark.asyncio
async def test_recent_non_english_language_returns_empty() -> None:
    ctx = _FakeCtxForRecent(items=[_item(manga_id="m1")], listings={"m1": []})
    releases = await AtsumaruSource().recent(
        languages=["ja"], limit=10, since=None, ctx=ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call


def test_ms_to_iso_valid_and_malformed() -> None:
    # A normal epoch-millis value → aware-UTC RFC3339.
    assert _ms_to_iso(1780084797472) == "2026-05-29T19:59:57.472000+00:00"
    assert _ms_to_iso("1780084797472").endswith("+00:00")
    # Missing / non-numeric → None (caller falls back).
    assert _ms_to_iso(None) is None
    assert _ms_to_iso("") is None
    assert _ms_to_iso("not-a-number") is None


@pytest.mark.parametrize("bad", [10**30, -(10**30), 10**400])
def test_ms_to_iso_out_of_range_falls_back(bad: int) -> None:
    # CodeRabbit #184: an absurd epoch must not raise OverflowError/OSError out of
    # _ms_to_iso and crash search/recent — it falls back to None.
    assert _ms_to_iso(bad) is None
