"""Unit tests for ``MangaFireSource.search`` — JSON /api/titles → chapter fan-out.

The LIVE flow (260706-hgu): ``GET /api/titles?keyword=`` returns title-only items
(the chapter-list key is ``hid``); ``search`` prunes candidates, deep-enumerates each
candidate's ``GET /api/titles/{hid}/chapters`` feed (paginating past the 200-cap for the
COMPLETE list), filters by ``chapter_matches``, slices to ``req.limit`` and ONLY THEN
mints (GAP-2 mint-after-slice). No browser, no vrf, no external-links stamping.

No network: the shared ``_FakeCtx`` (from ``test_mangafire_recent``) routes ``get_json``
for the titles + chapters endpoints and mints real handles.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangafire import MangaFireSource
from tests.test_mangafire_recent import _chapters_body, _FakeCtx, _titles_body

_GUID_RE = re.compile(r"^mangafire:[\w.-]+:ch-[\d.?]+:[a-z-]+:[\w.-]+$")


@pytest.mark.asyncio
async def test_search_mints_per_chapter_releases_with_numeric_handle() -> None:
    titles = [{"id": 50, "hid": "kw9", "title": "Blue Lock"}]
    chapter_pages = {
        "kw9": [
            _chapters_body(
                [{"id": 4736538, "number": "346.2", "createdAt": 1757308339}]
            )
        ]
    }
    ctx = _FakeCtx(titles_body=_titles_body(titles), chapter_pages=chapter_pages)
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock"),
        ctx,  # type: ignore[arg-type]
    )

    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.chapter_number == Decimal("346.2")
    # URL fragments are gone from the new API — the guid tail is the numeric id.
    assert "#" not in rel.guid
    assert rel.download_handle and ":" not in rel.download_handle
    assert rel.ids == {"mangafireHid": "kw9", "mangafireTitleId": "50"}
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    # The resolve unit is the numeric chapter id (the /api/chapters/{id} key).
    assert record.chapter_id == "4736538"
    # The search hit /api/titles with the keyword.
    assert any("/api/titles" in u for u in ctx.get_json_calls)


@pytest.mark.asyncio
async def test_search_gap2_mints_only_for_sliced_releases() -> None:
    chapters = [
        {"id": n, "number": str(n), "createdAt": n}
        for n in range(40, 0, -1)  # newest-first, 40 chapters
    ]
    ctx = _FakeCtx(
        titles_body=_titles_body([{"id": 1, "hid": "kw9", "title": "Blue Lock"}]),
        chapter_pages={"kw9": [_chapters_body(chapters)]},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock", limit=3),
        ctx,  # type: ignore[arg-type]
    )
    assert len(releases) == 3
    # GAP-2: a handle is minted ONLY for the post-slice survivors (store-cap safety).
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    # Newest-first slice: chapters 40, 39, 38 survive.
    assert [r.chapter_number for r in releases] == [
        Decimal("40"),
        Decimal("39"),
        Decimal("38"),
    ]


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    chapters = [{"id": n, "number": str(n), "createdAt": n} for n in range(5, 0, -1)]
    ctx = _FakeCtx(
        titles_body=_titles_body([{"id": 1, "hid": "kw9", "title": "Blue Lock"}]),
        chapter_pages={"kw9": [_chapters_body(chapters)]},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="chapter", query="blue lock", chapter=3.0),
        ctx,  # type: ignore[arg-type]
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}


@pytest.mark.asyncio
async def test_search_paginates_complete_chapter_list_past_200_cap() -> None:
    """A >200-chapter series has meta.lastPage>1; the COMPLETE list paginates every
    page (source-onboarding completeness rule) so an old chapter on page 2 is found."""
    # Page 1: newest 200 (numbers 250..51). Page 2: oldest 50 (numbers 50..1).
    page1 = [{"id": n, "number": str(n), "createdAt": n} for n in range(250, 50, -1)]
    page2 = [{"id": n, "number": str(n), "createdAt": n} for n in range(50, 0, -1)]
    ctx = _FakeCtx(
        titles_body=_titles_body([{"id": 1, "hid": "kw9", "title": "Blue Lock"}]),
        chapter_pages={
            "kw9": [
                _chapters_body(page1, page=1, last_page=2),
                _chapters_body(page2, page=2, last_page=2),
            ]
        },
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock", limit=1000),
        ctx,  # type: ignore[arg-type]
    )
    numbers = {r.chapter_number for r in releases}
    # All 250 chapters across both pages are present (completeness proven).
    assert len(releases) == 250
    assert Decimal("1") in numbers  # the oldest chapter lives on page 2
    assert Decimal("250") in numbers
    # Both pages were fetched (page 1 + the fanned-out page 2).
    chapter_gets = [u for u in ctx.get_json_calls if "/chapters" in u]
    assert len(chapter_gets) == 2


@pytest.mark.asyncio
async def test_search_empty_query_returns_no_releases() -> None:
    ctx = _FakeCtx(titles_body=_titles_body([]), chapter_pages={})
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query=""),
        ctx,  # type: ignore[arg-type]
    )
    assert releases == []
    assert ctx.get_json_calls == []
