"""Unit tests for ``MangaFireSource.recent`` — JSON /api/titles → DIRECT newest chapter.

``GET /api/titles?order[chapter_updated_at]=desc&limit=`` returns title-only items;
``recent`` fans out the per-title chapter list (``GET /api/titles/{hid}/chapters``)
under a bounded semaphore and mints the NEWEST chapter DIRECT. The chapter numeric id
is always present, so there is no ``:DEFERRED``.

No network: a fake ``SourceContext`` routes ``get_json`` by URL and mints real handles.
The ``_titles_body`` / ``_chapters_body`` builders here are shared by the search tests.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangafire import MangaFireSource

_GUID_RE = re.compile(r"^mangafire:[\w.-]+:ch-[\d.?]+:[a-z-]+:[\w.-]+$")


def _titles_body(titles: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``/api/titles`` response: ``{items:[{id,hid,title,…}], meta}``."""
    return {"items": titles, "meta": {"total": len(titles), "page": 1, "lastPage": 1}}


def _chapters_body(
    chapters: list[dict[str, Any]], *, page: int = 1, last_page: int = 1
) -> dict[str, Any]:
    """A ``/api/titles/{hid}/chapters`` response page (newest-first items)."""
    return {
        "items": chapters,
        "meta": {"total": len(chapters), "page": page, "lastPage": last_page},
    }


class _FakeCtx:
    """Routes ``get_json`` for the titles + chapters endpoints from canned bodies.

    ``chapter_pages`` maps ``hid`` → an ordered list of page bodies (one per page
    number, 1-indexed); a single-page series is a one-element list. ``titles_body``
    answers the ``/api/titles`` call. ``chapters_body`` answers ``/api/chapters/{id}``.
    """

    def __init__(
        self,
        *,
        titles_body: dict[str, Any],
        chapter_pages: dict[str, list[dict[str, Any]]],
        chapters_body: dict[str, Any] | None = None,
    ) -> None:
        self.handle_store = HandleStore()
        self._titles_body = titles_body
        self._chapter_pages = chapter_pages
        self._chapters_body = chapters_body or {}
        self.get_json_calls: list[str] = []
        # (hid, page) for every /api/titles/{hid}/chapters GET — lets tests assert
        # recent never paginates past page 1 per title.
        self.chapter_page_gets: list[tuple[str, int]] = []
        self.candidates_enumerated: int | None = None
        self.expected_pages: int | None = None

    async def cached_resolve(self, key: Any, fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: Any, fetch_fn: Any) -> Any:
        return await fetch_fn()

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("mangafire", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, hid: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("mangafire", hid, tuple(sorted(languages)))

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.get_json_calls.append(url)
        m = re.search(r"/api/titles/([^/]+)/chapters", url)
        if m:
            hid = m.group(1)
            pages = self._chapter_pages.get(hid, [_chapters_body([])])
            page = int(params.get("page", 1))
            self.chapter_page_gets.append((hid, page))
            return pages[min(page - 1, len(pages) - 1)]
        if "/api/chapters/" in url:
            return self._chapters_body
        if "/api/titles" in url:
            return self._titles_body
        raise AssertionError(f"unexpected get_json url {url!r}")


@pytest.mark.asyncio
async def test_recent_mints_direct_newest_chapter_per_title() -> None:
    titles = [
        {"id": 1, "hid": "kw9", "title": "Blue Lock"},
        {"id": 2, "hid": "abc", "title": "One Piece"},
    ]
    chapter_pages = {
        "kw9": [
            _chapters_body(
                [
                    {"id": 5002, "number": "346.2", "createdAt": 1757308339},
                    {"id": 5001, "number": "346.1", "createdAt": 1757208339},
                ]
            )
        ],
        "abc": [_chapters_body([{"id": 9001, "number": "1120", "createdAt": 1}])],
    }
    ctx = _FakeCtx(titles_body=_titles_body(titles), chapter_pages=chapter_pages)
    releases = await MangaFireSource().recent(
        languages=None,
        limit=50,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
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
        record = await ctx.handle_store.resolve(rel.download_handle)
        assert record is not None
        # The chapter_id is the numeric chapter id (the resolve unit).
        assert record.chapter_id.isdigit()
        assert rel.language == "en"
    # The recent feed used the required order[...] bracket param.
    assert any("order%5Bchapter_updated_at%5D=desc" in u for u in ctx.get_json_calls)


@pytest.mark.asyncio
async def test_recent_skips_title_with_empty_chapter_list() -> None:
    titles = [
        {"id": 1, "hid": "kw9", "title": "Blue Lock"},
        {"id": 2, "hid": "zzz", "title": "Empty Title"},
    ]
    chapter_pages = {
        "kw9": [_chapters_body([{"id": 1, "number": "1", "createdAt": 1}])],
        "zzz": [_chapters_body([])],  # no chapters → skipped, never crashes the poll
    }
    ctx = _FakeCtx(titles_body=_titles_body(titles), chapter_pages=chapter_pages)
    releases = await MangaFireSource().recent(
        languages=["en"],
        limit=50,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
    )
    assert [r.manga_title for r in releases] == ["Blue Lock"]


@pytest.mark.asyncio
async def test_recent_bounds_fanout_by_limit() -> None:
    titles = [{"id": i, "hid": f"h{i}", "title": f"Title {i}"} for i in range(5)]
    chapter_pages = {
        f"h{i}": [_chapters_body([{"id": 100 + i, "number": "1", "createdAt": 1}])]
        for i in range(5)
    }
    ctx = _FakeCtx(titles_body=_titles_body(titles), chapter_pages=chapter_pages)
    releases = await MangaFireSource().recent(
        languages=None,
        limit=2,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
    )
    # limit=2 bounds the per-title fan-out → at most 2 chapter-list GETs + 2 releases.
    assert len(releases) == 2
    chapter_gets = [u for u in ctx.get_json_calls if "/chapters" in u]
    assert len(chapter_gets) == 2


@pytest.mark.asyncio
async def test_recent_fetches_only_page_1_per_title() -> None:
    """Recent must read ONLY page 1 per title — never paginate the full history.

    The newest chapter is page-1 row 0 (newest-first), so even a multi-page series
    (``lastPage=3`` here) must produce exactly one chapters GET, at ``page=1``.
    """
    titles = [{"id": 50, "hid": "l33", "title": "Naruto"}]
    # A 3-page series: if recent paginated (like search's _chapter_list) it would GET
    # pages 1, 2 AND 3. Page 1's row 0 (number 700) is the newest and all recent needs.
    chapter_pages = {
        "l33": [
            _chapters_body(
                [{"id": 4736538, "number": "700", "createdAt": 1757308339}],
                page=1,
                last_page=3,
            ),
            _chapters_body(
                [{"id": 111, "number": "400", "createdAt": 2}], page=2, last_page=3
            ),
            _chapters_body(
                [{"id": 222, "number": "1", "createdAt": 3}], page=3, last_page=3
            ),
        ]
    }
    ctx = _FakeCtx(titles_body=_titles_body(titles), chapter_pages=chapter_pages)
    releases = await MangaFireSource().recent(
        languages=None,
        limit=10,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
    )
    # Exactly one chapters GET, and it was page 1 — pages 2/3 never fetched.
    assert ctx.chapter_page_gets == [("l33", 1)]
    # The minted release is the newest chapter (page-1 row 0), not a later page.
    assert len(releases) == 1
    assert releases[0].chapter_number == Decimal("700")
