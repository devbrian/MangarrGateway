"""Unit tests for ``ProjectSukiSource.search`` — /search → /book fan-out.

The LIVE flow is TWO calls (live recon 2026-06-20):

1. ``GET /search?q=`` → TITLE-ONLY HTML (``/book/{id}`` anchors). ``search`` prunes to
   ``_DEFAULT_TITLE_CANDIDATES`` candidates.
2. ``GET /book/{id}`` per candidate → the COMPLETE chapter list (one Release each).

Each Release carries the guid ``projectsuki:{bookId}:ch-{number}:{chapterId}`` (D-08),
an opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is the
COMPOSITE ``{bookId}:{chapterId}`` (the manifest needs both). Handles are minted AFTER
the per-candidate slice (mint-after-slice).

No network: a fake ``SourceContext`` routes ``get_bytes`` by URL (search vs book) and
records calls for assertions.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.projectsuki import ProjectSukiSource

_GUID_RE = re.compile(r"^projectsuki:\d+:ch-[\d.?]+:[\w-]+$")
_SEARCH = "https://projectsuki.com/search"
_BOOK = "https://projectsuki.com/book"


def _search_html(books: list[tuple[str, str]]) -> str:
    rows = "".join(
        f'<h4><a class="inherit-color" href="/book/{bid}">{title}</a></h4>'
        for bid, title in books
    )
    return f"<html><body><div class='results'>{rows}</div></body></html>"


def _book_html(book_id: str, chapters: list[tuple[str, str]]) -> str:
    """``chapters`` = list of (chapter_id, number)."""
    rows = "".join(
        f'<tr class="row mx-0"><td class="col-5">'
        f'<a href="/read/{book_id}/{cid}/1" title="Sub">Chapter {num}</a></td>'
        f'<td><a href="/group/9">Asura Scans</a></td><td>English</td>'
        f"<td>2026-06-19</td></tr>"
        for cid, num in chapters
    )
    return f"<html><body><table>{rows}</table></body></html>"


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes ``get_bytes`` by URL (search vs book)."""

    def __init__(
        self,
        *,
        search_html: str,
        books: dict[str, str],
    ) -> None:
        self.handle_store = HandleStore()
        self._search_html = search_html
        self._books = books  # book_id -> book HTML
        self.calls: list[str] = []
        self.candidates_enumerated: int | None = None

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("projectsuki", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("projectsuki", series_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
        self.calls.append(url)
        if url.startswith(_SEARCH):
            return self._search_html.encode()
        if url.startswith(_BOOK + "/"):
            book_id = url.rsplit("/", 1)[-1]
            return self._books.get(book_id, "<html></html>").encode()
        raise AssertionError(f"unexpected get_bytes url: {url}")


def _book_calls(ctx: _FakeCtxForSearch) -> list[str]:
    return [u for u in ctx.calls if u.startswith(_BOOK + "/")]


@pytest.mark.asyncio
async def test_search_one_book_per_candidate() -> None:
    ctx = _FakeCtxForSearch(
        search_html=_search_html([("180716", "Solo Leveling")]),
        books={"180716": _book_html("180716", [("ch-1", "1")])},
    )
    await ProjectSukiSource().search(
        SearchRequest(type="manga", query="solo leveling"), ctx
    )
    # The search GET carries the query, then one /book GET per candidate.
    assert any(u.startswith(_SEARCH + "?") and "q=solo" in u for u in ctx.calls)
    assert _book_calls(ctx) == [f"{_BOOK}/180716"]


@pytest.mark.asyncio
async def test_search_caps_candidates_to_five() -> None:
    books = [(f"{i}", f"Series {i}") for i in range(8)]
    ctx = _FakeCtxForSearch(
        search_html=_search_html(books),
        books={bid: _book_html(bid, [("c", "1")]) for bid, _ in books},
    )
    await ProjectSukiSource().search(SearchRequest(type="manga", query="series"), ctx)
    assert len(_book_calls(ctx)) == 5  # _DEFAULT_TITLE_CANDIDATES


@pytest.mark.asyncio
async def test_search_mints_composite_handle_and_guid() -> None:
    ctx = _FakeCtxForSearch(
        search_html=_search_html([("180716", "Solo Leveling")]),
        books={"180716": _book_html("180716", [("ch-9", "9")])},
    )
    releases = await ProjectSukiSource().search(
        SearchRequest(type="manga", query="solo leveling"), ctx
    )
    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == "projectsuki:180716:ch-9:ch-9"
    assert rel.download_handle
    assert ":" not in rel.download_handle  # opaque, not a structured composite
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == "180716:ch-9"  # composite
    assert record.source_key == "projectsuki"
    assert rel.language == "en"
    assert rel.chapter_number == Decimal("9")
    assert rel.ids == {
        "projectsukiBookId": "180716",
        "projectsukiChapterId": "ch-9",
    }
    assert rel.scanlation_group == "Asura Scans"
    assert "[Asura Scans]" in rel.title


@pytest.mark.asyncio
async def test_search_decimal_chapter_preserved() -> None:
    ctx = _FakeCtxForSearch(
        search_html=_search_html([("1", "X")]),
        books={"1": _book_html("1", [("c", "1.5")])},
    )
    releases = await ProjectSukiSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert releases[0].chapter_number == Decimal("1.5")
    assert releases[0].guid == "projectsuki:1:ch-1.5:c"


@pytest.mark.asyncio
async def test_search_newest_first_slice_respects_limit() -> None:
    chapters = [(f"c{n}", str(n)) for n in range(1, 6)]
    ctx = _FakeCtxForSearch(
        search_html=_search_html([("1", "X")]),
        books={"1": _book_html("1", chapters)},
    )
    releases = await ProjectSukiSource().search(
        SearchRequest(type="manga", query="x", limit=2), ctx
    )
    assert len(releases) == 2
    # Newest-first by chapter number: 5 and 4 survive the slice.
    assert [r.chapter_number for r in releases] == [Decimal("5"), Decimal("4")]


@pytest.mark.asyncio
async def test_search_mints_handles_only_for_returned_releases() -> None:
    """Mint-after-slice: a handle ONLY for the post-slice survivors (store-cap)."""
    chapters = [(f"c{n}", str(n)) for n in range(1, 41)]
    ctx = _FakeCtxForSearch(
        search_html=_search_html([("1", "X")]),
        books={"1": _book_html("1", chapters)},
    )
    releases = await ProjectSukiSource().search(
        SearchRequest(type="manga", query="x", limit=3), ctx
    )
    assert len(releases) == 3
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    for rel in releases:
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    chapters = [(f"c{n}", str(n)) for n in range(1, 6)]
    ctx = _FakeCtxForSearch(
        search_html=_search_html([("1", "X")]),
        books={"1": _book_html("1", chapters)},
    )
    releases = await ProjectSukiSource().search(
        SearchRequest(type="chapter", query="x", chapter=3.0), ctx
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}


@pytest.mark.asyncio
async def test_search_empty_results_returns_no_releases() -> None:
    ctx = _FakeCtxForSearch(search_html=_search_html([]), books={})
    releases = await ProjectSukiSource().search(
        SearchRequest(type="manga", query="nothing"), ctx
    )
    assert releases == []
    assert _book_calls(ctx) == []  # no candidate to enumerate


@pytest.mark.asyncio
async def test_search_non_english_language_filter_returns_empty() -> None:
    ctx = _FakeCtxForSearch(
        search_html=_search_html([("1", "X")]),
        books={"1": _book_html("1", [("c", "1")])},
    )
    releases = await ProjectSukiSource().search(
        SearchRequest(type="manga", query="x", languages=["ja"]), ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call
