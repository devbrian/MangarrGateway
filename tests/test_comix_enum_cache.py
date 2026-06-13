"""Plan 09-05 proof: Comix opted into both enum-cache layers (CACHE-02..06).

Comix's chapter enumeration is a browser-DOM read of the warm series page — the
SINGLE biggest cost on this source (7-18s/nav). The headline win is that a repeat
same-series chapter search skips that navigation entirely (Layer-2 HIT) AND skips
the plaintext ``/api/v1/manga`` title call (Layer-1 HIT).

Three assertions, all network-free (respx for the plaintext call) + browser-free
(a fake solver that COUNTS ``fetch_via_browser`` invocations):

* **zero-cost repeat** — a type=manga search then a same-(query, languages)
  type=chapter search through ONE ``SourceContext`` + ``EnumerationCache`` issues
  zero ``fetch_via_browser`` calls AND zero ``/api/v1/manga`` calls on the second
  search (both layers HIT) — and the second search still returns the correct
  floor-family releases (the ``chapter_matches`` filter is applied post-cache, in
  ``search()``).
* **kill-switch** — ``EnumerationCache(enabled=False)`` restores the pre-Phase-9
  re-navigation: the repeat chapter search re-fires the browser nav (delta > 0).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from manga_gateway.config import Settings
from manga_gateway.framework.antibot import Clearance
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.enum_cache import EnumerationCache
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.framework.transport import HttpxTransport
from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.comix import ComixSource

_COMIX = ComixSource.base_url
TEST_API_KEY = "test-key-deterministic-0123456789"
_SERIES_ID = "mr3m0"
_SLUG = "cipher-tales"
_SERIES_URL = f"{_COMIX}/title/{_SERIES_ID}-{_SLUG}"


class _CountingComixSolver:
    """Fake AntiBotSolver: a canned ``Clearance`` + browser-fetch primitives that
    COUNT their invocations and return a staged per-URL result (no real browser).

    ``fetch_calls`` is the cross-search witness the headline assertion reads: a
    Layer-2 cache HIT must never reach the browser, so the delta across the second
    search is exactly 0. The search path enumerates the FULL chapter list in
    PARALLEL (#222) via ``fetch_via_browser_parallel_pages`` — that is the method
    the source actually calls now (``_fetch_series_chapters_raw``), so it increments
    ``fetch_calls``. ``fetch_via_browser`` (the one-shot chapter-pages read) is
    present so ``_solver_from_ctx`` (which requires BOTH primitives) accepts the
    fake; it is unused on the search path.
    """

    def __init__(self) -> None:
        self.browser_results: dict[str, object] = {}
        self.fetch_calls = 0

    def stage_browser_fetch(self, url: str, result: object) -> None:
        self.browser_results[url] = result

    async def get_clearance(self, source_key: str) -> Clearance:
        return Clearance(cookies={"cf_clearance": "CF"}, user_agent="UA")

    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        _ = (extract, wait_for, timeout)
        self.fetch_calls += 1
        if url not in self.browser_results:
            raise AssertionError(f"unmocked fetch_via_browser({url!r})")
        return self.browser_results[url]

    async def fetch_via_browser_typed(
        self,
        url: str,
        *,
        type_selector: str,
        type_text: str,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        # Search mints a `_=` token by typing into the SPA box (debug
        # comix-search-api-403). This is the SEARCH-token nav, distinct from the
        # chapter-enumeration navs ``fetch_calls`` witnesses — so it deliberately
        # does NOT bump ``fetch_calls`` (and the Layer-1 resolve cache skips it on
        # repeat anyway). Returns the tokenized /api/v1/manga URL the source then
        # replays verbatim into the respx mock (matched by path, query ignored).
        _ = (url, type_selector, extract, wait_for, timeout)
        return (
            f"{_COMIX}/api/v1/manga?keyword={type_text}"
            "&limit=6&content_rating=suggestive&_=faketoken"
        )

    async def fetch_via_browser_parallel_pages(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        last_page_selector: str,
        page_param: str,
        route_rewrite: tuple[str, int] | None = None,
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        _ = (extract, wait_for, last_page_selector, page_param, route_rewrite)
        _ = (max_pages, timeout)
        self.fetch_calls += 1
        if url not in self.browser_results:
            raise AssertionError(f"unmocked fetch_via_browser_parallel_pages({url!r})")
        return self.browser_results[url]


def _build_ctx(
    cache: EnumerationCache, solver: _CountingComixSolver
) -> tuple[SourceContext, HttpxTransport]:
    """A real-transport SourceContext (respx intercepts httpx) wired with the fake
    solver + the cache seam. A high rate limit keeps the deterministic run fast."""
    transport = HttpxTransport(Settings(api_key=TEST_API_KEY))
    ctx = SourceContext(
        source_key="comix",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,  # type: ignore[arg-type]  # structural fake (D-42)
        antibot="cloudflare+encrypted",
        enum_cache=cache,
    )
    return ctx, transport


def _candidates_payload() -> dict:
    return {
        "status": "ok",
        "result": {
            "items": [
                {
                    "id": 116210,
                    "hid": _SERIES_ID,
                    "title": "Cipher Tales",
                    "latestChapter": 12,
                    "url": f"/title/{_SERIES_ID}-{_SLUG}",
                    "hasChapters": True,
                    "contentRating": "safe",
                }
            ],
            "meta": {"total": 1, "perPage": 28, "page": 1, "lastPage": 1},
        },
    }


def _chapter_rows() -> list[dict]:
    """Newest-first-ish raw DOM rows (the source re-sorts). Includes a 10.x family
    (``10`` and ``10.5``) so the post-cache floor filter is observable."""
    return [
        {"id": "9001", "chapter": "10", "lang": "en", "groups": [{"name": "TeamX"}]},
        {"id": "9002", "chapter": "10.5", "lang": "en", "groups": [{"name": "TeamX"}]},
        {"id": "9003", "chapter": "11", "lang": "en", "groups": [{"name": "TeamX"}]},
        {"id": "9004", "chapter": "12", "lang": "en", "groups": [{"name": "TeamX"}]},
    ]


# ──────────────── headline: zero browser navs + zero title calls on repeat ─────────


@respx.mock
@pytest.mark.asyncio
async def test_repeat_same_series_chapter_search_zero_browser_navs() -> None:
    """A manga search then a same-series chapter search → 0 browser navs AND 0
    ``/api/v1/manga`` calls on the second search; the served floor family is right."""
    manga_route = respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(200, json=_candidates_payload())
    )
    solver = _CountingComixSolver()
    solver.stage_browser_fetch(_SERIES_URL, _chapter_rows())

    src = ComixSource()
    ctx, transport = _build_ctx(EnumerationCache(), solver)
    try:
        first = await src.search(SearchRequest(type="manga", query="Cipher"), ctx)
        assert first  # the first search populated both layers
        manga_after_first = manga_route.call_count
        navs_after_first = solver.fetch_calls
        assert navs_after_first == 1  # exactly one series-page navigation

        # Same (query, languages); the floor query selects the 10.x family.
        second = await src.search(
            SearchRequest(type="chapter", query="Cipher", chapter=10), ctx
        )

        # The headline win: the second same-series search touches neither the
        # browser nor the plaintext title endpoint (both layers HIT).
        assert solver.fetch_calls - navs_after_first == 0
        assert manga_route.call_count - manga_after_first == 0

        # The floor filter is applied post-cache (in search()): chapter=10 keeps
        # the whole-number/floor family (10 and 10.5), nothing else.
        nums = sorted(str(r.chapter_number) for r in second)
        assert nums == ["10", "10.5"]
        # Fresh handle per serve (CACHE-03/05) — every served release has one.
        assert all(r.download_handle for r in second)
    finally:
        await transport.aclose()


# ─── #162: a mode flip (interactive↔non-interactive) is a Layer-1 resolve HIT ──────


@respx.mock
@pytest.mark.asyncio
async def test_mode_flip_is_resolve_hit_zero_new_manga_calls() -> None:
    """#162: the series-candidate count is mode-invariant (5), so the resolve key is
    mode-agnostic.

    A non-interactive ``type=manga`` search warms (query, languages); a SUBSEQUENT
    ``interactive=True`` search of the SAME (query, languages) makes ZERO additional
    ``/api/v1/manga`` resolve calls — a Layer-1 resolve HIT across the mode flip.
    Pre-#162 the ``extra=count`` discriminator (15 interactive vs 5 default) forced a
    deliberate MISS here.
    """
    manga_route = respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(200, json=_candidates_payload())
    )
    solver = _CountingComixSolver()
    solver.stage_browser_fetch(_SERIES_URL, _chapter_rows())

    src = ComixSource()
    ctx, transport = _build_ctx(EnumerationCache(), solver)
    try:
        await src.search(
            SearchRequest(type="manga", query="Cipher", interactive=False), ctx
        )
        manga_after_first = manga_route.call_count

        await src.search(
            SearchRequest(type="chapter", query="Cipher", interactive=True), ctx
        )
        # The mode flip is a resolve HIT: zero additional /api/v1/manga calls.
        assert manga_route.call_count - manga_after_first == 0
    finally:
        await transport.aclose()


# ─────────────────── kill-switch restores the pre-Phase-9 re-nav ───────────────────


@respx.mock
@pytest.mark.asyncio
async def test_kill_switch_reissues_browser_nav_and_title_call() -> None:
    """``enabled=False`` (D-08): the repeat chapter search re-navigates + re-calls."""
    manga_route = respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(200, json=_candidates_payload())
    )
    solver = _CountingComixSolver()
    solver.stage_browser_fetch(_SERIES_URL, _chapter_rows())

    src = ComixSource()
    ctx, transport = _build_ctx(EnumerationCache(enabled=False), solver)
    try:
        await src.search(SearchRequest(type="manga", query="Cipher"), ctx)
        manga_after = manga_route.call_count
        navs_after = solver.fetch_calls

        await src.search(SearchRequest(type="chapter", query="Cipher", chapter=10), ctx)
        # No caching → both layers re-issue their upstream work (delta > 0).
        assert solver.fetch_calls - navs_after > 0
        assert manga_route.call_count - manga_after > 0
    finally:
        await transport.aclose()
