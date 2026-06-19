"""Unit tests for ``MangadotSource.search`` — two-call fan-out + mint-after-slice.

The LIVE flow is TWO calls:

1. ``GET /api/search?search={q}&sortBy=relevance&page=1`` → an object envelope
   ``{manga_list:[{id,title,…}],pagination}`` (TITLE-ONLY).
2. ``GET /api/manga/{id}/chapters/list`` per candidate → a BARE JSON ARRAY of
   chapter rows (consumed via ``ctx.get_json_array``).

Each Release carries the fully-specific guid
``mangadot:{manga_id}:ch-{number}:{language}:{chapter_id}`` (D-08), an opaque minted
``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is the bare chapter ``id``,
and a normalized RFC3339 ``publishDate``. Handles are minted AFTER sort+slice to
``limit`` (mangaball GAP-2 handle-eviction lesson).

No network: a fake ``SourceContext`` routes ``get_json`` (search envelope) vs
``get_json_array`` (chapters/list bare array) by URL and records calls.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangadot import MangadotSource

# guid contract (D-08): mangadot:{manga_id}:ch-{number}:{lang}:{chapter_id}
_GUID_RE = re.compile(r"^mangadot:[^:]+:ch-[\d.?]+:[a-z-]{2,}:[^:]+$")

_BASE = "https://mangadot.net"
_SEARCH = f"{_BASE}/api/search"


def _chapters_url(manga_id: str) -> str:
    return f"{_BASE}/api/manga/{manga_id}/chapters/list"


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes ``get_json`` vs ``get_json_array`` by URL.

    ``GET /api/search`` returns the title-only object envelope; ``GET
    /api/manga/{id}/chapters/list`` returns the bare chapter array keyed by the
    manga id parsed out of the URL. Calls are recorded for assertions.
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

    # Enum-cache seam (09-06): mirror the real SourceContext's default-None
    # pass-through — bare ``await fetch_fn()``, no caching — so these fan-out-count
    # tests exercise byte-for-byte the pre-cache behavior they were written for.
    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangadot", normalized_query, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangadot", series_id, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    def cache_replace(self, key: tuple[Any, ...], enum: Any) -> None:
        return None

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
    return _FakeCtxForSearch(manga_list=manga_list, listings=listings or {})


def _manga(*, manga_id: str, title: str = "Murim Psychopath") -> dict[str, Any]:
    """A TITLE-ONLY search hit — explicitly NO chapters embedded (RESEARCH §search)."""
    return {
        "id": manga_id,
        "title": title,
        "chapter_count": 0,  # unreliable in search (RESEARCH) — ignored by the source
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
    """One bare chapter-array row (RESEARCH §chapter list)."""
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
async def test_search_calls_search_then_one_array_per_candidate() -> None:
    ctx = _ctx(
        manga_list=[_manga(manga_id="20277")],
        listings={"20277": [_row(chapter_id="388872")]},
    )
    await MangadotSource().search(
        SearchRequest(type="manga", query="murim psychopath"), ctx
    )

    assert len(ctx.json_calls) == 1
    url0, params0 = ctx.json_calls[0]
    assert url0 == _SEARCH
    assert params0["search"] == "murim psychopath"
    assert params0["sortBy"] == "relevance"
    assert params0["page"] == 1
    assert ctx.array_calls == [_chapters_url("20277")]


@pytest.mark.asyncio
async def test_search_caps_candidates_to_five() -> None:
    manga_list = [_manga(manga_id=str(i)) for i in range(8)]
    listings = {str(i): [_row(chapter_id=f"c{i}")] for i in range(8)}
    ctx = _ctx(manga_list=manga_list, listings=listings)
    await MangadotSource().search(SearchRequest(type="manga", query="x"), ctx)

    assert len(ctx.array_calls) == 5  # _DEFAULT_MANGA_CANDIDATES


@pytest.mark.asyncio
async def test_search_interactive_does_not_change_candidate_count() -> None:
    manga_list = [_manga(manga_id=str(i)) for i in range(8)]
    listings = {str(i): [_row(chapter_id=f"c{i}")] for i in range(8)}

    ctx_a = _ctx(manga_list=manga_list, listings=listings)
    await MangadotSource().search(
        SearchRequest(type="manga", query="x", interactive=False), ctx_a
    )
    ctx_b = _ctx(manga_list=manga_list, listings=listings)
    await MangadotSource().search(
        SearchRequest(type="manga", query="x", interactive=True), ctx_b
    )
    assert len(ctx_a.array_calls) == len(ctx_b.array_calls) == 5


@pytest.mark.asyncio
async def test_search_mints_specific_guid_and_direct_chapter_id() -> None:
    ctx = _ctx(
        manga_list=[_manga(manga_id="20277")],
        listings={
            "20277": [
                _row(
                    chapter_id="388872",
                    chapter_number="26.0",
                    language="en",
                    comment_count=7,
                )
            ]
        },
    )
    releases = await MangadotSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )

    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == "mangadot:20277:ch-26:en:388872"
    # Opaque, non-composite handle.
    assert rel.download_handle
    assert ":" not in rel.download_handle
    # ResolutionRecord.chapter_id is the COMPOSITE {id}|{source} (debug
    # mangadot-resolve-404) — DIRECT, no :DEFERRED. The guid + ids above stay BARE
    # ("388872"); only the handle's resolve unit carries the packed source so
    # fetch_manifest can route to /api/uploads vs /api/chapters.
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == "388872|user"
    assert MangadotSource._parse_resolve_id(record.chapter_id) == ("388872", "user")
    assert record.source_key == "mangadot"
    assert record.page_count == 75
    assert rel.page_count == 75
    assert rel.publish_date == "2026-05-12T12:21:39+00:00"
    assert rel.language == "en"
    assert rel.scanlation_group == "Stick"
    # REL-03: per-chapter comment_count (unit = comments) wired to display-only
    # votes.
    assert rel.votes == 7
    assert rel.chapter_number == Decimal("26.0")
    assert rel.ids == {"mangaId": "20277", "mangadotChapterId": "388872"}


@pytest.mark.asyncio
async def test_search_comment_count_zero_and_absent_votes() -> None:
    # REL-03: default comment_count=0 → votes==0; a row omitting the key →
    # votes is None (_parse_int is None-safe).
    row_absent = _row(chapter_id="1", chapter_number="2.0")
    del row_absent["comment_count"]
    ctx = _ctx(
        manga_list=[_manga(manga_id="20277")],
        listings={
            "20277": [
                _row(chapter_id="388872", chapter_number="26.0"),  # default 0
                row_absent,
            ]
        },
    )
    releases = await MangadotSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    by_id = {r.ids["mangadotChapterId"]: r for r in releases}
    assert by_id["388872"].votes == 0
    assert by_id["1"].votes is None


@pytest.mark.asyncio
async def test_search_multi_language_rows_per_number_yield_multi_releases() -> None:
    """One chapter_number with TWO language/group rows → TWO distinct releases."""
    ctx = _ctx(
        manga_list=[_manga(manga_id="20277")],
        listings={
            "20277": [
                _row(chapter_id="aaa", chapter_number=7, language="en", group_name="A"),
                _row(chapter_id="bbb", chapter_number=7, language="es", group_name="B"),
            ]
        },
    )
    releases = await MangadotSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert len(releases) == 2
    assert {rel.language for rel in releases} == {"en", "es"}
    assert len({rel.guid for rel in releases}) == 2


@pytest.mark.asyncio
async def test_search_language_filter_drops_unrequested_languages() -> None:
    ctx = _ctx(
        manga_list=[_manga(manga_id="20277")],
        listings={
            "20277": [
                _row(chapter_id="aaa", language="en"),
                _row(chapter_id="bbb", language="es"),
            ]
        },
    )
    releases = await MangadotSource().search(
        SearchRequest(type="manga", query="x", languages=["es"]), ctx
    )
    assert len(releases) == 1
    assert releases[0].language == "es"


@pytest.mark.asyncio
async def test_search_orders_newest_first_and_slices_to_limit() -> None:
    rows = [
        _row(
            chapter_id=f"c{n}",
            chapter_number=n,
            date_added=f"2026-05-{n:02d} 00:00:00+00",
        )
        for n in range(1, 6)  # dates 2026-05-01 .. 2026-05-05
    ]
    ctx = _ctx(manga_list=[_manga(manga_id="20277")], listings={"20277": rows})
    releases = await MangadotSource().search(
        SearchRequest(type="manga", query="x", limit=2), ctx
    )
    assert len(releases) == 2
    assert releases[0].publish_date == "2026-05-05T00:00:00+00:00"
    assert releases[1].publish_date == "2026-05-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_search_mints_handles_only_for_returned_releases() -> None:
    """GAP-2 (mangaball): a handle is minted ONLY for the post-slice survivors.

    A long listing (N >> limit) must NOT mint a handle per row — that blows past
    ``HandleStore`` ``maxsize`` (default 200_000, GATEWAY_HANDLE_MAXSIZE) and evicts
    the returned releases' own
    handles, breaking ``POST /downloads``. Here a 40-row listing with ``limit=3``
    yields 3 releases AND mints exactly 3 handles, all of which still resolve.
    """
    rows = [
        _row(
            chapter_id=f"c{n}",
            chapter_number=n,
            date_added=f"2026-05-01 {n:02d}:00:00+00",
        )
        for n in range(1, 41)
    ]
    ctx = _ctx(manga_list=[_manga(manga_id="20277")], listings={"20277": rows})
    releases = await MangadotSource().search(
        SearchRequest(type="manga", query="x", limit=3), ctx
    )
    assert len(releases) == 3
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    for rel in releases:
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


@pytest.mark.asyncio
async def test_search_empty_results_returns_no_releases() -> None:
    ctx = _ctx(manga_list=[])
    releases = await MangadotSource().search(
        SearchRequest(type="manga", query="nothing"), ctx
    )
    assert releases == []
    # Only the search call fired — no candidate to enumerate.
    assert len(ctx.json_calls) == 1
    assert ctx.array_calls == []


@pytest.mark.asyncio
async def test_recent_is_noop() -> None:
    ctx = _ctx(manga_list=[])
    out = await MangadotSource().recent(languages=None, limit=50, since=None, ctx=ctx)
    assert out == []
    assert MangadotSource.supports_recent is False
