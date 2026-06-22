"""Plan 09-05 proof: Comix opted into both enum-cache layers (CACHE-02..06).

Comix's chapter enumeration is an in-WebView eval of the warm series page — the
SINGLE biggest cost on this source. The headline win is that a repeat same-series
chapter search skips that eval entirely (Layer-2 HIT) AND skips the search
token-mint eval (Layer-1 HIT).

Phase 14: comix runs BOTH its search token-mint and its chapter-list enumeration
through ``solver.eval_in_webview`` (the redroid WebView), so the witnesses are now
solver-eval counters, not httpx calls. All assertions stay network-free + browser-
free (a fake solver that COUNTS its evals by kind):

* **zero-cost repeat** — a type=manga search then a same-(query, languages)
  type=chapter search through ONE ``SourceContext`` + ``EnumerationCache`` issues
  zero chapter-list evals AND zero search token-mint evals on the second search
  (both layers HIT) — and the second search still returns the correct floor-family
  releases (the ``chapter_matches`` filter is applied post-cache, in ``search()``).
* **kill-switch** — ``EnumerationCache(enabled=False)`` restores the pre-Phase-9
  re-enumeration: the repeat chapter search re-fires both evals (delta > 0).
"""

from __future__ import annotations

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
    """Fake AntiBotSolver: a canned ``Clearance`` + an ``eval_in_webview`` seam that
    COUNTS its invocations per kind and returns a staged per-URL result (no browser).

    Phase 14: comix runs BOTH its search token-mint (one eval on the homepage) and
    its chapter-list enumeration (one eval per series page) through
    ``eval_in_webview``. The fake routes by nav target:

    * ``search_evals`` counts the homepage search ``c.list`` evals — the Layer-1
      resolve-cache witness (a repeat (query, languages) search must skip it).
    * ``fetch_calls`` counts the per-series chapter-list evals — the Layer-2
      enum-cache witness (a same-series repeat must skip it; delta 0 on HIT).

    Both are network-free; the staged candidates envelope (homepage) and the staged
    chapter rows (series URL) live in ``browser_results``.
    """

    def __init__(self) -> None:
        self.browser_results: dict[str, object] = {}
        self.fetch_calls = 0
        self.search_evals = 0

    def stage_browser_fetch(self, url: str, result: object) -> None:
        self.browser_results[url] = result

    async def get_clearance(self, source_key: str) -> Clearance:
        return Clearance(cookies={"cf_clearance": "CF"}, user_agent="UA")

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — matches the seam contract
    ) -> object:
        _ = (js, wait_for, timeout)
        # The homepage eval is the search token-mint (Layer-1 witness); a series-page
        # eval is the chapter-list enumeration (Layer-2 witness).
        if "/title/" in challenge_url:
            self.fetch_calls += 1
        else:
            self.search_evals += 1
        if challenge_url not in self.browser_results:
            raise AssertionError(f"unmocked eval_in_webview({challenge_url!r})")
        return self.browser_results[challenge_url]


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
    """A manga search then a same-series chapter search → 0 chapter-list evals AND 0
    search token-mint evals on the second search; the served floor family is right."""
    solver = _CountingComixSolver()
    solver.stage_browser_fetch(f"{_COMIX}/", _candidates_payload())
    solver.stage_browser_fetch(_SERIES_URL, _chapter_rows())

    src = ComixSource()
    ctx, transport = _build_ctx(EnumerationCache(), solver)
    try:
        first = await src.search(SearchRequest(type="manga", query="Cipher"), ctx)
        assert first  # the first search populated both layers
        search_after_first = solver.search_evals
        navs_after_first = solver.fetch_calls
        assert navs_after_first == 1  # exactly one series-page chapter-list eval

        # Same (query, languages); the floor query selects the 10.x family.
        second = await src.search(
            SearchRequest(type="chapter", query="Cipher", chapter=10), ctx
        )

        # The headline win: the second same-series search runs neither the
        # chapter-list eval (Layer-2) nor the search token-mint eval (Layer-1).
        assert solver.fetch_calls - navs_after_first == 0
        assert solver.search_evals - search_after_first == 0

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
    search token-mint evals — a Layer-1 resolve HIT across the mode flip. Pre-#162 the
    ``extra=count`` discriminator (15 interactive vs 5 default) forced a deliberate
    MISS here.
    """
    solver = _CountingComixSolver()
    solver.stage_browser_fetch(f"{_COMIX}/", _candidates_payload())
    solver.stage_browser_fetch(_SERIES_URL, _chapter_rows())

    src = ComixSource()
    ctx, transport = _build_ctx(EnumerationCache(), solver)
    try:
        await src.search(
            SearchRequest(type="manga", query="Cipher", interactive=False), ctx
        )
        search_after_first = solver.search_evals

        await src.search(
            SearchRequest(type="chapter", query="Cipher", interactive=True), ctx
        )
        # The mode flip is a resolve HIT: zero additional search token-mint evals.
        assert solver.search_evals - search_after_first == 0
    finally:
        await transport.aclose()


# ─────────────────── kill-switch restores the pre-Phase-9 re-nav ───────────────────


@respx.mock
@pytest.mark.asyncio
async def test_kill_switch_reissues_browser_nav_and_title_call() -> None:
    """``enabled=False`` (D-08): the repeat chapter search re-navigates + re-calls."""
    solver = _CountingComixSolver()
    solver.stage_browser_fetch(f"{_COMIX}/", _candidates_payload())
    solver.stage_browser_fetch(_SERIES_URL, _chapter_rows())

    src = ComixSource()
    ctx, transport = _build_ctx(EnumerationCache(enabled=False), solver)
    try:
        await src.search(SearchRequest(type="manga", query="Cipher"), ctx)
        search_after = solver.search_evals
        navs_after = solver.fetch_calls

        await src.search(SearchRequest(type="chapter", query="Cipher", chapter=10), ctx)
        # No caching → both layers re-issue their upstream work (delta > 0).
        assert solver.fetch_calls - navs_after > 0
        assert solver.search_evals - search_after > 0
    finally:
        await transport.aclose()
