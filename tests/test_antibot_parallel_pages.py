"""Focused unit test for ``CloudflareSolver.fetch_via_browser_parallel_pages`` (#222).

Drives the parallel-pagination primitive against hand-rolled FakeContext/FakePage
doubles — NO real browser is launched (D-42). Models the spike-014 recipe:

* a DISCOVERY page exposes a mutable ``url`` attr; its ``evaluate`` answers the
  "Last page" probe (script contains ``present``) and, on the click script
  (contains ``.click()``), syncs ``url`` to ``?page=N`` (the P-discovery signal);
* a fresh FAN-OUT page per pagination page synthesizes the ``/chapters`` Route on
  ``goto``, runs the installed ``page.route`` handler to verify the per-page
  ``page``+``limit`` rewrite, then returns the rows for its pinned target index.

Coverage (plan Task 1 behavior):
  1. P-discovery via the "Last page" URL sync — fan-out issues exactly P fetches;
  2. P=1 when Last is absent / present-but-disabled;
  3. per-page ``page``+``limit`` rewrite (and a non-``/chapters`` request untouched);
  4. merge + dedup + newest-first order (a low id only on the last page survives);
  5. bounded concurrency == ``fetch_concurrency`` (the existing ``_browser_lock``);
  6. fail-closed on an empty fan-out page (raises, never a partial list);
  7. context-acquire failure surfaces as ``BrowserFetchError``;
  8. dead-driver one-retry of the WHOLE op.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest

from manga_gateway.framework import antibot
from manga_gateway.framework.antibot import BrowserFetchError, CloudflareSolver
from manga_gateway.framework.solver_lifecycle import BrowserLifecycle

_EXTRACT = "return window.__rows__;"
_LAST = 'button[aria-label*="Last"]'
_PAGE_PARAM = "page"
_ROUTE = ("/chapters", 100)
_URL = "https://x/title/abc"
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
_LIMIT_RE = re.compile(r"[?&]limit=(\d+)")


# ─────────────────────────── fakes ───────────────────────────


class _DiscoveryPage:
    """The first page ``new_page()`` hands out: models the "Last page" probe/click.

    ``evaluate`` answers the probe (``{present, disabled}``) and, on the click
    script, syncs ``url`` to ``?page={last_page}`` (only when the Last control is
    present + enabled — exactly the SPA's behavior).
    """

    def __init__(
        self,
        *,
        last_present: bool = True,
        last_disabled: bool = False,
        last_page: int = 4,
    ) -> None:
        self.url = _URL
        self._last_present = last_present
        self._last_disabled = last_disabled
        self._last_page = last_page
        self.routes: list[tuple[str, Any]] = []
        self.goto_calls: list[str] = []
        self.evaluate_scripts: list[str] = []
        self.closed = False

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, **_: object) -> None:
        self.goto_calls.append(url)

    async def wait_for_function(self, *_: object, **__: object) -> None:
        return None

    async def wait_for_selector(self, *_: object, **__: object) -> None:
        return None

    async def evaluate(self, script: str, *_args: object) -> object:
        self.evaluate_scripts.append(script)
        if ".click()" in script:
            if self._last_present and not self._last_disabled:
                self.url = f"{_URL}?page={self._last_page}"
            return None
        if "present" in script:
            return {"present": self._last_present, "disabled": self._last_disabled}
        raise AssertionError(f"unexpected discovery evaluate: {script!r}")

    async def close(self) -> None:
        self.closed = True


class _FanoutPage:
    """A fresh page per fan-out call: derives its pinned target from the installed
    ``page.route`` rewrite and returns ``pages_rows[target]``.

    On ``goto`` it synthesizes a ``/chapters`` request AND a non-matching static
    request, runs the installed handler against both, and records the rewritten
    ``/chapters`` URL (so the test asserts ``page``→target + ``limit``→100) and the
    static continue (untouched). An optional ``tracker`` bumps a shared in-flight
    counter inside the extract ``evaluate`` (with a brief sleep) for the bounded-
    concurrency assertion. ``empty`` forces a fail-closed empty-rows return;
    ``raise_exc`` raises inside ``evaluate`` (the dead-driver path).
    """

    def __init__(
        self,
        pages_rows: dict[int, list[dict[str, Any]]],
        *,
        tracker: _InFlightTracker | None = None,
        empty: bool = False,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._pages_rows = pages_rows
        self._tracker = tracker
        self._empty = empty
        self._raise_exc = raise_exc
        self.routes: list[tuple[str, Any]] = []
        self.goto_calls: list[str] = []
        self.target: int | None = None
        self.chapters_rewrite: str | None = None
        self.static_continue: dict[str, object] | None = None
        self.closed = False

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, **_: object) -> None:
        self.goto_calls.append(url)
        # Run the installed handler against a synthetic /chapters request (the SPA's
        # chapter-list XHR) to verify the per-page page+limit rewrite.
        captured: dict[str, object] = {}

        async def _chapters_continue(**kwargs: object) -> None:
            captured.update(kwargs)

        chapters_req = SimpleNamespace(
            request=SimpleNamespace(
                url="https://x/api/v1/manga/abc/chapters?page=1&limit=20"
            ),
            continue_=_chapters_continue,
        )
        # And a non-/chapters request, which must be continued untouched.
        static_captured: dict[str, object] = {}

        async def _static_continue(**kwargs: object) -> None:
            static_captured.update(kwargs)

        static_req = SimpleNamespace(
            request=SimpleNamespace(url="https://x/static/app.js?page=1&limit=20"),
            continue_=_static_continue,
        )
        for _pattern, handler in self.routes:
            await handler(chapters_req)
            await handler(static_req)
        new_url = str(captured.get("url", ""))
        self.chapters_rewrite = new_url
        self.static_continue = static_captured
        m = _PAGE_RE.search(new_url)
        self.target = int(m.group(1)) if m else None

    async def wait_for_function(self, *_: object, **__: object) -> None:
        return None

    async def wait_for_selector(self, *_: object, **__: object) -> None:
        return None

    def _rows(self) -> list[dict[str, Any]]:
        if self._empty:
            return []
        target = self.target
        assert target is not None  # set by goto's route rewrite
        return list(self._pages_rows[target])

    async def evaluate(self, script: str, *_args: object) -> object:
        if script.startswith("async () => {"):
            if self._raise_exc is not None:
                raise self._raise_exc
            if self._tracker is not None:
                self._tracker.enter()
                try:
                    await asyncio.sleep(0.02)
                    return self._rows()
                finally:
                    self._tracker.exit()
            return self._rows()
        raise AssertionError(f"unexpected fan-out evaluate: {script!r}")

    async def close(self) -> None:
        self.closed = True


class _InFlightTracker:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    def enter(self) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def exit(self) -> None:
        self.in_flight -= 1


class _ParallelContext:
    """Hands out the discovery page first, then a fresh fan-out page per call."""

    def __init__(self, discovery: _DiscoveryPage, fanout_factory: Any) -> None:
        self._discovery = discovery
        self._fanout_factory = fanout_factory
        self._calls = 0
        self.fanout_pages: list[_FanoutPage] = []
        self.closed = False

    async def new_page(self) -> Any:
        self._calls += 1
        if self._calls == 1:
            return self._discovery
        page = self._fanout_factory()
        self.fanout_pages.append(page)
        return page

    async def cookies(self) -> list[dict[str, str]]:  # pragma: no cover — unused
        return []

    async def close(self) -> None:
        self.closed = True


def _solver_with(launch: Any, *, fetch_concurrency: int = 1) -> CloudflareSolver:
    async def _solve(_ctx: object, _key: str) -> antibot.Clearance:  # pragma: no cover
        return antibot.Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=launch, solve=_solve, solve_concurrency=1)
    return CloudflareSolver(lifecycle=lc, fetch_concurrency=fetch_concurrency)


def _single_ctx_solver(
    ctx: _ParallelContext, *, fetch_concurrency: int = 1
) -> CloudflareSolver:
    async def _launch() -> _ParallelContext:
        return ctx

    return _solver_with(_launch, fetch_concurrency=fetch_concurrency)


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the bounded URL-sync poll so the discovery step is fast."""
    monkeypatch.setattr(antibot, "_PAGINATE_POLL_TICKS", 5)
    monkeypatch.setattr(antibot, "_PAGINATE_POLL_INTERVAL", 0.001)


# ─────────────────── (1) P-discovery via Last URL sync ───────────────────


async def test_discovers_p_via_last_button_url_sync() -> None:
    pages_rows = {
        n: [{"id": str(100 - n), "chapter": str(100 - n)}] for n in range(1, 5)
    }
    discovery = _DiscoveryPage(last_present=True, last_disabled=False, last_page=4)
    ctx = _ParallelContext(discovery, lambda: _FanoutPage(pages_rows))
    solver = _single_ctx_solver(ctx)

    result = await solver.fetch_via_browser_parallel_pages(
        _URL,
        extract=_EXTRACT,
        last_page_selector=_LAST,
        page_param=_PAGE_PARAM,
        route_rewrite=_ROUTE,
    )
    # Exactly P=4 fan-out pages fetched, targets 1..4.
    assert len(ctx.fanout_pages) == 4
    assert sorted(p.target for p in ctx.fanout_pages) == [1, 2, 3, 4]
    assert len(result) == 4
    await solver.aclose()


# ─────────────────── (2) P=1 — Last absent / disabled ───────────────────


async def test_p1_when_last_absent() -> None:
    pages_rows = {1: [{"id": "1", "chapter": "1"}]}
    discovery = _DiscoveryPage(last_present=False)
    ctx = _ParallelContext(discovery, lambda: _FanoutPage(pages_rows))
    solver = _single_ctx_solver(ctx)

    result = await solver.fetch_via_browser_parallel_pages(
        _URL,
        extract=_EXTRACT,
        last_page_selector=_LAST,
        page_param=_PAGE_PARAM,
        route_rewrite=_ROUTE,
    )
    assert len(ctx.fanout_pages) == 1  # exactly ONE fetch
    assert [r["id"] for r in result] == ["1"]
    # No click was issued (Last absent → no URL sync attempted).
    assert not any(".click()" in s for s in discovery.evaluate_scripts)
    await solver.aclose()


async def test_p1_when_last_present_but_disabled() -> None:
    pages_rows = {1: [{"id": "1", "chapter": "1"}]}
    discovery = _DiscoveryPage(last_present=True, last_disabled=True)
    ctx = _ParallelContext(discovery, lambda: _FanoutPage(pages_rows))
    solver = _single_ctx_solver(ctx)

    result = await solver.fetch_via_browser_parallel_pages(
        _URL,
        extract=_EXTRACT,
        last_page_selector=_LAST,
        page_param=_PAGE_PARAM,
        route_rewrite=_ROUTE,
    )
    assert len(ctx.fanout_pages) == 1
    assert [r["id"] for r in result] == ["1"]
    assert not any(".click()" in s for s in discovery.evaluate_scripts)
    await solver.aclose()


# ─────────────────── (3) per-page page+limit rewrite ───────────────────


async def test_per_page_page_and_limit_rewrite() -> None:
    pages_rows = {n: [{"id": str(n), "chapter": str(n)}] for n in range(1, 4)}
    discovery = _DiscoveryPage(last_page=3)
    ctx = _ParallelContext(discovery, lambda: _FanoutPage(pages_rows))
    solver = _single_ctx_solver(ctx)

    await solver.fetch_via_browser_parallel_pages(
        _URL,
        extract=_EXTRACT,
        last_page_selector=_LAST,
        page_param=_PAGE_PARAM,
        route_rewrite=_ROUTE,
    )
    by_target = {p.target: p for p in ctx.fanout_pages}
    assert set(by_target) == {1, 2, 3}
    for target, page in by_target.items():
        rewrite = page.chapters_rewrite
        assert rewrite is not None
        page_match = _PAGE_RE.search(rewrite)
        limit_match = _LIMIT_RE.search(rewrite)
        assert page_match is not None, f"page= param missing in {rewrite}"
        assert limit_match is not None, f"limit= param missing in {rewrite}"
        # page= rewritten to this page's pinned target.
        assert page_match.group(1) == str(target)
        # limit= rewritten to the route target (100), at the SAME limit discovery used.
        assert limit_match.group(1) == "100"
        # A non-/chapters request was continued untouched (no url kwarg).
        assert page.static_continue == {}
    await solver.aclose()


# ─────────────────── (4) merge + dedup + newest-first order ───────────────────


async def test_merge_dedups_and_preserves_newest_first_order() -> None:
    # Overlapping ids across page boundaries; id "5" lives ONLY on the last page.
    pages_rows = {
        1: [{"id": "300", "chapter": "300"}, {"id": "250", "chapter": "250"}],
        2: [{"id": "250", "chapter": "250"}, {"id": "200", "chapter": "200"}],
        3: [{"id": "150", "chapter": "150"}, {"id": "5", "chapter": "5"}],
    }
    discovery = _DiscoveryPage(last_page=3)
    ctx = _ParallelContext(discovery, lambda: _FanoutPage(pages_rows))
    solver = _single_ctx_solver(ctx)

    result = await solver.fetch_via_browser_parallel_pages(
        _URL,
        extract=_EXTRACT,
        last_page_selector=_LAST,
        page_param=_PAGE_PARAM,
        route_rewrite=_ROUTE,
    )
    ids = [r["id"] for r in result]
    assert ids == ["300", "250", "200", "150", "5"]  # deduped, first-seen order
    assert "5" in ids  # the low id present ONLY on the last page survives
    await solver.aclose()


# ─────────────────── (5) bounded concurrency == fetch_concurrency ───────────────────


async def test_fan_out_bounded_by_fetch_concurrency() -> None:
    tracker = _InFlightTracker()
    pages_rows = {n: [{"id": str(n), "chapter": str(n)}] for n in range(1, 6)}
    discovery = _DiscoveryPage(last_page=5)
    ctx = _ParallelContext(discovery, lambda: _FanoutPage(pages_rows, tracker=tracker))
    solver = _single_ctx_solver(ctx, fetch_concurrency=2)

    result = await solver.fetch_via_browser_parallel_pages(
        _URL,
        extract=_EXTRACT,
        last_page_selector=_LAST,
        page_param=_PAGE_PARAM,
        route_rewrite=_ROUTE,
    )
    assert len(result) == 5
    # The ONLY bound is the existing _browser_lock semaphore (no second gate).
    assert tracker.max_in_flight == 2
    await solver.aclose()


# ─────────────────── (6) fail-closed on an empty fan-out page ───────────────────


async def test_empty_fan_out_page_fails_closed() -> None:
    pages_rows = {n: [{"id": str(n), "chapter": str(n)}] for n in range(1, 4)}
    made = 0

    def _factory() -> _FanoutPage:
        nonlocal made
        made += 1
        # The SECOND fan-out page constructed returns [] → the whole call must raise.
        return _FanoutPage(pages_rows, empty=made == 2)

    discovery = _DiscoveryPage(last_page=3)
    ctx = _ParallelContext(discovery, _factory)
    solver = _single_ctx_solver(ctx, fetch_concurrency=3)

    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser_parallel_pages(
            _URL,
            extract=_EXTRACT,
            last_page_selector=_LAST,
            page_param=_PAGE_PARAM,
            route_rewrite=_ROUTE,
        )
    assert "no rows" in str(exc.value)
    await solver.aclose()


# ─────────────── (7) context-acquire failure → BrowserFetchError ───────────────


async def test_context_acquire_failure_surfaces_as_browser_fetch_error() -> None:
    async def _bad_launch() -> object:
        raise RuntimeError("patchright failed to start")

    solver = _solver_with(_bad_launch)
    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser_parallel_pages(
            _URL,
            extract=_EXTRACT,
            last_page_selector=_LAST,
            page_param=_PAGE_PARAM,
            route_rewrite=_ROUTE,
        )
    assert "context unavailable" in str(exc.value)
    await solver.aclose()


# ─────────────────── (8) dead-driver one-retry of the whole op ───────────────────


async def test_dead_driver_retries_whole_op_once_then_succeeds() -> None:
    pages_rows = {1: [{"id": "1", "chapter": "1"}]}
    # Two contexts: the first session's fan-out page raises a dead-driver-marked
    # error; after one recycle the second session's fan-out page returns rows.
    ctx1 = _ParallelContext(
        _DiscoveryPage(last_present=False),
        lambda: _FanoutPage(pages_rows, raise_exc=RuntimeError("Target closed")),
    )
    ctx2 = _ParallelContext(
        _DiscoveryPage(last_present=False),
        lambda: _FanoutPage(pages_rows),
    )
    contexts = [ctx1, ctx2]

    async def _launch() -> _ParallelContext:
        return contexts.pop(0)

    solver = _solver_with(_launch)
    result = await solver.fetch_via_browser_parallel_pages(
        _URL,
        extract=_EXTRACT,
        last_page_selector=_LAST,
        page_param=_PAGE_PARAM,
        route_rewrite=_ROUTE,
    )
    assert [r["id"] for r in result] == ["1"]
    # Exactly ONE recycle: both contexts were consumed (the first relaunched once).
    assert contexts == []
    assert ctx1.closed is True  # the dead context was recycled
    await solver.aclose()
