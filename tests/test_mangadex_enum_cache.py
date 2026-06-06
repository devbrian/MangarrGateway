"""Plan 09-04 proof: MangaDex opted into both enum-cache layers (CACHE-02..06).

Four respx call-count assertions on the reference source, mirroring the four phase
success criteria:

* **Test A** (criterion 1) — a type=manga search then a same-series type=chapter
  search through ONE ``SourceContext`` + ``EnumerationCache`` issues ZERO upstream
  ``/manga`` and ``/chapter`` calls on the second search (both layers HIT).
* **Test B** (criterion 2) — a type=chapter floor query BELOW a cached non-exhausted
  window triggers exactly one deeper ``/chapter`` refetch clamped to ``limit=100``
  (completeness refetch, CACHE-04), instead of a false-empty.
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


# ──────────────── Test B: below-window floor query refetches at 100 ────────────────


@respx.mock
@pytest.mark.asyncio
async def test_below_window_chapter_search_refetches_deeper_at_100() -> None:
    """criterion 2: a floor below the cached window refetches deeper, limit=100."""
    manga_id = str(uuid.uuid4())
    respx.get(f"{MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    seen_limits: list[str | None] = []

    def _chapter_responder(request: httpx.Request) -> httpx.Response:
        limit = request.url.params.get("limit")
        seen_limits.append(limit)
        if limit == "100":
            # the deeper refetch reaches the older chapters that cover floor 5
            return httpx.Response(
                200, json=_chapter_feed_payload(manga_id, ["1", "2", "3", "4", "5"])
            )
        # the initial (limit=3) page is a non-exhausted window of 50..60
        return httpx.Response(
            200, json=_chapter_feed_payload(manga_id, ["50", "55", "60"])
        )

    chapter_route = respx.get(f"{MANGADEX}/chapter").mock(
        side_effect=_chapter_responder
    )
    src = MangaDexSource()
    ctx, transport = _build_ctx(EnumerationCache())
    try:
        # limit=3 returns exactly 3 rows → exhausted=False → a real cached window.
        await src.search(
            SearchRequest(type="manga", query="Solo Leveling", limit=3), ctx
        )
        assert seen_limits == ["3"]
        chapter_before = chapter_route.call_count

        # chapter=5 floors below the cached [50, 60] window (not exhausted) → one
        # deeper refetch clamped to _MAX_FEED_LIMIT (100), not a false-empty.
        await src.search(
            SearchRequest(type="chapter", query="Solo Leveling", chapter=5), ctx
        )
        assert chapter_route.call_count - chapter_before == 1
        assert seen_limits[-1] == "100"  # clamped deeper refetch (CACHE-04 / T-09-08)
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
