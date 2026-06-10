"""Unit tests for ``AtsumaruSource.recent`` — title feed → newest-chapter mint.

The recent feed (``GET /api/infinite/recentlyUpdated``) is TITLE-ONLY (no embedded
chapter), so ``recent`` fans out exactly ONE ``GET /api/manga/page?id=`` per title —
that detail endpoint returns the newest-first chapter WINDOW *and* the scanlators map
together, so recent needs no separate ``allChapters`` and no separate scanlators call
(``1 + N`` calls, not ``1 + 2N``). It mints the newest chapter as a DIRECT release
(id always present, no ``:DEFERRED``); the minted handle carries the COMPOSITE
``{mangaId}:{chapterId}`` chapter id (read/chapter needs both).

No network: a fake ``SourceContext`` routes ``get_json`` by URL. ``allChapters`` is
deliberately NOT routed — recent must never call it (it would AssertionError).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.atsumaru import (
    _CHAPTERS_FANOUT_CONCURRENCY,
    _RECENT_TITLE_CAP,
    AtsumaruSource,
    _ms_to_iso,
)

_RECENT = "https://atsu.moe/api/infinite/recentlyUpdated"
_MANGAPAGE = "https://atsu.moe/api/manga/page"
_DEFAULT_SCANLATORS = [{"id": "sg", "name": "Alpha"}]


class _FakeCtxForRecent:
    def __init__(
        self,
        *,
        items: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
        scanlators: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self.handle_store = HandleStore()
        self._items = items
        self._listings = listings
        self._scanlators = scanlators or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append((url, params))
        if url == _RECENT:
            return {"items": self._items}
        if url == _MANGAPAGE:
            mid = str(params.get("id"))
            # manga.page carries BOTH the newest-first chapter window AND scanlators.
            return {
                "mangaPage": {
                    "chapters": self._listings.get(mid, []),
                    "scanlators": self._scanlators.get(mid, _DEFAULT_SCANLATORS),
                }
            }
        raise AssertionError(f"unexpected get_json url: {url}")


def _item(*, manga_id: str, title: str = "One Piece") -> dict[str, Any]:
    return {"id": manga_id, "title": title, "type": "Manga"}


def _chapter(
    *, chapter_id: str, number: Any, created: int = 1780084797472
) -> dict[str, Any]:
    return {
        "id": chapter_id,
        "scanlationMangaId": "sg",
        "title": f"Chapter {number}",
        "number": number,
        "createdAt": created,
        "index": int(number),
        "pageCount": 13,
    }


@pytest.mark.asyncio
async def test_recent_mints_newest_chapter_per_title() -> None:
    ctx = _FakeCtxForRecent(
        items=[
            _item(manga_id="m1", title="One Piece"),
            _item(manga_id="m2", title="Naruto"),
        ],
        listings={
            # manga.page chapters is newest-first: chapters[0] is the latest.
            "m1": [
                _chapter(chapter_id="op2", number=1184),
                _chapter(chapter_id="op1", number=1183),
            ],
            "m2": [_chapter(chapter_id="nr1", number=700)],
        },
    )
    releases = await AtsumaruSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    assert len(releases) == 2
    by_title = {r.manga_title: r for r in releases}
    assert by_title["One Piece"].guid == "atsumaru:m1:ch-1184:op2"  # newest, not op1
    assert by_title["Naruto"].guid == "atsumaru:m2:ch-700:nr1"
    # Composite chapter_id on the minted record.
    record = ctx.handle_store.resolve(by_title["One Piece"].download_handle)
    assert record is not None
    assert record.chapter_id == "m1:op2"
    assert by_title["One Piece"].publish_date.endswith("+00:00")
    # Scanlation group resolved from manga.page scanlators (sg → Alpha).
    assert by_title["One Piece"].scanlation_group == "Alpha"
    assert record.scanlation_group == "Alpha"


@pytest.mark.asyncio
async def test_recent_bounds_fanout_to_limit() -> None:
    items = [_item(manga_id=f"m{i}") for i in range(10)]
    listings = {f"m{i}": [_chapter(chapter_id=f"c{i}", number=i)] for i in range(10)}
    ctx = _FakeCtxForRecent(items=items, listings=listings)
    await AtsumaruSource().recent(languages=None, limit=3, since=None, ctx=ctx)
    # ONE manga.page call per title (bounded by limit) — and NO allChapters at all.
    page_calls = [c for c in ctx.calls if c[0] == _MANGAPAGE]
    assert len(page_calls) == 3
    # 1 recentlyUpdated + 3 manga.page = 4 total (the 1+N shape, not 1+2N).
    assert len(ctx.calls) == 4


@pytest.mark.asyncio
async def test_recent_skips_titles_with_no_chapters() -> None:
    ctx = _FakeCtxForRecent(
        items=[_item(manga_id="m1"), _item(manga_id="empty")],
        listings={"m1": [_chapter(chapter_id="c1", number=1)], "empty": []},
    )
    releases = await AtsumaruSource().recent(
        languages=None, limit=10, since=None, ctx=ctx
    )
    assert len(releases) == 1
    assert releases[0].guid == "atsumaru:m1:ch-1:c1"


@pytest.mark.asyncio
async def test_recent_non_english_language_returns_empty() -> None:
    ctx = _FakeCtxForRecent(items=[_item(manga_id="m1")], listings={"m1": []})
    releases = await AtsumaruSource().recent(
        languages=["ja"], limit=10, since=None, ctx=ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call


def test_recent_fanout_concurrency_is_single_wave() -> None:
    """recent() must fan out wide enough to clear the title cap in ONE wave.

    Regression lock for debug ``atsumaru-unavailable-58171``: the recent fan-out
    originally reused a search-tuned concurrency of 6, which forced ~4 sequential waves
    across the 20-title cap. On atsu.moe's intermittent multi-second latency tail the
    cumulative wall-clock exceeded fanout.py's 30s per-source ``asyncio.timeout`` and
    the source was cancelled → ``source_unavailable``. The source-wide
    ``_CHAPTERS_FANOUT_CONCURRENCY`` must be ≥ the title cap so the worst case is one
    wave (~recentlyUpdated + the single slowest page).
    """
    assert _CHAPTERS_FANOUT_CONCURRENCY >= _RECENT_TITLE_CAP


class _ConcurrencyTrackingCtx(_FakeCtxForRecent):
    """Records peak in-flight ``manga.page`` calls to assert a single fan-out wave."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._inflight = 0
        self.peak_inflight = 0

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        if url == _MANGAPAGE:
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
            try:
                # Yield so every concurrently-launched task can enter before any exits;
                # peak then reflects the true wave width, not scheduling order.
                await asyncio.sleep(0)
                return await super().get_json(url, **params)
            finally:
                self._inflight -= 1
        return await super().get_json(url, **params)


@pytest.mark.asyncio
async def test_recent_launches_full_cap_in_one_wave() -> None:
    # 20 titles (the cap) with limit large enough not to clamp the fan-out.
    n = _RECENT_TITLE_CAP
    items = [_item(manga_id=f"m{i}") for i in range(n)]
    listings = {f"m{i}": [_chapter(chapter_id=f"c{i}", number=i)] for i in range(n)}
    ctx = _ConcurrencyTrackingCtx(items=items, listings=listings)
    releases = await AtsumaruSource().recent(
        languages=None, limit=1000, since=None, ctx=ctx
    )
    assert len(releases) == n
    # All 20 manga.page calls must be in flight at once (single wave), proving the
    # fan-out is no longer throttled to the search-tuned 6.
    assert ctx.peak_inflight == n


def test_ms_to_iso_valid_and_malformed() -> None:
    # A normal epoch-millis value → aware-UTC RFC3339.
    assert _ms_to_iso(1780084797472) == "2026-05-29T19:59:57.472000+00:00"
    assert _ms_to_iso("1780084797472").endswith("+00:00")
    # Missing / non-numeric → None (caller falls back).
    assert _ms_to_iso(None) is None
    assert _ms_to_iso("") is None
    assert _ms_to_iso("not-a-number") is None


@pytest.mark.parametrize("bad", [10**30, -(10**30), 10**400])
def test_ms_to_iso_out_of_range_falls_back(bad: int) -> None:
    # CodeRabbit #184: an absurd epoch must not raise OverflowError/OSError out of
    # _ms_to_iso and crash search/recent — it falls back to None.
    assert _ms_to_iso(bad) is None
