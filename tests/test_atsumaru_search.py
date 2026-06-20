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

from manga_gateway.framework.errors import SourceError
from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.atsumaru import AtsumaruSource

_GUID_RE = re.compile(r"^atsumaru:[\w-]+:ch-[\d.]+:[\w-]+$")
_SEARCH = "https://atsu.moe/collections/manga/documents/search"
_ALLCHAPTERS = "https://atsu.moe/api/manga/allChapters"
_MANGAPAGE = "https://atsu.moe/api/manga/page"
# Default scanlators map — the _chapter() fixture's scanlationMangaId is "cmgz".
_DEFAULT_SCANLATORS = [{"id": "cmgz", "name": "Alpha"}]


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes ``get_json`` by URL (search/chapters/page)."""

    def __init__(
        self,
        *,
        hits: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
        scanlators: dict[str, list[dict[str, str]]] | None = None,
        links: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.handle_store = HandleStore()
        self._hits = hits
        self._listings = listings
        self._scanlators = scanlators or {}
        # Per-manga raw tracker-link fields to merge onto the mangaPage body (the same
        # body that already carries the scanlators map — Pitfall 5, zero added HTTP).
        self._links = links or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.candidates_enumerated: int | None = None
        # 13-02 external-links seam: per-request scratch stash + a pass-through wrapper
        # (the real wrapper's resolve-once cache + timeout/swallow are tested in 13-02).
        self.external_links_raw: dict[str, dict[str, Any]] = {}

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

    async def resolve_external_links(self, series_id: str, parse_fn: Any) -> Any:
        return await parse_fn()

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append((url, params))
        if url == _SEARCH:
            return {"found": len(self._hits), "hits": self._hits}
        if url == _ALLCHAPTERS:
            manga_id = str(params.get("mangaId"))
            return {"chapters": self._listings.get(manga_id, [])}
        if url == _MANGAPAGE:
            manga_id = str(params.get("id"))
            # The mangaPage body carries BOTH the scanlators map AND the tracker-link
            # fields on the same object (links merged in alongside scanlators).
            page: dict[str, Any] = {
                "scanlators": self._scanlators.get(manga_id, _DEFAULT_SCANLATORS)
            }
            page.update(self._links.get(manga_id, {}))
            return {"mangaPage": page}
        raise AssertionError(f"unexpected get_json url: {url}")


def _ctx(
    *,
    hits: list[dict[str, Any]],
    listings: dict[str, list[dict[str, Any]]] | None = None,
    scanlators: dict[str, list[dict[str, str]]] | None = None,
    links: dict[str, dict[str, Any]] | None = None,
) -> Any:
    return _FakeCtxForSearch(
        hits=hits, listings=listings or {}, scanlators=scanlators, links=links
    )


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

    urls = [u for u, _ in ctx.calls]
    # Typesense search, then per-candidate allChapters + manga.page (scanlators).
    assert urls.count(_SEARCH) == 1
    assert urls.count(_ALLCHAPTERS) == 1
    assert urls.count(_MANGAPAGE) == 1
    search_params = next(p for u, p in ctx.calls if u == _SEARCH)
    assert search_params["q"] == "one piece"
    assert search_params["filter_by"] == "hidden:!=true"
    assert next(p for u, p in ctx.calls if u == _ALLCHAPTERS)["mangaId"] == "sVC2A"
    assert next(p for u, p in ctx.calls if u == _MANGAPAGE)["id"] == "sVC2A"


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
    record = await ctx.handle_store.resolve(rel.download_handle)
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
    # Scanlation group resolved from manga.page scanlators (scanlationMangaId cmgz →
    # "Alpha"): on the release, the minted record, AND the parseable title.
    assert rel.scanlation_group == "Alpha"
    assert record.scanlation_group == "Alpha"
    assert "[Alpha]" in rel.title


@pytest.mark.asyncio
async def test_search_resolves_distinct_groups_per_scanlation() -> None:
    """A manga with two scanlations → each chapter carries its own group name."""
    chapters = [
        {**_chapter(chapter_id="a", number=200, index=200), "scanlationMangaId": "g1"},
        {**_chapter(chapter_id="b", number=199, index=199), "scanlationMangaId": "g2"},
    ]
    ctx = _ctx(
        hits=[_hit(manga_id="m")],
        listings={"m": chapters},
        scanlators={
            "m": [{"id": "g1", "name": "Alpha"}, {"id": "g2", "name": "Asura"}]
        },
    )
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert {r.scanlation_group for r in releases} == {"Alpha", "Asura"}


@pytest.mark.asyncio
async def test_search_group_absent_degrades_to_none() -> None:
    """An unknown scanlationMangaId (no scanlators match) → scanlation_group None."""
    ctx = _ctx(
        hits=[_hit(manga_id="m")],
        listings={"m": [_chapter(chapter_id="c")]},  # scanlationMangaId "cmgz"
        scanlators={"m": [{"id": "OTHER", "name": "Nope"}]},
    )
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert releases[0].scanlation_group is None
    assert "[" not in releases[0].title  # no group bracket


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
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


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


class _PageCtx:
    """Minimal ctx for _fetch_scanlator_names: get_json returns canned/raises."""

    def __init__(self, behavior: Any) -> None:
        self._behavior = behavior

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        if self._behavior == "raise":
            raise SourceError("source_unavailable", "boom")
        return self._behavior


@pytest.mark.asyncio
async def test_fetch_page_meta_happy_and_best_effort() -> None:
    src = AtsumaruSource()
    names, links = await src._fetch_page_meta(
        "m",
        _PageCtx(  # type: ignore[arg-type]
            {
                "mangaPage": {
                    "scanlators": [
                        {"id": "a", "name": "Alpha"},
                        {"id": "b", "name": "Asura"},
                    ],
                    "anilistId": "105398",
                    "kenmeiUrl": "https://www.kenmei.co/series/solo-leveling",
                }
            }
        ),
    )
    assert names == {"a": "Alpha", "b": "Asura"}
    # The raw tracker fields are lifted off the SAME mangaPage body, verbatim.
    assert links == {
        "anilistId": "105398",
        "kenmeiUrl": "https://www.kenmei.co/series/solo-leveling",
    }
    # A fetch error is swallowed → ({}, {}) (both advisory, never fail enumeration).
    assert await src._fetch_page_meta("m", _PageCtx("raise")) == ({}, {})  # type: ignore[arg-type]
    # Malformed shapes all degrade to ({}, {}) — incl. a non-dict body (CodeRabbit #185).
    for bad in (
        {},
        {"mangaPage": {}},
        {"mangaPage": {"scanlators": None}},
        {"mangaPage": {"scanlators": [{"id": "x"}]}},  # missing name
        [{"id": "a", "name": "X"}],  # non-dict (list) body
        "oops",  # non-dict (str) body
    ):
        assert await src._fetch_page_meta("m", _PageCtx(bad)) == ({}, {})  # type: ignore[arg-type]


# ─────────────────────────── external tracker links (R2/R4/R5) ──────────────────
# The mangaPage body fetched for the scanlator map ALSO carries the 8 Atsumaru tracker
# fields (Pitfall 5); the hook parses them with ZERO added HTTP and stamps one
# ExternalLinks object onto every release of the series.

# Live-shape raw fields (13-RESEARCH frozen table): all bare except kenmeiUrl (a URL).
_ATSU_RAW_LINKS = {
    "anilistId": "105398",
    "malId": "121496",
    "mangaUpdatesId": "6z1uqw7",
    "mangaBakaId": "3397",
    "kitsuId": "54114",
    "apId": "solo-leveling",
    "annId": "32926",
    "kenmeiUrl": "https://www.kenmei.co/series/solo-leveling",
}
_ATSU_EXPECTED = {
    "anilist": "105398",
    "myAnimeList": "121496",
    "mangaUpdates": "6z1uqw7",
    "mangaBaka": "3397",
    "kitsu": "54114",
    "animePlanet": "solo-leveling",
    "ann": "32926",
    "kenmei": "solo-leveling",  # slug extracted from the full kenmeiUrl (R2)
}


@pytest.mark.asyncio
async def test_search_emits_external_links_from_mangapage() -> None:
    ctx = _ctx(
        hits=[_hit(manga_id="m")],
        listings={"m": [_chapter(chapter_id="c")]},
        links={"m": _ATSU_RAW_LINKS},
    )
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert len(releases) == 1
    links = releases[0].external_links
    assert links is not None
    assert links.model_dump() == _ATSU_EXPECTED


@pytest.mark.asyncio
async def test_search_external_links_zero_added_http() -> None:
    ctx = _ctx(
        hits=[_hit(manga_id="m")],
        listings={"m": [_chapter(chapter_id="c")]},
        links={"m": _ATSU_RAW_LINKS},
    )
    await AtsumaruSource().search(SearchRequest(type="manga", query="x"), ctx)
    # Links ride the existing scanlator-map fetch — exactly ONE /api/manga/page (R5).
    assert [u for u, _ in ctx.calls].count(_MANGAPAGE) == 1


@pytest.mark.asyncio
async def test_search_external_links_identical_object_across_releases() -> None:
    chapters = [_chapter(chapter_id=f"c{n}", number=n, index=n) for n in range(1, 4)]
    ctx = _ctx(
        hits=[_hit(manga_id="m")],
        listings={"m": chapters},
        links={"m": _ATSU_RAW_LINKS},
    )
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert len(releases) == 3
    first = releases[0].external_links
    assert first is not None
    for rel in releases:
        assert rel.external_links is first  # resolve-once + stamp (R4)


@pytest.mark.asyncio
async def test_search_external_links_none_when_mangapage_has_no_trackers() -> None:
    # mangaPage carries the scanlators map but NO tracker fields → externalLinks None.
    ctx = _ctx(
        hits=[_hit(manga_id="m")],
        listings={"m": [_chapter(chapter_id="c")]},
    )
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert len(releases) == 1
    assert releases[0].external_links is None


@pytest.mark.asyncio
async def test_search_non_english_language_filter_returns_empty() -> None:
    """Atsumaru is English-only: a non-en language filter yields nothing (no calls)."""
    ctx = _ctx(hits=[_hit(manga_id="m")], listings={"m": [_chapter(chapter_id="c")]})
    releases = await AtsumaruSource().search(
        SearchRequest(type="manga", query="x", languages=["ja"]), ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call
