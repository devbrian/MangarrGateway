"""Plan 09-04 proof: MangaDex opted into both enum-cache layers (CACHE-02..06).

Four respx call-count assertions on the reference source, mirroring the four phase
success criteria:

* **Test A** (criterion 1) — a type=manga search then a same-series type=chapter
  search through ONE ``SourceContext`` + ``EnumerationCache`` issues ZERO upstream
  ``/manga`` and ``/chapter`` calls on the second search (both layers HIT).
* **Test B** (criterion 2) — a type=manga search PAGINATE-ALL walks the full chapter
  feed across multiple ``/chapter`` pages (page size ``_MAX_FEED_LIMIT=100``) and
  caches the COMPLETE feed. The obsolete completeness-refetch path is gone (CR-01):
  the walk handles exhaustion internally, so a deep chapter is never truncated and
  never needs a deeper refetch.
* **Test C** (criterion 4) — two ``recent()`` calls each hit ``/chapter``; recent is
  never cached (CACHE-05).
* **Test D** (kill-switch) — ``EnumerationCache(enabled=False)`` restores the
  byte-for-byte pre-Phase-9 upstream behavior: the repeat chapter search re-issues
  both calls (D-08).

All network-free (respx) and fast. The cache check is BEFORE the rate limiter (it
lives inside the fetch closure), so a HIT costs no upstream call and no token.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx

from manga_gateway.config import Settings
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.enum_cache import EnumerationCache
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.framework.transport import HttpxTransport
from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangadex import MangaDexSource

MANGADEX = "https://api.mangadex.org"
TEST_API_KEY = "test-key-deterministic-0123456789"


def _build_ctx(cache: EnumerationCache) -> tuple[SourceContext, HttpxTransport]:
    """A real-transport SourceContext (respx intercepts httpx) + the cache seam.

    Returns the transport too so the test can ``aclose`` the shared client. A high
    rate limit keeps the per-source limiter from slowing the deterministic run.
    """
    transport = HttpxTransport(Settings(api_key=TEST_API_KEY))
    ctx = SourceContext(
        source_key="mangadex",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        enum_cache=cache,
    )
    return ctx, transport


def _manga_search_payload(manga_id: str) -> dict:
    return {
        "result": "ok",
        "response": "collection",
        "data": [
            {
                "id": manga_id,
                "type": "manga",
                "attributes": {
                    "title": {"en": "Solo Leveling"},
                    "altTitles": [],
                    "availableTranslatedLanguages": ["en"],
                },
                "relationships": [],
            }
        ],
        "total": 1,
        "limit": 1,
        "offset": 0,
    }


def _chapter_feed_payload(manga_id: str, chapters: list[str]) -> dict:
    def _ch(chapter: str) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "type": "chapter",
            "attributes": {
                "volume": None,
                "chapter": chapter,
                "title": None,
                "translatedLanguage": "en",
                "externalUrl": None,
                "isUnavailable": False,
                "publishAt": "2026-05-29T13:57:18+00:00",
                "readableAt": "2026-05-29T13:57:18+00:00",
                "pages": 2,
            },
            "relationships": [
                {"id": "grp", "type": "scanlation_group", "attributes": {"name": "G"}},
                {
                    "id": manga_id,
                    "type": "manga",
                    "attributes": {"title": {"en": "Solo Leveling"}},
                },
            ],
        }

    data = [_ch(c) for c in chapters]
    return {
        "result": "ok",
        "response": "collection",
        "data": data,
        "total": len(data),
        "limit": 100,
        "offset": 0,
    }


# ──────────────── Test A: zero upstream calls on the second search ─────────────────


@respx.mock
@pytest.mark.asyncio
async def test_second_same_series_chapter_search_zero_upstream_calls() -> None:
    """criterion 1: manga search then same-series chapter search → 0 calls #2."""
    manga_id = str(uuid.uuid4())
    manga_route = respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200, json=_chapter_feed_payload(manga_id, ["10", "11", "12"])
        )
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        first = await src.search(
            SearchRequest(type="manga", query="Solo Leveling"), ctx
        )
        assert first  # the first search populated both layers
        manga_after_first = manga_route.call_count
        chapter_after_first = chapter_route.call_count

        second = await src.search(
            SearchRequest(type="chapter", query="Solo Leveling"), ctx
        )
        assert second  # served entirely from the cache

        # The headline win: the second same-series search touches neither route.
        assert manga_route.call_count - manga_after_first == 0
        assert chapter_route.call_count - chapter_after_first == 0
    finally:
        await transport.aclose()


# ──────────── Test B: paginate-all walk spans multiple feed pages, no refetch ───────


@respx.mock
@pytest.mark.asyncio
async def test_paginate_all_walk_spans_multiple_feed_pages() -> None:
    """criterion 2: a type=manga search walks the full feed across pages at limit=100.

    The completeness-refetch path is gone (CR-01): the walk pages through the whole
    feed itself (page size ``_MAX_FEED_LIMIT``=100, advancing ``offset``), caches the
    COMPLETE feed, and never truncates — so no deeper refetch is ever needed.
    """
    manga_id = str(uuid.uuid4())
    respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    seen: list[tuple[str | None, str | None]] = []

    def _chapter_responder(request: httpx.Request) -> httpx.Response:
        limit = request.url.params.get("limit")
        offset = request.url.params.get("offset")
        seen.append((limit, offset))
        if offset == "0":
            # a FULL page (100 rows) → the walker must fetch a second page.
            payload = _chapter_feed_payload(manga_id, [str(n) for n in range(1, 101)])
        else:
            # a SHORT second page (50 rows) → exhaustion, the walk stops.
            payload = _chapter_feed_payload(manga_id, [str(n) for n in range(101, 151)])
        payload["total"] = 150
        return httpx.Response(200, json=payload)

    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        side_effect=_chapter_responder
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        result = await src.search(
            SearchRequest(type="manga", query="Solo Leveling"), ctx
        )
        # The walk paged TWICE — every page at the _MAX_FEED_LIMIT=100 ceiling,
        # advancing the offset cursor — then stopped on the short page. No refetch.
        assert seen == [("100", "0"), ("100", "100")]
        assert chapter_route.call_count == 2
        # The cached enumeration holds the COMPLETE 150-chapter feed (mangadex does
        # NOT pre-truncate to req.limit — the route does that, search.py:218).
        assert len(result) == 150
    finally:
        await transport.aclose()


# ── window-agnostic Layer-2 cache: same-series searches share ONE cached feed ──────


@respx.mock
@pytest.mark.asyncio
async def test_offset_agnostic_source_feed_still_hits_cache() -> None:
    """260620-ki0: ``offset`` is a ROUTE concern — the source returns the
    window-agnostic FULL cached feed regardless of offset, and a different offset still
    HITs the cache (no re-fetch).

    The walk always paginate-all walks the whole feed from offset 0 and caches it
    keyed on ``(manga_id, languages)`` ONLY. Since per-source offset consumption was
    removed (search.py now applies offset+limit on the merged list), the source returns
    ALL chapters for any offset; a second search with a different offset therefore HITs
    both cache layers (zero new ``/manga`` and ``/chapter`` calls, mirroring
    comix/mangaball) and returns the identical full feed.
    """
    manga_id = str(uuid.uuid4())
    manga_route = respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200, json=_chapter_feed_payload(manga_id, ["1", "2", "3", "4", "5"])
        )
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        page1 = await src.search(
            SearchRequest(type="manga", query="Solo Leveling", offset=0), ctx
        )
        assert sorted(str(r.chapter_number) for r in page1) == ["1", "2", "3", "4", "5"]
        manga_after = manga_route.call_count
        chapter_after = chapter_route.call_count

        page2 = await src.search(
            SearchRequest(type="manga", query="Solo Leveling", offset=2), ctx
        )
        # Both layers HIT (same query AND window-agnostic Layer-2 key): NO new upstream
        # calls. offset is no longer consumed at the source — it returns the full feed.
        assert manga_route.call_count - manga_after == 0
        assert chapter_route.call_count - chapter_after == 0
        assert sorted(str(r.chapter_number) for r in page2) == ["1", "2", "3", "4", "5"]
    finally:
        await transport.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chapter_specific_search_served_from_manga_feed_cache() -> None:
    """A chapter-specific search is served from the type=manga complete-feed cache.

    The Layer-2 key is ``(manga_id, languages)`` ONLY (no ``stop_floor``/offset), so a
    type=chapter search after a type=manga search of the same series is a cache HIT
    (zero new ``/chapter`` calls) and the ``chapter_matches`` floor filter is applied
    post-cache — exactly the comix/mangaball model."""
    manga_id = str(uuid.uuid4())
    respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200, json=_chapter_feed_payload(manga_id, ["4", "5", "6"])
        )
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        await src.search(SearchRequest(type="manga", query="Solo Leveling"), ctx)
        chapter_after = chapter_route.call_count
        # type=chapter chapter=5 shares the (manga_id, languages) cache entry → HIT,
        # no new /chapter call; chapter_matches filters the cached feed to family 5.
        result = await src.search(
            SearchRequest(type="chapter", query="Solo Leveling", chapter=5), ctx
        )
        assert chapter_route.call_count - chapter_after == 0
        assert sorted(str(r.chapter_number) for r in result) == ["5"]
    finally:
        await transport.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_zero_result_repeat_search_served_from_cache() -> None:
    """debug mangadex-search-not-cached (headline regression): a series FOUND on
    MangaDex whose chapters all filter to ZERO results must NOT re-hit ``/chapter`` on
    every differently-targeted repeat — it serves the cached feed like comix/mangaball.

    Pre-fix, the Layer-2 key baked in ``(stop_floor, req.offset)``, so each distinct
    chapter search re-walked ``/chapter`` (logged ~0.11s while comix/mangaball were
    0.00s cache hits). With the window-agnostic key, the feed is walked ONCE and every
    subsequent same-series search (any chapter) is a cache HIT.
    """
    manga_id = str(uuid.uuid4())
    # The manga is found (Layer-1 resolve HITs on repeat), but every chapter is an
    # off-site (externalUrl) row → _to_release drops it → zero matching results.
    feed = _chapter_feed_payload(manga_id, ["1", "2", "3"])
    for row in feed["data"]:
        row["attributes"]["externalUrl"] = "https://example.test/ch"
    respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=feed)
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        first = await src.search(
            SearchRequest(type="manga", query="Solo Leveling"), ctx
        )
        assert first == []  # zero matching results
        chapter_after = chapter_route.call_count
        assert chapter_after == 1  # the feed was walked exactly once

        # Repeated same-series searches at DIFFERENT chapters — every one a HIT.
        for chapter in (5, 6, 5, 7):
            again = await src.search(
                SearchRequest(type="chapter", query="Solo Leveling", chapter=chapter),
                ctx,
            )
            assert again == []
        # The headline assertion: ZERO additional /chapter calls across all repeats.
        assert chapter_route.call_count - chapter_after == 0
    finally:
        await transport.aclose()


# ─── #162: a mode flip (interactive↔non-interactive) is a Layer-1 resolve HIT ──────


@respx.mock
@pytest.mark.asyncio
async def test_mode_flip_is_resolve_hit_zero_new_manga_calls() -> None:
    """#162: count is mode-invariant (5), so the resolve key is mode-agnostic.

    A non-interactive ``type=manga`` search warms (query, languages); a SUBSEQUENT
    ``interactive=True`` search of the SAME (query, languages) makes ZERO additional
    ``/manga`` resolve calls — a Layer-1 resolve HIT across the mode flip (the deployed
    request-29412 redundant-/manga scenario). Pre-#162 the ``extra=count`` discriminator
    (15 interactive vs 5 default) forced a deliberate MISS here.
    """
    manga_id = str(uuid.uuid4())
    manga_route = respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200, json=_chapter_feed_payload(manga_id, ["10", "11", "12"])
        )
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        await src.search(
            SearchRequest(type="manga", query="Solo Leveling", interactive=False), ctx
        )
        manga_after_first = manga_route.call_count

        await src.search(
            SearchRequest(type="chapter", query="Solo Leveling", interactive=True), ctx
        )
        # The mode flip is a resolve HIT: zero additional /manga calls.
        assert manga_route.call_count - manga_after_first == 0
    finally:
        await transport.aclose()


# ──────────────────────── Test C: recent() is never cached ─────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_recent_is_never_cached() -> None:
    """criterion 4: two recent() calls each hit /chapter (no caching, CACHE-05)."""
    manga_id = str(uuid.uuid4())
    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200, json=_chapter_feed_payload(manga_id, ["100", "101"])
        )
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        await src.recent(languages=["en"], limit=5, since=None, ctx=ctx)
        after_first = chapter_route.call_count
        assert after_first == 1
        await src.recent(languages=["en"], limit=5, since=None, ctx=ctx)
        # recent never consults the cache → every call hits upstream again.
        assert chapter_route.call_count - after_first == 1
    finally:
        await transport.aclose()


# ───────────────────── Test D: kill-switch restores pre-Phase-9 ────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_kill_switch_reissues_upstream_calls() -> None:
    """kill-switch (D-08): enabled=False → the repeat chapter search re-fetches."""
    manga_id = str(uuid.uuid4())
    manga_route = respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200, json=_chapter_feed_payload(manga_id, ["10", "11", "12"])
        )
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache(enabled=False))
    try:
        await src.search(SearchRequest(type="manga", query="Solo Leveling"), ctx)
        manga_after = manga_route.call_count
        chapter_after = chapter_route.call_count

        await src.search(SearchRequest(type="chapter", query="Solo Leveling"), ctx)
        # No caching → both layers re-issue their upstream calls (delta > 0).
        assert manga_route.call_count - manga_after > 0
        assert chapter_route.call_count - chapter_after > 0
    finally:
        await transport.aclose()


# ──────── Test E: malformed attributes=None row does not crash the search ─────────


@respx.mock
@pytest.mark.asyncio
async def test_attributes_none_row_does_not_crash_search() -> None:
    """CodeRabbit (PR #156): a ``{"attributes": None}`` chapter row is admitted by
    ``_fetch_chapter_feed`` — ``_build_enumeration``/``_to_release`` must skip it
    rather than raise ``AttributeError`` on ``None.get(...)`` and break the whole
    search path. The valid rows still come back."""
    manga_id = str(uuid.uuid4())
    payload = _chapter_feed_payload(manga_id, ["10", "11"])
    # Inject a malformed row with attributes=None alongside the two valid rows.
    payload["data"].append(
        {
            "id": str(uuid.uuid4()),
            "type": "chapter",
            "attributes": None,
            "relationships": [
                {"id": manga_id, "type": "manga", "attributes": {"title": {"en": "x"}}},
            ],
        }
    )
    payload["total"] = len(payload["data"])
    respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=payload)
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        # Must not raise; the malformed row is skipped, the two valid rows survive.
        result = await src.search(
            SearchRequest(type="manga", query="Solo Leveling"), ctx
        )
        # Strict: the malformed row is DROPPED (not minted as a chapter_number=None
        # release), so exactly the two valid rows come back (CodeRabbit).
        numbers = {str(r.chapter_number) for r in result}
        assert numbers == {"10", "11"}

        # Also exercise a type=chapter search over the same cached feed so the
        # malformed row passes through the post-cache _to_release/chapter_matches path
        # (window-agnostic key → cache HIT, no re-walk). Must not raise (CodeRabbit).
        bounded = await src.search(
            SearchRequest(type="chapter", query="Solo Leveling", chapter=10), ctx
        )
        assert {str(r.chapter_number) for r in bounded} == {"10"}
    finally:
        await transport.aclose()
