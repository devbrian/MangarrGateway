"""Unit tests for ``KaganeSource.search`` — search/series → series-detail fan-out.

The LIVE flow is TWO calls (live recon 2026-06-09):

1. ``POST /api/v2/search/series?page=0&size=N`` (JSON body
   ``{"title": q, "content_rating": ["Safe","Suggestive"]}``) → a TITLE-ONLY
   ``{content:[{series_id, title, alternate_titles, translated_language, ...}]}``
   envelope. ``search`` prunes to ``_DEFAULT_TITLE_CANDIDATES`` candidates.
2. ``GET /api/v2/series/{series_id}`` per candidate → the COMPLETE
   ``{series_books:[{book_id, chapter_no, sort_no, page_count, groups}]}`` feed (one
   Release each).

Each Release carries the guid ``kagane:{series_id}:ch-{chapter_no}:{book_id}`` (D-08),
an opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is the bare
``book_id``, and ``page_count`` from the book row. Handles are minted AFTER the
per-candidate slice (GAP-2).

No network: a fake ``SourceContext`` routes ``post_json_body``/``get_json`` by URL and
records calls for assertions.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.kagane import KaganeSource

_GUID_RE = re.compile(r"^kagane:[\w-]+:ch-[\d.?]+:[\w-]+$")
_SEARCH = "https://yuzuki.kagane.to/api/v2/search/series"
_SERIES = "https://yuzuki.kagane.to/api/v2/series"


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes by URL (search/series vs series detail)."""

    def __init__(
        self,
        *,
        content: list[dict[str, Any]],
        details: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.handle_store = HandleStore()
        self._content = content
        self._details = details
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.candidates_enumerated: int | None = None

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("kagane", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("kagane", series_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def post_json_body(
        self,
        url: str,
        *,
        body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((url, {"body": body, "params": params}))
        if url == _SEARCH:
            return {"content": self._content}
        raise AssertionError(f"unexpected post_json_body url: {url}")

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append((url, params))
        prefix = f"{_SERIES}/"
        if url.startswith(prefix):
            series_id = url[len(prefix) :]
            return {"series_books": self._details.get(series_id, [])}
        raise AssertionError(f"unexpected get_json url: {url}")


def _ctx(
    *,
    content: list[dict[str, Any]],
    details: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    return _FakeCtxForSearch(content=content, details=details or {})


def _series(
    *, series_id: str, title: str = "Solo Leveling", lang: str = "en"
) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "title": title,
        "alternate_titles": [],
        "translated_language": lang,
    }


def _book(
    *, book_id: str, chapter_no: Any = 200, sort_no: float = 200.0, pages: int = 12
) -> dict[str, Any]:
    return {
        "book_id": book_id,
        "title": f"Episode {chapter_no}",
        "chapter_no": str(chapter_no),
        "volume_no": None,
        "sort_no": sort_no,
        "page_count": pages,
        "groups": [{"group_id": "g1", "title": "The Hours Between"}],
        "became_visible_at": "2026-03-07T14:27:20.534756Z",
        "created_at": "2026-03-07T14:27:20.534756Z",
    }


def _series_url_calls(ctx: Any) -> list[tuple[str, dict[str, Any]]]:
    return [c for c in ctx.calls if c[0].startswith(f"{_SERIES}/")]


@pytest.mark.asyncio
async def test_search_post_then_one_detail_per_candidate() -> None:
    ctx = _ctx(
        content=[_series(series_id="s1")],
        details={"s1": [_book(book_id="b1")]},
    )
    await KaganeSource().search(SearchRequest(type="manga", query="solo leveling"), ctx)
    urls = [u for u, _ in ctx.calls]
    assert urls.count(_SEARCH) == 1
    assert len(_series_url_calls(ctx)) == 1
    search_call = next(p for u, p in ctx.calls if u == _SEARCH)
    assert search_call["body"]["title"] == "solo leveling"
    assert search_call["body"]["content_rating"] == ["Safe", "Suggestive"]
    assert search_call["params"]["page"] == 0


@pytest.mark.asyncio
async def test_search_caps_candidates_to_five() -> None:
    content = [_series(series_id=f"s{i}", title=f"Series {i}") for i in range(8)]
    details = {f"s{i}": [_book(book_id=f"b{i}")] for i in range(8)}
    ctx = _ctx(content=content, details=details)
    await KaganeSource().search(SearchRequest(type="manga", query="series"), ctx)
    assert len(_series_url_calls(ctx)) == 5  # _DEFAULT_TITLE_CANDIDATES


@pytest.mark.asyncio
async def test_search_mints_book_id_handle_and_guid() -> None:
    ctx = _ctx(
        content=[_series(series_id="s1")],
        details={"s1": [_book(book_id="b1", chapter_no=200, pages=12)]},
    )
    releases = await KaganeSource().search(
        SearchRequest(type="manga", query="solo leveling"), ctx
    )
    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == "kagane:s1:ch-200:b1"
    assert rel.download_handle and ":" not in rel.download_handle
    record = ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == "b1"  # bare book_id (no composite)
    assert record.source_key == "kagane"
    assert record.page_count == 12
    assert rel.page_count == 12
    assert rel.language == "en"
    assert rel.chapter_number == Decimal("200")
    assert rel.publish_date == "2026-03-07T14:27:20.534756+00:00"
    assert rel.ids == {"kaganeSeriesId": "s1", "kaganeBookId": "b1"}
    assert rel.scanlation_group == "The Hours Between"
    assert "[The Hours Between]" in rel.title


@pytest.mark.asyncio
async def test_search_decimal_chapter_number_preserved() -> None:
    ctx = _ctx(
        content=[_series(series_id="s1")],
        details={"s1": [_book(book_id="b1", chapter_no="200.5", sort_no=200.5)]},
    )
    releases = await KaganeSource().search(SearchRequest(type="manga", query="x"), ctx)
    assert releases[0].chapter_number == Decimal("200.5")
    assert releases[0].guid == "kagane:s1:ch-200.5:b1"


@pytest.mark.asyncio
async def test_search_newest_first_slice_respects_limit() -> None:
    books = [
        _book(book_id=f"b{n}", chapter_no=n, sort_no=float(n)) for n in range(1, 6)
    ]
    ctx = _ctx(content=[_series(series_id="s1")], details={"s1": books})
    releases = await KaganeSource().search(
        SearchRequest(type="manga", query="x", limit=2), ctx
    )
    assert len(releases) == 2
    assert [r.chapter_number for r in releases] == [Decimal("5"), Decimal("4")]


@pytest.mark.asyncio
async def test_search_mints_handles_only_for_returned_releases() -> None:
    """GAP-2: mint a handle ONLY for the post-slice survivors (store-cap safety)."""
    books = [
        _book(book_id=f"b{n}", chapter_no=n, sort_no=float(n)) for n in range(1, 41)
    ]
    ctx = _ctx(content=[_series(series_id="s1")], details={"s1": books})
    releases = await KaganeSource().search(
        SearchRequest(type="manga", query="x", limit=3), ctx
    )
    assert len(releases) == 3
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001
    for rel in releases:
        assert ctx.handle_store.resolve(rel.download_handle) is not None


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    books = [
        _book(book_id=f"b{n}", chapter_no=n, sort_no=float(n)) for n in range(1, 6)
    ]
    ctx = _ctx(content=[_series(series_id="s1")], details={"s1": books})
    releases = await KaganeSource().search(
        SearchRequest(type="chapter", query="x", chapter=3.0), ctx
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}


@pytest.mark.asyncio
async def test_search_skips_books_without_id() -> None:
    books = [_book(book_id="good"), {"chapter_no": "5", "sort_no": 5.0}]
    ctx = _ctx(content=[_series(series_id="s1")], details={"s1": books})
    releases = await KaganeSource().search(SearchRequest(type="manga", query="x"), ctx)
    assert len(releases) == 1
    assert releases[0].guid.endswith(":good")


@pytest.mark.asyncio
async def test_search_empty_content_returns_no_releases() -> None:
    ctx = _ctx(content=[])
    releases = await KaganeSource().search(
        SearchRequest(type="manga", query="nothing"), ctx
    )
    assert releases == []
    assert len(ctx.calls) == 1  # only the search POST — no candidate to enumerate


@pytest.mark.asyncio
async def test_search_non_matching_language_filtered_out() -> None:
    """A series whose translated_language the caller excluded yields no fan-out."""
    ctx = _ctx(
        content=[_series(series_id="s1", lang="en")],
        details={"s1": [_book(book_id="b1")]},
    )
    releases = await KaganeSource().search(
        SearchRequest(type="manga", query="x", languages=["ja"]), ctx
    )
    assert releases == []
    assert len(_series_url_calls(ctx)) == 0  # filtered before the detail fan-out
