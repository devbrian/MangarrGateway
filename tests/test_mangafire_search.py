"""Unit tests for ``MangaFireSource.search`` — typed vrf capture → /filter fan-out.

The LIVE flow (D-13): type the keyword via ``solver.fetch_via_browser_typed`` to mint
the search ``vrf`` (captured from ``performance``), then ``ctx.get_bytes`` the
``/filter?keyword=…&vrf=…`` HTML (the SAME vrf works on /filter), parse the title-only
cards, fan out the per-title chapter list, filter by ``chapter_matches``, slice to
``req.limit`` and ONLY THEN mint (GAP-2 mint-after-slice). The vrf is cached per query
(LRU) so a repeat search skips the typed browser nav.

No network/browser: a fake solver returns a canned vrf, and a fake ``SourceContext``
routes ``get_bytes`` (filter HTML) + ``get_json`` (chapter-list result) and mints real
handles.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangafire import (
    _SEARCH_VRF_EXTRACT_JS,
    MangaFireSource,
)
from tests.test_mangafire_recent import _cards_html, _chapter_list_html

_GUID_RE = re.compile(r"^mangafire:[\w.-]+:ch-[\d.?]+:[a-z-]+:[\w.-]+$")


class _FakeTypedSolver:
    def __init__(self, vrf: str | None = "VRF123") -> None:
        self._vrf = vrf
        self.calls: list[dict[str, Any]] = []

    async def fetch_via_browser(self, url: str, **kw: Any) -> Any:  # gate only
        raise AssertionError("search must not use the one-shot browser primitive")

    async def fetch_via_browser_typed(
        self,
        url: str,
        *,
        type_selector: str,
        type_text: str,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — op-budget kwarg mirror
    ) -> Any:
        self.calls.append(
            {
                "url": url,
                "type_selector": type_selector,
                "type_text": type_text,
                "extract": extract,
            }
        )
        return self._vrf


class _NoTypedSolver:
    async def fetch_via_browser(self, url: str, **kw: Any) -> Any:
        return None


class _FakeCtx:
    def __init__(
        self,
        *,
        solver: Any,
        filter_html: bytes,
        chapter_lists: dict[str, str],
    ) -> None:
        from manga_gateway.handles.store import HandleStore

        self.handle_store = HandleStore()
        self._solver = solver
        self._filter_html = filter_html
        self._chapter_lists = chapter_lists
        self.get_bytes_calls: list[str] = []
        self.candidates_enumerated: int | None = None

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("mangafire", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, slug_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("mangafire", slug_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def get_bytes(self, url: str) -> bytes:
        self.get_bytes_calls.append(url)
        return self._filter_html

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        slug_id = url.split("/ajax/manga/")[1].split("/")[0]
        return {"status": 200, "result": self._chapter_lists.get(slug_id, "")}


def _ctx(
    *,
    solver: Any,
    cards: list[tuple[str, str]],
    chapter_lists: dict[str, str],
) -> _FakeCtx:
    return _FakeCtx(
        solver=solver,
        filter_html=_cards_html(cards),
        chapter_lists=chapter_lists,
    )


@pytest.mark.asyncio
async def test_search_types_keyword_captures_vrf_and_mints_releases() -> None:
    solver = _FakeTypedSolver(vrf="VRF123")
    chapter_lists = {
        "kw9j9": _chapter_list_html(
            [{"number": "346.2", "href": "/read/blue-lockk.kw9j9/en/chapter-346.2"}]
        )
    }
    ctx = _ctx(
        solver=solver,
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists=chapter_lists,
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock"), ctx
    )

    # The keyword was TYPED via the real-keyboard primitive with the vrf extract.
    assert len(solver.calls) == 1
    assert solver.calls[0]["type_text"] == "blue lock"
    assert "keyword" in solver.calls[0]["type_selector"]
    assert solver.calls[0]["extract"] == _SEARCH_VRF_EXTRACT_JS
    # The captured vrf rides the /filter call.
    assert any("vrf=VRF123" in u and "keyword=" in u for u in ctx.get_bytes_calls)
    # One per-chapter release with an opaque handle.
    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.chapter_number == Decimal("346.2")
    assert rel.download_handle and ":" not in rel.download_handle
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == "/read/blue-lockk.kw9j9/en/chapter-346.2"


@pytest.mark.asyncio
async def test_search_gap2_mints_only_for_sliced_releases() -> None:
    chapters = [
        {"number": str(n), "href": f"/read/blue-lockk.kw9j9/en/chapter-{n}"}
        for n in range(40, 0, -1)  # newest-first, 40 chapters
    ]
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists={"kw9j9": _chapter_list_html(chapters)},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock", limit=3), ctx
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
async def test_search_caches_vrf_per_query() -> None:
    solver = _FakeTypedSolver()
    ctx = _ctx(
        solver=solver,
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists={
            "kw9j9": _chapter_list_html(
                [{"number": "1", "href": "/read/blue-lockk.kw9j9/en/chapter-1"}]
            )
        },
    )
    src = MangaFireSource()
    await src.search(SearchRequest(type="manga", query="blue lock"), ctx)
    await src.search(SearchRequest(type="manga", query="blue lock"), ctx)
    # vrf cached per query → the typed browser nav fires only ONCE for two searches.
    assert len(solver.calls) == 1


@pytest.mark.asyncio
async def test_search_missing_typed_primitive_raises() -> None:
    ctx = _ctx(
        solver=_NoTypedSolver(),
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists={"kw9j9": ""},
    )
    with pytest.raises(SourceError):
        await MangaFireSource().search(
            SearchRequest(type="manga", query="blue lock"), ctx
        )


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    chapters = [
        {"number": str(n), "href": f"/read/blue-lockk.kw9j9/en/chapter-{n}"}
        for n in range(5, 0, -1)
    ]
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists={"kw9j9": _chapter_list_html(chapters)},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="chapter", query="blue lock", chapter=3.0), ctx
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}
