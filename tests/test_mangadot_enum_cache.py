"""Plan 09-06 proof: Mangadot opted into both enum-cache layers (CACHE-02..06).

Mangadot is a title-only fan-out source: Layer 1 (``cached_resolve``) caches the
pruned candidate list (the ``/api/search`` title call) and Layer 2
(``cached_enumerate``) caches each candidate's RAW ``/api/manga/{id}/chapters/list``
enumeration. The headline win is that a repeat same-(query, languages) chapter
search issues ZERO upstream calls on the second search (both layers HIT).

Three assertions, all network-free (a call-counting ``SourceContext`` stand-in
that delegates the cache seam to a REAL ``EnumerationCache``):

* **zero-cost repeat** — a type=manga search then a same-(query, languages)
  type=chapter search through ONE counting ctx + cache issues a 0 call-count delta
  on BOTH the ``/api/search`` route AND the per-candidate ``chapters/list`` route
  across the second search — and the second search still returns the correct
  floor-family releases (the ``chapter_matches`` filter is applied post-cache, in
  ``search()``), each with a freshly minted handle.
* **kill-switch** — ``EnumerationCache(enabled=False)`` (D-08) restores the
  pre-Phase-9 behavior: the repeat chapter search re-fires both upstream calls.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from manga_gateway.framework.enum_cache import EnumerationCache
from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangadot import MangadotSource

_BASE = "https://mangadot.net"
_SEARCH = f"{_BASE}/api/search"
_MANGA_ID = "20277"


def _chapters_url(manga_id: str) -> str:
    return f"{_BASE}/api/manga/{manga_id}/chapters/list"


def _manga(
    *, manga_id: str = _MANGA_ID, title: str = "Murim Psychopath"
) -> dict[str, Any]:
    return {"id": manga_id, "title": title, "chapter_count": 0, "status": "ongoing"}


def _row(
    *, chapter_id: str, chapter_number: Any, language: str = "en"
) -> dict[str, Any]:
    return {
        "id": chapter_id,
        "chapter_number": chapter_number,
        "volume_number": None,
        "chapter_title": None,
        "language": language,
        "group_name": "Stick",
        "scanlator_name": "Stick",
        "date_added": "2026-05-12 12:21:39+00",
        "page_count": 75,
        "source": "user",
    }


def _chapter_rows() -> list[dict[str, Any]]:
    """A 10.x floor family (``10`` + ``10.5``) so the post-cache filter is visible."""
    return [
        _row(chapter_id="c10", chapter_number="10"),
        _row(chapter_id="c105", chapter_number="10.5"),
        _row(chapter_id="c11", chapter_number="11"),
        _row(chapter_id="c12", chapter_number="12"),
    ]


class _CountingCtx:
    """``SourceContext`` stand-in: counts upstream calls + delegates the cache seam
    to a REAL ``EnumerationCache`` (so the cache behavior, not a stub, is exercised).
    """

    def __init__(
        self,
        *,
        manga_list: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
        enum_cache: EnumerationCache,
    ) -> None:
        self.handle_store = HandleStore()
        self._manga_list = manga_list
        self._listings = listings
        self._enum_cache = enum_cache
        self.search_calls = 0
        self.listing_calls = 0
        self.detail_calls = 0
        self.candidates_enumerated: int | None = None
        # 13-02 external-links seam (mangadot's search now resolves links).
        self.external_links_raw: dict[str, dict[str, Any]] = {}

    async def resolve_external_links(self, series_id: str, parse_fn: Any) -> Any:
        # Best-effort swallow-all mirror — the detail GET below is routed as an
        # UNCOUNTED upstream route (it is neither the /api/search nor the
        # /chapters/list call these tests count), so the headline "zero upstream
        # calls on the repeat search" assertions over search_calls/listing_calls
        # stay exact.
        try:
            return await parse_fn()
        except Exception:
            return None

    # Enum-cache seam — delegate to the real cache (mirrors SourceContext).
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
        return await self._enum_cache.cached_resolve(key, fetch_fn)

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await self._enum_cache.cached_enumerate(key, fetch_fn)

    def cache_replace(self, key: tuple[Any, ...], enum: Any) -> None:
        self._enum_cache.cache_replace(key, enum)

    # Upstream calls — counted.
    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        # The Phase-13 detail object GET /api/manga/{id} (NO /chapters/list suffix)
        # is the external-links route — tracked separately, NOT counted as a search
        # call. Empty body → normalize(...) → None (no links staged here).
        if re.match(rf"{re.escape(_BASE)}/api/manga/[^/]+$", url):
            self.detail_calls += 1
            return {}
        self.search_calls += 1
        if url == _SEARCH:
            return {
                "manga_list": self._manga_list,
                "pagination": {"current_page": 1, "total_pages": 1},
            }
        raise AssertionError(f"unexpected get_json url: {url}")

    async def get_json_array(self, url: str, **params: Any) -> list[Any]:
        self.listing_calls += 1
        m = re.match(rf"{re.escape(_BASE)}/api/manga/(.+)/chapters/list$", url)
        if not m:
            raise AssertionError(f"unexpected get_json_array url: {url}")
        return self._listings.get(m.group(1), [])


# ──────────── headline: zero upstream calls on the repeat chapter search ────────────


@pytest.mark.asyncio
async def test_repeat_same_series_chapter_search_zero_upstream_calls() -> None:
    """A manga search then a same-series chapter search → 0 ``/api/search`` calls
    AND 0 ``chapters/list`` calls on the second search; floor family right."""
    ctx = _CountingCtx(
        manga_list=[_manga()],
        listings={_MANGA_ID: _chapter_rows()},
        enum_cache=EnumerationCache(),
    )
    src = MangadotSource()

    first = await src.search(SearchRequest(type="manga", query="murim psychopath"), ctx)
    assert first  # the first search populated both layers
    assert ctx.search_calls == 1
    assert ctx.listing_calls == 1
    search_after_first = ctx.search_calls
    listing_after_first = ctx.listing_calls

    # Same (query, languages); the floor query selects the 10.x family.
    second = await src.search(
        SearchRequest(type="chapter", query="murim psychopath", chapter=10), ctx
    )

    # The headline win: both layers HIT — neither upstream route re-fires.
    assert ctx.search_calls - search_after_first == 0
    assert ctx.listing_calls - listing_after_first == 0

    # The floor filter is applied post-cache (in _chapters_to_releases): chapter=10
    # keeps the whole-number/floor family (10 and 10.5), nothing else.
    nums = sorted(str(r.chapter_number) for r in second)
    assert nums == ["10", "10.5"]
    # Fresh handle per serve (CACHE-03/05) — every served release resolves.
    assert all(r.download_handle for r in second)
    for rel in second:
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


# ─────────────────── kill-switch restores the pre-Phase-9 calls ────────────────────


@pytest.mark.asyncio
async def test_kill_switch_reissues_both_upstream_calls() -> None:
    """``enabled=False`` (D-08): the repeat chapter search re-fires both calls."""
    ctx = _CountingCtx(
        manga_list=[_manga()],
        listings={_MANGA_ID: _chapter_rows()},
        enum_cache=EnumerationCache(enabled=False),
    )
    src = MangadotSource()

    await src.search(SearchRequest(type="manga", query="murim psychopath"), ctx)
    search_after = ctx.search_calls
    listing_after = ctx.listing_calls

    await src.search(
        SearchRequest(type="chapter", query="murim psychopath", chapter=10), ctx
    )
    # No caching → both layers re-issue their upstream work (delta > 0).
    assert ctx.search_calls - search_after > 0
    assert ctx.listing_calls - listing_after > 0
