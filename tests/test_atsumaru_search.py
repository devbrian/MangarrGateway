"""Unit tests for ``AtsumaruSource.search`` — Typesense → allChapters fan-out.

The LIVE flow is TWO calls (live recon 2026-06-08):

1. ``GET /collections/manga/documents/search`` (Typesense proxy) → a TITLE-ONLY
   ``{hits:[{document:{id,title,englishTitle,…}}]}`` envelope. ``search`` prunes to
   ``_DEFAULT_TITLE_CANDIDATES`` candidates.
2. ``GET /api/manga/allChapters?mangaId=`` per candidate → the COMPLETE newest-first
   ``{chapters:[{id,number,createdAt,index,pageCount}]}`` feed (one Release each).

Each Release carries the guid ``atsumaru:{mangaId}:ch-{number}:{chapterId}`` (D-08),
an opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is the
COMPOSITE ``{mangaId}:{chapterId}`` (read/chapter needs both), and ``page_count``
from ``pageCount``. Handles are minted AFTER the per-candidate slice (GAP-2).

No network: a fake ``SourceContext`` routes ``get_json`` by URL (Typesense vs
allChapters) and records calls for assertions.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.atsumaru import AtsumaruSource

_GUID_RE = re.compile(r"^atsumaru:[\w-]+:ch-[\d.]+:[\w-]+$")
_SEARCH = "https://atsu.moe/collections/manga/documents/search"
_ALLCHAPTERS = "https://atsu.moe/api/manga/allChapters"


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes ``get_json`` by URL (two-call flow)."""

    def __init__(
        self,
        *,
        hits: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.handle_store = HandleStore()
        self._hits = hits
        self._listings = listings
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.candidates_enumerated: int | None = None

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("atsumaru", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("atsumaru", series_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append((url, params))
        if url == _SEARCH:
            return {"found": len(self._hits), "hits": self._hits}
        if url == _ALLCHAPTERS:
            manga_id = str(params.get("mangaId"))
            return {"chapters": self._listings.get(manga_id, [])}
        raise AssertionError(f"unexpected get_json url: {url}")


def _ctx(
    *,
    hits: list[dict[str, Any]],
    listings: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    return _FakeCtxForSearch(hits=hits, listings=listings or {})


def _hit(
    *, manga_id: str, title: str = "One Piece", english: str = "ONE PIECE"
) -> dict[str, Any]:
    return {"document": {"id": manga_id, "title": title, "englishTitle": english}}


def _chapter(
    *, chapter_id: str, number: Any = 1184, index: int = 1184, pages: int = 13
) -> dict[str, Any]:
    return {
        "id": chapter_id,
        "scanlationMangaId": "cmgz",
        "title": f"Chapter {number}",
        "number": number,
        "createdAt": 1780084797472,
        "index": index,
        "pageCount": pages,
        "progress": None,
    }


def _allchapters_url_calls(ctx: Any) -> list[tuple[str, dict[str, Any]]]:
    return [c for c in ctx.calls if c[0] == _ALLCHAPTERS]


@pytest.mark.asyncio
async def test_search_typesense_then_one_listing_per_candidate() -> None:
    ctx = _ctx(
        hits=[_hit(manga_id="sVC2A")],
        listings={"sVC2A": [_chapter(chapter_id="gGfRS")]},
    )
    await AtsumaruSource().search(SearchRequest(type="manga", query="one piece"), ctx)

    assert len(ctx.calls) == 2
    url0, params0 = ctx.calls[0]
    assert url0 == _SEARCH
    assert params0["q"] == "one piece"
    assert params0["filter_by"] == "hidden:!=true"
    url1, params1 = ctx.calls[1]
    assert url1 == _ALLCHAPTERS
    assert params1["mangaId"] == "sVC2A"


@pytest.mark.asyncio
async def test_search_caps_candidates_to_five() -> None:
    hits = [_hit(manga_id=f"id{i}", title=f"Series {i}") for i in range(8)]
    listings = {f"id{i}": [_chapter(chapter_id=f"ch{i}")] for i in range(8)}
    ctx = _ctx(hits=hits, listings=listings)
    await AtsumaruSource().search(SearchRequest(type="manga", query="series"), ctx)
    assert len(_allchapters_url_calls(ctx)) == 5  # _DEFAULT_TITLE_CANDIDATES


@pytest.mark.asyncio
async def test_search_mints_composite_handle_and_guid() -> None:
    ctx = _ctx(
        hits=[_hit(manga_id="sVC2A")],
        listings={"sVC2A": [_chapter(chapter_id="gGfRS", number=1184, pages=13)]},
    )
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="one piece"), ctx
    )
    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == "atsumaru:sVC2A:ch-1184:gGfRS"
    assert rel.download_handle
    assert ":" not in rel.download_handle  # opaque, not a structured composite
    record = ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    # The COMPOSITE chapter_id carries both ids (read/chapter needs both).
    assert record.chapter_id == "sVC2A:gGfRS"
    assert record.source_key == "atsumaru"
    assert record.page_count == 13
    assert rel.page_count == 13
    assert rel.language == "en"
    assert rel.chapter_number == Decimal("1184")
    assert rel.publish_date == "2026-05-29T19:59:57.472000+00:00"
    assert rel.ids == {"atsumaruMangaId": "sVC2A", "atsumaruChapterId": "gGfRS"}


@pytest.mark.asyncio
async def test_search_decimal_chapter_number_preserved() -> None:
    ctx = _ctx(
        hits=[_hit(manga_id="sVC2A")],
        listings={"sVC2A": [_chapter(chapter_id="g", number=1184.5)]},
    )
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert releases[0].chapter_number == Decimal("1184.5")
    assert releases[0].guid == "atsumaru:sVC2A:ch-1184.5:g"


@pytest.mark.asyncio
async def test_search_newest_first_slice_respects_limit() -> None:
    chapters = [_chapter(chapter_id=f"c{n}", number=n, index=n) for n in range(1, 6)]
    ctx = _ctx(hits=[_hit(manga_id="m")], listings={"m": chapters})
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x", limit=2), ctx
    )
    assert len(releases) == 2
    # Newest-first by index: chapters 5 and 4 survive the slice.
    assert [r.chapter_number for r in releases] == [Decimal("5"), Decimal("4")]


@pytest.mark.asyncio
async def test_search_mints_handles_only_for_returned_releases() -> None:
    """GAP-2: mint a handle ONLY for the post-slice survivors (store-cap safety)."""
    chapters = [_chapter(chapter_id=f"c{n}", number=n, index=n) for n in range(1, 41)]
    ctx = _ctx(hits=[_hit(manga_id="m")], listings={"m": chapters})
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x", limit=3), ctx
    )
    assert len(releases) == 3
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    for rel in releases:
        assert ctx.handle_store.resolve(rel.download_handle) is not None


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    chapters = [_chapter(chapter_id=f"c{n}", number=n, index=n) for n in range(1, 6)]
    ctx = _ctx(hits=[_hit(manga_id="m")], listings={"m": chapters})
    releases = await AtsumaruSource().search(
        SearchRequest(type="chapter", query="x", chapter=3.0), ctx
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}


@pytest.mark.asyncio
async def test_search_skips_chapters_without_id() -> None:
    chapters = [_chapter(chapter_id="good"), {"number": 5, "index": 5}]  # 2nd has no id
    ctx = _ctx(hits=[_hit(manga_id="m")], listings={"m": chapters})
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert len(releases) == 1
    assert releases[0].guid.endswith(":good")


@pytest.mark.asyncio
async def test_search_empty_hits_returns_no_releases() -> None:
    ctx = _ctx(hits=[])
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="nothing"), ctx
    )
    assert releases == []
    assert len(ctx.calls) == 1  # only the Typesense call — no candidate to enumerate


@pytest.mark.asyncio
async def test_search_non_english_language_filter_returns_empty() -> None:
    """Atsumaru is English-only: a non-en language filter yields nothing (no calls)."""
    ctx = _ctx(hits=[_hit(manga_id="m")], listings={"m": [_chapter(chapter_id="c")]})
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x", languages=["ja"]), ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call
