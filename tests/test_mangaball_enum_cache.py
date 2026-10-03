"""Plan 09-06 proof: MangaBall opted into both enum-cache layers (CACHE-02..06).

MangaBall is a title-only fan-out source: Layer 1 (``cached_resolve``) caches the
pruned candidate list (the ``search-advanced`` POST) and Layer 2
(``cached_enumerate``) caches each candidate's RAW ``chapter-listing-by-title-id``
flat v2 rows (261003-mangaball-api-v2). The headline win is that a repeat
same-(query, languages) chapter search issues ZERO upstream POSTs on the second
search (both layers HIT).

Three assertions, all network-free (a call-counting ``SourceContext`` stand-in
that delegates the cache seam to a REAL ``EnumerationCache``):

* **zero-cost repeat** — a type=manga search then a same-(query, languages)
  type=chapter search through ONE counting ctx + cache issues a 0 call-count delta
  on BOTH the ``search-advanced`` route AND the per-candidate ``chapter-listing``
  route across the second search — and the second search still returns the correct
  floor-family releases (the ``chapter_matches`` filter is applied post-cache, in
  ``search()``), each with a freshly minted handle.
* **kill-switch** — ``EnumerationCache(enabled=False)`` (D-08) restores the
  pre-Phase-9 behavior: the repeat chapter search re-fires both upstream POSTs.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.enum_cache import EnumerationCache
from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangaball import MangaBallSource
from tests.test_mangaball_search import (
    _chapter,
    _chapter_listing,
    _search_envelope,
    _title,
)

_SEARCH_ADVANCED = "https://mangaball.com/api/v1/title/search-advanced"
_CHAPTER_LISTING = "https://mangaball.com/api/v1/chapter/chapter-listing-by-title-id"
_TITLE_ID = "0123456789abcdef01234567"  # 24-hex (guid contract)


def _chapter_rows() -> list[dict[str, Any]]:
    """A 10.x floor family (``10`` + ``10.5``) so the post-cache filter is visible."""
    return [
        _chapter(row_id="aaaaaaaaaaaaaaaaaaaaaa10", number=10, title_id=_TITLE_ID),
        _chapter(row_id="aaaaaaaaaaaaaaaaaaaaa105", number=10.5, title_id=_TITLE_ID),
        _chapter(row_id="aaaaaaaaaaaaaaaaaaaaaa11", number=11.0, title_id=_TITLE_ID),
        _chapter(row_id="aaaaaaaaaaaaaaaaaaaaaa12", number=12.0, title_id=_TITLE_ID),
    ]


class _CountingCtx:
    """``SourceContext`` stand-in: counts the search-advanced form POST
    (``post_json``) + the JSON-body listing POST (``post_json_body``) and delegates
    the cache seam to a REAL ``EnumerationCache`` (so the cache, not a stub, runs).
    """

    def __init__(
        self,
        *,
        titles: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
        enum_cache: EnumerationCache,
    ) -> None:
        self.handle_store = HandleStore()
        self._titles = titles
        self._listings = listings
        self._enum_cache = enum_cache
        self.search_calls = 0
        self.listing_calls = 0
        self.candidates_enumerated: int | None = None

    # Enum-cache seam — delegate to the real cache (mirrors SourceContext).
    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangaball", normalized_query, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangaball", series_id, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await self._enum_cache.cached_resolve(key, fetch_fn)

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await self._enum_cache.cached_enumerate(key, fetch_fn)

    def cache_replace(self, key: tuple[Any, ...], enum: Any) -> None:
        self._enum_cache.cache_replace(key, enum)

    # Upstream POSTs — counted.
    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        if url == _SEARCH_ADVANCED:
            self.search_calls += 1
            return _search_envelope(self._titles)
        raise AssertionError(f"unexpected post_json url: {url}")

    async def post_json_body(
        self, url: str, *, body: dict[str, Any], **_kw: Any
    ) -> dict[str, Any]:
        if url == _CHAPTER_LISTING:
            self.listing_calls += 1
            return _chapter_listing(self._listings.get(str(body["title_id"]), []))
        raise AssertionError(f"unexpected post_json_body url: {url}")


# ──────────── headline: zero upstream POSTs on the repeat chapter search ────────────


@pytest.mark.asyncio
async def test_repeat_same_series_chapter_search_zero_upstream_calls() -> None:
    """A manga search then a same-series chapter search → 0 ``search-advanced`` POSTs
    AND 0 ``chapter-listing`` POSTs on the second search; floor family right."""
    ctx = _CountingCtx(
        titles=[_title(title_id=_TITLE_ID)],
        listings={_TITLE_ID: _chapter_rows()},
        enum_cache=EnumerationCache(),
    )
    src = MangaBallSource()

    first = await src.search(SearchRequest(type="manga", query="one piece"), ctx)
    assert first  # the first search populated both layers
    assert ctx.search_calls == 1
    assert ctx.listing_calls == 1
    search_after_first = ctx.search_calls
    listing_after_first = ctx.listing_calls

    # Same (query, languages); the floor query selects the 10.x family.
    second = await src.search(
        SearchRequest(type="chapter", query="one piece", chapter=10), ctx
    )

    # The headline win: both layers HIT — neither upstream POST re-fires.
    assert ctx.search_calls - search_after_first == 0
    assert ctx.listing_calls - listing_after_first == 0

    # The floor filter is applied post-cache (in _chapters_to_releases): chapter=10
    # keeps the whole-number/floor family (10 and 10.5), nothing else (the int 10
    # row normalizes to "10").
    nums = sorted(str(r.chapter_number) for r in second)
    assert nums == ["10", "10.5"]
    # Fresh handle per serve (CACHE-03/05) — every served release resolves.
    assert all(r.download_handle for r in second)
    for rel in second:
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


# ─────────────────── kill-switch restores the pre-Phase-9 calls ────────────────────


@pytest.mark.asyncio
async def test_kill_switch_reissues_both_upstream_calls() -> None:
    """``enabled=False`` (D-08): the repeat chapter search re-fires both POSTs."""
    ctx = _CountingCtx(
        titles=[_title(title_id=_TITLE_ID)],
        listings={_TITLE_ID: _chapter_rows()},
        enum_cache=EnumerationCache(enabled=False),
    )
    src = MangaBallSource()

    await src.search(SearchRequest(type="manga", query="one piece"), ctx)
    search_after = ctx.search_calls
    listing_after = ctx.listing_calls

    await src.search(SearchRequest(type="chapter", query="one piece", chapter=10), ctx)
    # No caching → both layers re-issue their upstream work (delta > 0).
    assert ctx.search_calls - search_after > 0
    assert ctx.listing_calls - listing_after > 0
