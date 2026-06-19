"""Unit tests for ``WeebCentralSource.search`` — /search/data → full-chapter-list.

The LIVE flow is TWO HTML calls (live recon 2026-06-08):

1. ``GET /search/data`` → a TITLE-ONLY page, one ``<article class="bg-base-300 …">``
   per series. ``search`` prunes to ``_DEFAULT_TITLE_CANDIDATES`` candidates.
2. ``GET /series/{id}/full-chapter-list`` per candidate → the COMPLETE newest-first
   list, one ``<a href=".../chapters/{ULID}">`` + ``Chapter N`` + ``<time datetime>``
   per chapter (one Release each).

Each Release carries the guid ``weebcentral:{seriesId}:ch-{number}:{chapterId}``
(D-08), an opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is
the BARE chapter ULID (the manifest endpoint needs only it). Handles are minted AFTER
the per-candidate slice (mint-after-slice / store-cap safety).

No network: a fake ``SourceContext`` routes ``get_bytes`` by URL (search vs
full-chapter-list) and returns canned HTML byte fixtures built from the live shapes.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.weebcentral import WeebCentralSource

_GUID_RE = re.compile(r"^weebcentral:[0-9A-Za-z]{26}:ch-[\d.?]+:[0-9A-Za-z]{26}$")
_BASE = "https://weebcentral.com"
_SEARCH = f"{_BASE}/search/data"

# Valid 26-char Crockford-base32 ULIDs for fixtures.
_SERIES = "01J76XY7E9FNDZ1DBBM6PBJPFK"


def _series_id(n: int) -> str:
    """A deterministic distinct 26-char ULID-shaped id for fixture series n."""
    return f"01J76XY7E9FNDZ1DBBM6PBJP{n:02d}"


def _chapter_id(n: int) -> str:
    """A deterministic distinct 26-char ULID-shaped id for fixture chapter n."""
    return f"01KSTMTWZE2WHXAP7E18YE{n:04d}"


def _search_html(series: list[tuple[str, str]]) -> bytes:
    """Build a /search/data page: one bg-base-300 article per (series_id, title).

    Mirrors the live markup (a nested cover ``<article>`` + the series anchor + the
    cover ``<img alt="<Title> cover">``).
    """
    articles = "\n".join(
        f"""
        <article class="bg-base-300 flex gap-4 p-4">
          <a href="{_BASE}/series/{sid}/{title.replace(" ", "-")}">
            <article class="hidden lg:block">
              <picture>
                <img src="https://temp.compsci88.com/cover/fallback/{sid}.jpg"
                     alt="{title} cover" width="400" height="600">
              </picture>
            </article>
          </a>
        </article>
        """
        for sid, title in series
    )
    return f"<div>{articles}</div>".encode()


def _chapter_list_html(chapters: list[dict[str, Any]]) -> bytes:
    """Build a /full-chapter-list page from chapter dicts (newest-first order).

    Each dict: ``chapter_id`` (required), ``number`` (str/int/None), ``date`` (str).
    Mirrors the live anchor: ``Chapter N`` span text + a ``<time datetime>``.
    """
    rows = []
    for ch in chapters:
        cid = ch["chapter_id"]
        num = ch.get("number")
        label = f"Chapter {num}" if num is not None else "Oneshot"
        date = ch.get("date", "2026-05-29T19:54:30.766Z")
        rows.append(
            f"""
            <a href="{_BASE}/chapters/{cid}" class="flex-1 flex items-center p-2">
              <span class="grow flex items-center gap-2">
                <span class="">{label}</span>
              </span>
              <time class="text-datetime" datetime="{date}">{date}</time>
            </a>
            """
        )
    return f"<div>{''.join(rows)}</div>".encode()


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes ``get_bytes`` by URL (search/chapter-list)."""

    def __init__(
        self,
        *,
        search_html: bytes,
        listings: dict[str, bytes],
    ) -> None:
        self.handle_store = HandleStore()
        self._search_html = search_html
        self._listings = listings
        self.calls: list[str] = []
        self.candidates_enumerated: int | None = None

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("weebcentral", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("weebcentral", series_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        parsed = urlparse(url)
        path = parsed.path
        if path == "/search/data":
            return self._search_html
        m = re.match(r"^/series/([^/]+)/full-chapter-list$", path)
        if m is not None:
            return self._listings.get(m.group(1), b"<div></div>")
        raise AssertionError(f"unexpected get_bytes url: {url}")


def _ctx(
    *,
    series: list[tuple[str, str]],
    listings: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    listings = listings or {}
    return _FakeCtxForSearch(
        search_html=_search_html(series),
        listings={sid: _chapter_list_html(chs) for sid, chs in listings.items()},
    )


def _chapter(
    *, chapter_id: str, number: Any = 1184, date: str = "2026-05-29T19:54:30.766Z"
) -> dict[str, Any]:
    return {"chapter_id": chapter_id, "number": number, "date": date}


def _list_calls(ctx: Any) -> list[str]:
    return [u for u in ctx.calls if "/full-chapter-list" in u]


@pytest.mark.asyncio
async def test_search_one_listing_per_candidate() -> None:
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1))]},
    )
    await WeebCentralSource().search(
        SearchRequest(type="manga", query="one piece"), ctx
    )
    # One /search/data, then exactly one /full-chapter-list for the candidate.
    assert sum(1 for u in ctx.calls if urlparse(u).path == "/search/data") == 1
    assert len(_list_calls(ctx)) == 1
    assert f"/series/{_SERIES}/full-chapter-list" in ctx.calls[1]
    # The query rides the ``text`` param.
    assert "text=one+piece" in ctx.calls[0]


@pytest.mark.asyncio
async def test_search_caps_candidates_to_five() -> None:
    series = [(_series_id(i), f"Series {i}") for i in range(8)]
    listings = {_series_id(i): [_chapter(chapter_id=_chapter_id(i))] for i in range(8)}
    ctx = _ctx(series=series, listings=listings)
    await WeebCentralSource().search(SearchRequest(type="manga", query="series"), ctx)
    assert len(_list_calls(ctx)) == 5  # _DEFAULT_TITLE_CANDIDATES


@pytest.mark.asyncio
async def test_search_mints_bare_handle_and_guid() -> None:
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1), number=1184)]},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="one piece"), ctx
    )
    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == f"weebcentral:{_SERIES}:ch-1184:{_chapter_id(1)}"
    assert rel.download_handle
    assert ":" not in rel.download_handle  # opaque, not a structured composite
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    # The BARE chapter ULID — the manifest endpoint needs only it (NOT a composite).
    assert record.chapter_id == _chapter_id(1)
    assert record.source_key == "weebcentral"
    assert rel.language == "en"
    assert rel.chapter_number == Decimal("1184")
    assert rel.publish_date == "2026-05-29T19:54:30.766000+00:00"
    assert rel.ids == {
        "weebcentralSeriesId": _SERIES,
        "weebcentralChapterId": _chapter_id(1),
    }
    assert "(en)" in rel.title


@pytest.mark.asyncio
async def test_search_decimal_chapter_number_preserved() -> None:
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1), number="1184.5")]},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert releases[0].chapter_number == Decimal("1184.5")
    assert releases[0].guid == f"weebcentral:{_SERIES}:ch-1184.5:{_chapter_id(1)}"


@pytest.mark.asyncio
async def test_search_newest_first_slice_respects_limit() -> None:
    # Document order IS newest-first (live full-chapter-list); 5,4,3,2,1 top→bottom.
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in (5, 4, 3, 2, 1)]
    ctx = _ctx(series=[(_SERIES, "S")], listings={_SERIES: chapters})
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x", limit=2), ctx
    )
    assert len(releases) == 2
    assert [r.chapter_number for r in releases] == [Decimal("5"), Decimal("4")]


@pytest.mark.asyncio
async def test_search_mints_handles_only_for_returned_releases() -> None:
    """Mint-after-slice: a handle ONLY for the post-slice survivors (store-cap)."""
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in range(40, 0, -1)]
    ctx = _ctx(series=[(_SERIES, "S")], listings={_SERIES: chapters})
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x", limit=3), ctx
    )
    assert len(releases) == 3
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    for rel in releases:
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in (5, 4, 3, 2, 1)]
    ctx = _ctx(series=[(_SERIES, "S")], listings={_SERIES: chapters})
    releases = await WeebCentralSource().search(
        SearchRequest(type="chapter", query="x", chapter=3.0), ctx
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}


@pytest.mark.asyncio
async def test_search_empty_results_returns_no_releases() -> None:
    ctx = _ctx(series=[])
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="nothing"), ctx
    )
    assert releases == []
    # Only the /search/data call — no candidate to enumerate.
    assert len(_list_calls(ctx)) == 0


@pytest.mark.asyncio
async def test_search_non_english_language_filter_returns_empty() -> None:
    """Weeb Central is English-only: a non-en filter yields nothing (no calls)."""
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1))]},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x", languages=["ja"]), ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call
