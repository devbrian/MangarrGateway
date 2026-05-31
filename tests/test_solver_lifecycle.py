"""Bounded solver-lifecycle tests (Plan 04-04 Task 1, criterion #4 / BOT-02).

Every test here drives a MOCKED browser double — NO real Chromium is launched
(D-42, the deterministic gate). The fake records launch/close/solve calls so the
four lifecycle invariants can be asserted offline:

* single-flight collapse (Pitfall 6) — N concurrent callers trigger ONE solve;
* solve-cap semaphore (Pitfall 6) — simultaneous solves are bounded;
* recycle watchdog (Pattern 7) — the persistent context is restarted on cadence;
* cleanup-on-all-paths (Pitfall 4) — ``aclose()`` tears the browser down even
  when invoked under ``CancelledError`` (no orphan Chromium).

``CloudflareSolver.get_clearance`` is also exercised against the fake to prove the
captured ``user_agent`` comes from the SAME session as the ``cf_clearance`` cookie
(Pitfall 1) and that a non-cloudflare key returns ``None`` (MangaDex untouched).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from manga_gateway.framework.antibot import (
    AntiBotSolver,
    BrowserFetchError,
    Clearance,
    CloudflareSolver,
    _looks_like_dead_driver,
)
from manga_gateway.framework.solver_lifecycle import BrowserLifecycle


class FakeContext:
    """A fake Patchright persistent context capturing the cf_clearance + UA."""

    def __init__(self, *, ua: str = "Mozilla/5.0 FakeChrome", clearance: str = "TOKEN"):
        self._ua = ua
        self._clearance = clearance
        self.closed = False
        self.goto_urls: list[str] = []

    async def new_page(self) -> FakePage:
        return FakePage(self)

    async def cookies(self) -> list[dict[str, str]]:
        # Mirror real Patchright cookie dicts: every entry has a ``domain`` so the
        # solver's host-scope filter can keep cf_clearance + the on-host session
        # cookie and reject cross-site (ad-tracking) cookies. challenge_url
        # defaults to https://comix.to/ for these tests.
        return [
            {"name": "cf_clearance", "value": self._clearance, "domain": ".comix.to"},
            {"name": "session", "value": "LARAVEL_SESSION_VALUE", "domain": "comix.to"},
            {"name": "ad_tracking", "value": "ignored", "domain": "mosved.com"},
        ]

    async def close(self) -> None:
        self.closed = True


class FakePage:
    def __init__(self, ctx: FakeContext) -> None:
        self._ctx = ctx

    async def goto(self, url: str, **_: object) -> None:
        self._ctx.goto_urls.append(url)

    async def evaluate(self, script: str) -> str:
        return self._ctx._ua

    async def wait_for_function(self, *_: object, **__: object) -> None:
        return None


class FakeBrowser:
    """Counts launches/closes; each launch yields a fresh FakeContext.

    Passed to ``BrowserLifecycle`` as the ``launch`` callable + to
    ``CloudflareSolver`` so NO real Chromium is ever spawned.
    """

    def __init__(self) -> None:
        self.launch_count = 0
        self.contexts: list[FakeContext] = []
        self.solve_count = 0
        self.concurrent_solves = 0
        self.max_concurrent_solves = 0

    async def launch(self) -> FakeContext:
        self.launch_count += 1
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    async def solve(self, ctx: FakeContext) -> Clearance:
        self.concurrent_solves += 1
        self.max_concurrent_solves = max(
            self.max_concurrent_solves, self.concurrent_solves
        )
        self.solve_count += 1
        try:
            await asyncio.sleep(0.02)  # widen the window so collapse/cap are observable
            cookies = {
                c["name"]: c["value"]
                for c in await ctx.cookies()
                if c["name"] == "cf_clearance"
            }
            page = await ctx.new_page()
            ua = await page.evaluate("navigator.userAgent")
            return Clearance(cookies=cookies, user_agent=ua)
        finally:
            self.concurrent_solves -= 1


# ───────────────────────────── single-flight ─────────────────────────────


async def test_single_flight_collapses_concurrent_callers() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=4)
    results = await asyncio.gather(*(lc.solve() for _ in range(8)))
    # 8 concurrent callers → exactly ONE underlying solve (Pitfall 6).
    assert fake.solve_count == 1
    assert all(r.cookies["cf_clearance"] == "TOKEN" for r in results)
    await lc.aclose()


async def test_distinct_solves_after_first_completes() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    await lc.solve()
    await lc.solve(force=True)  # a forced re-solve runs again
    assert fake.solve_count == 2
    await lc.aclose()


# ───────────────────────────── D-35 held clearance ─────────────────────────────


async def test_sequential_nonforced_solves_reuse_held_clearance() -> None:
    """D-35: subsequent non-forced solve() calls return the held Clearance from
    the first solve. Without this, every outbound request drives a fresh
    Patchright solve and the per-search budget exhausts as
    ``source_unavailable / timed out`` (see debug session
    `comix-cf-solver-loop`)."""
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    first = await lc.solve()
    second = await lc.solve()
    third = await lc.solve()
    assert fake.solve_count == 1
    # All three callers see the EXACT same Clearance instance — the cache is
    # by-reference, not a re-issued copy.
    assert first is second is third
    await lc.aclose()


async def test_force_resolve_replaces_held_clearance() -> None:
    """A force=True solve must overwrite the cached value so the next non-forced
    caller sees the freshly-issued Clearance (D-35 reactive re-solve)."""
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    first = await lc.solve()
    second = await lc.solve(force=True)
    # held updated → next non-forced returns the FORCED clearance, not the first.
    third = await lc.solve()
    assert fake.solve_count == 2
    assert third is second
    assert third is not first
    await lc.aclose()


async def test_recycle_invalidates_held_clearance() -> None:
    """The recycle watchdog tears the persistent context down; the cached
    Clearance is bound to that session so the next call must re-solve against
    the fresh context (no orphan cookies, no stale UA)."""
    fake = FakeBrowser()
    lc = BrowserLifecycle(
        launch=fake.launch, solve=fake.solve, solve_concurrency=1, recycle_seconds=0.05
    )
    await lc.solve()  # solve #1, held populated, launch #1
    lc.start_recycle_watchdog()
    await asyncio.sleep(0.12)  # let the watchdog fire at least once
    await lc.solve()  # held was cleared by _close_context → real re-solve
    assert fake.solve_count >= 2
    assert fake.launch_count >= 2
    await lc.aclose()


# ───────────────────────────── solve cap ─────────────────────────────


async def test_solve_semaphore_bounds_concurrency() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=2)
    # Force distinct solves so the semaphore (not single-flight) gates concurrency.
    await asyncio.gather(*(lc.solve(force=True) for _ in range(6)))
    assert fake.max_concurrent_solves <= 2
    await lc.aclose()


# ───────────────────────────── recycle ─────────────────────────────


async def test_recycle_restarts_persistent_context() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(
        launch=fake.launch, solve=fake.solve, solve_concurrency=1, recycle_seconds=0.05
    )
    await lc.solve()  # launch #1
    first_ctx = fake.contexts[0]
    lc.start_recycle_watchdog()
    await asyncio.sleep(0.12)  # let the watchdog fire at least once
    await lc.solve()
    assert fake.launch_count >= 2  # the context was recycled
    assert first_ctx.closed  # old context torn down on recycle (no orphan)
    await lc.aclose()


# ───────────────────────────── cleanup-on-all-paths ─────────────────────────────


async def test_aclose_tears_down_browser() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    await lc.solve()
    await lc.aclose()
    assert all(c.closed for c in fake.contexts)


async def test_aclose_under_cancellederror_still_closes() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    await lc.solve()

    # Simulate aclose() being invoked while a CancelledError is propagating
    # (lifespan teardown / drain-timeout cancellation). The browser MUST still
    # be torn down — no orphan Chromium survives (Pitfall 4).
    try:
        raise asyncio.CancelledError
    except asyncio.CancelledError:
        await lc.aclose()
    assert all(c.closed for c in fake.contexts)


async def test_recycle_watchdog_cancelled_by_aclose() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(
        launch=fake.launch, solve=fake.solve, solve_concurrency=1, recycle_seconds=0.05
    )
    await lc.solve()
    lc.start_recycle_watchdog()
    await lc.aclose()  # must cancel the watchdog task, not leak it
    assert lc._recycle_task is None or lc._recycle_task.done()


# ───────────────────────── CloudflareSolver.get_clearance ─────────────────────────


async def test_solver_satisfies_protocol() -> None:
    solver = CloudflareSolver()
    assert isinstance(solver, AntiBotSolver)


async def test_get_clearance_captures_matching_ua() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc, cloudflare_keys=("cf-source",))
    clearance = await solver.get_clearance("cf-source")
    assert clearance is not None
    assert clearance.cookies["cf_clearance"] == "TOKEN"
    # The UA is the EXACT navigator.userAgent of the solving session (Pitfall 1).
    assert clearance.user_agent == "Mozilla/5.0 FakeChrome"
    await solver.aclose()


async def test_get_clearance_none_for_non_cloudflare_key() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc, cloudflare_keys=("cf-source",))
    # A key that is NOT in cloudflare_keys needs no clearance → None, no solve.
    assert await solver.get_clearance("plain-source") is None
    assert fake.solve_count == 0
    await solver.aclose()


async def test_get_clearance_force_resolve_kwarg_accepted() -> None:
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc, cloudflare_keys=("cf-source",))
    await solver.get_clearance("cf-source")
    # D-35 re-solve path: force_resolve is an internal kwarg kept OFF the Protocol.
    await solver.get_clearance("cf-source", force_resolve=True)
    assert fake.solve_count == 2
    await solver.aclose()


def test_force_resolve_off_protocol() -> None:
    import inspect

    sig = inspect.signature(AntiBotSolver.get_clearance)
    assert "force_resolve" not in sig.parameters  # D-41 — Protocol unchanged


# ─────────────────────── Option A: fetch_via_browser primitive ──────────────────


class _SharedConcurrency:
    """Cross-page in-flight counter — exposed by :class:`_FetchContext` so the
    serialization test can assert ``_browser_lock`` bounds concurrency GLOBALLY
    (not just per-page, which is trivially 1 since each page is opened once)."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0


class _FakeFetchPage:
    """A FakePage for ``fetch_via_browser`` — records goto/wait/evaluate/close
    and lets each test stage the return value or an error per stage.

    Also records ``page.on(event, handler)`` calls so the #54 diagnostic
    instrumentation tests can assert which handlers were attached. The
    ``url`` attribute is exposed so the instrumented handlers' ``getattr(
    page, "url", None)`` reads have something to read (real Playwright pages
    expose ``.url`` synchronously)."""

    def __init__(
        self,
        *,
        evaluate_result: object = None,
        goto_error: Exception | None = None,
        wait_function_error: Exception | None = None,
        wait_selector_error: Exception | None = None,
        evaluate_error: Exception | None = None,
        shared_concurrency: _SharedConcurrency | None = None,
        page_url: str = "https://example.test/page-url",
    ) -> None:
        self.evaluate_result = evaluate_result
        self.goto_error = goto_error
        self.wait_function_error = wait_function_error
        self.wait_selector_error = wait_selector_error
        self.evaluate_error = evaluate_error
        self.goto_calls: list[tuple[str, dict[str, object]]] = []
        self.wait_function_calls: list[str] = []
        self.wait_selector_calls: list[str] = []
        self.evaluate_calls: list[str] = []
        self.closed = False
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        # Optional — set by _FetchContext.new_page() so concurrent evaluates
        # across DIFFERENT pages count against a single shared peak.
        self._shared = shared_concurrency
        # #54 instrumentation: record every ``page.on(event, handler)`` call.
        # Mirrors Playwright's sync ``Page.on`` surface (NOT awaitable). Tests
        # can fire a registered handler manually via :meth:`fire`.
        self.on_calls: list[tuple[str, Callable[[object], None]]] = []
        # The ``page.url`` attribute the instrumented handlers read via
        # ``getattr(page, "url", None)``. Matches Playwright's sync property.
        self.url = page_url

    def on(self, event: str, handler: Callable[[object], None]) -> None:
        """Record an ``on(event, handler)`` registration. Sync — Playwright's
        ``Page.on`` is sync; an ``async def`` handler would silently no-op."""
        self.on_calls.append((event, handler))

    def fire(self, event: str, payload: object) -> None:
        """Invoke every registered handler for ``event`` with ``payload``.

        Tests use this to drive the instrumented pageerror/console/
        requestfailed handlers without booting a real browser."""
        for ev, handler in self.on_calls:
            if ev == event:
                handler(payload)

    async def goto(self, url: str, **kwargs: object) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.goto_calls.append((url, kwargs))

    async def wait_for_function(self, predicate: str, **_: object) -> None:
        if self.wait_function_error is not None:
            raise self.wait_function_error
        self.wait_function_calls.append(predicate)

    async def wait_for_selector(self, selector: str, **_: object) -> None:
        if self.wait_selector_error is not None:
            raise self.wait_selector_error
        self.wait_selector_calls.append(selector)

    async def evaluate(self, script: str, *_args: object) -> object:
        self.concurrent_calls += 1
        self.max_concurrent_calls = max(
            self.max_concurrent_calls, self.concurrent_calls
        )
        if self._shared is not None:
            self._shared.in_flight += 1
            self._shared.max_in_flight = max(
                self._shared.max_in_flight, self._shared.in_flight
            )
        try:
            await asyncio.sleep(0.01)  # widen window so serialization is observable
            if self.evaluate_error is not None:
                raise self.evaluate_error
            self.evaluate_calls.append(script)
            return self.evaluate_result
        finally:
            self.concurrent_calls -= 1
            if self._shared is not None:
                self._shared.in_flight -= 1

    async def close(self) -> None:
        self.closed = True


class _FetchContext:
    """A FakeContext whose ``new_page`` returns the queued :class:`_FakeFetchPage`
    instances (one per ``fetch_via_browser`` call)."""

    def __init__(self, pages: list[_FakeFetchPage]) -> None:
        self._pages = list(pages)
        self.opened: list[_FakeFetchPage] = []
        self.new_page_error: Exception | None = None
        self.closed = False
        # Shared in-flight counter wired into every page handed out — lets the
        # serialization test assert global cross-page bounding.
        self.shared_concurrency = _SharedConcurrency()

    async def new_page(self) -> _FakeFetchPage:
        if self.new_page_error is not None:
            raise self.new_page_error
        if not self._pages:  # pragma: no cover — test wiring guard
            raise AssertionError("no more queued fetch pages")
        page = self._pages.pop(0)
        page._shared = self.shared_concurrency
        self.opened.append(page)
        return page

    async def cookies(self) -> list[dict[str, str]]:  # pragma: no cover - unused here
        return []

    async def close(self) -> None:
        self.closed = True


def _solver_with_fetch_context(
    ctx: _FetchContext, *, log_browser_events: bool = False
) -> CloudflareSolver:
    """Build a CloudflareSolver whose lifecycle yields the given fetch context.

    The lifecycle's ``solve`` callable is never invoked by these tests (they
    exercise ``fetch_via_browser`` directly), but we still supply a stub that
    would record a Clearance if called. ``log_browser_events`` toggles the
    #54 diagnostic instrumentation under test in the
    ``test_browser_event_*`` cases below.
    """

    async def _launch() -> _FetchContext:
        return ctx

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    return CloudflareSolver(lifecycle=lc, log_browser_events=log_browser_events)


async def test_fetch_via_browser_goto_evaluate_roundtrip() -> None:
    page = _FakeFetchPage(evaluate_result=["a", "b", "c"])
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)

    result = await solver.fetch_via_browser(
        "https://comix.to/title/abc/1-chapter-1",
        extract="return [1,2,3];",
    )
    assert result == ["a", "b", "c"]
    # goto navigated to the requested URL.
    assert page.goto_calls and page.goto_calls[0][0].endswith("/1-chapter-1")
    # The extract body was wrapped in an async IIFE before page.evaluate.
    assert page.evaluate_calls == ["async () => { return [1,2,3]; }"]
    await solver.aclose()


async def test_fetch_via_browser_wait_for_selector_when_not_predicate() -> None:
    page = _FakeFetchPage(evaluate_result=[])
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)

    await solver.fetch_via_browser(
        "https://x/y",
        extract="return [];",
        wait_for='img[src*="/si/"]',
    )
    # CSS-selector-shaped wait_for routes to wait_for_selector, NOT to
    # wait_for_function (heuristic: no ``=>`` and no ``return``).
    assert page.wait_selector_calls == ['img[src*="/si/"]']
    assert page.wait_function_calls == []
    await solver.aclose()


async def test_fetch_via_browser_wait_for_predicate_when_js_arrow() -> None:
    page = _FakeFetchPage(evaluate_result=[])
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)

    predicate = "() => document.querySelectorAll('img').length > 0"
    await solver.fetch_via_browser(
        "https://x/y",
        extract="return [];",
        wait_for=predicate,
    )
    assert page.wait_function_calls == [predicate]
    assert page.wait_selector_calls == []
    await solver.aclose()


async def test_fetch_via_browser_closes_page_on_success() -> None:
    page = _FakeFetchPage(evaluate_result="ok")
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)
    await solver.fetch_via_browser("https://x/y", extract="return 1;")
    assert page.closed is True
    await solver.aclose()


async def test_fetch_via_browser_closes_page_on_evaluate_error() -> None:
    page = _FakeFetchPage(evaluate_error=RuntimeError("DOM read blew up"))
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)
    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser("https://x/y", extract="return 1;")
    assert "page.evaluate failed" in str(exc.value)
    # Page MUST be closed even when evaluate raised — no orphan pages (Pitfall 4).
    assert page.closed is True
    await solver.aclose()


async def test_fetch_via_browser_translates_goto_failure() -> None:
    page = _FakeFetchPage(goto_error=RuntimeError("net::ERR_FAILED"))
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)
    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser("https://x/y", extract="return 1;")
    assert "goto" in str(exc.value)
    assert page.closed is True
    await solver.aclose()


async def test_fetch_via_browser_translates_wait_for_timeout() -> None:
    page = _FakeFetchPage(wait_selector_error=RuntimeError("Timeout 30000ms exceeded"))
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)
    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser(
            "https://x/y", extract="return 1;", wait_for="img.page"
        )
    assert "wait_for" in str(exc.value)
    assert page.closed is True
    await solver.aclose()


async def test_fetch_via_browser_bounds_concurrent_calls() -> None:
    """fetch_via_browser acquires a ``_browser_lock`` Semaphore slot so a
    concurrent burst opens a fresh page per call and bounds in-flight
    page.evaluate calls to the Semaphore's cap (default 5). Was a 1-wide Lock
    before debug session ``comix-search-timeout`` — turning the per-source
    20s search budget into sum-of-individual-navs and breaking comix /search
    under variance. The bounded-Semaphore tradeoff: humans browse with
    multiple tabs open routinely (no novel fingerprint), the Playwright
    driver multiplexes pages on the warm persistent context, and a small
    cap still keeps the simultaneous-page footprint modest."""
    pages = [_FakeFetchPage(evaluate_result=i) for i in range(5)]
    ctx = _FetchContext(pages)
    solver = _solver_with_fetch_context(ctx)
    results = await asyncio.gather(
        *(
            solver.fetch_via_browser(f"https://x/{i}", extract=f"return {i};")
            for i in range(5)
        )
    )
    assert results == [0, 1, 2, 3, 4]
    # Bounded by the Semaphore cap (default 5 == _DEFAULT_SERIES_CANDIDATES).
    # The cap is read off the solver so this assertion tracks the constant if
    # someone tunes it; the meaningful invariant is that the cap is honored —
    # NOT that no parallelism happens (the old Lock behavior).
    assert ctx.shared_concurrency.max_in_flight <= solver._browser_concurrency
    # Each fake page is opened once — its own in-flight counter is trivially 1.
    assert all(p.max_concurrent_calls == 1 for p in pages)
    assert len(ctx.opened) == 5
    assert all(p.closed for p in pages)
    await solver.aclose()


async def test_fetch_via_browser_bounded_semaphore_caps_burst_beyond_limit() -> None:
    """A burst LARGER than the Semaphore cap (default 5) sees the cap honored —
    the first 5 run concurrently, the next 5 queue behind released slots.
    Verifies the bounded-Semaphore is not a free-for-all under heavy load
    (e.g. interactive search's 15 candidates)."""
    burst_size = 10
    pages = [_FakeFetchPage(evaluate_result=i) for i in range(burst_size)]
    ctx = _FetchContext(pages)
    solver = _solver_with_fetch_context(ctx)
    results = await asyncio.gather(
        *(
            solver.fetch_via_browser(f"https://x/{i}", extract=f"return {i};")
            for i in range(burst_size)
        )
    )
    assert results == list(range(burst_size))
    # Peak in-flight MUST honor the Semaphore cap regardless of burst size.
    assert ctx.shared_concurrency.max_in_flight <= solver._browser_concurrency
    assert len(ctx.opened) == burst_size
    assert all(p.closed for p in pages)
    await solver.aclose()


async def test_fetch_via_browser_context_failure_raises_browser_fetch_error() -> None:
    """If the warm context cannot be acquired (e.g. a launch failure), the
    primitive surfaces a BrowserFetchError rather than leaking the underlying
    exception type — sources treat the failure as a source-level read error."""

    async def _bad_launch() -> object:
        raise RuntimeError("patchright failed to start")

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_bad_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)
    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser("https://x/y", extract="return 1;")
    assert "context unavailable" in str(exc.value)
    await solver.aclose()


# ─────────────────────── #54: dead-driver recycle + retry-once ──────────────────


# Marker substring emitted by Playwright when the underlying Node driver process
# has exited (issue #54, Firefox handler crash on undefined pageError.location).
# Substring-matched in production by ``_looks_like_dead_driver``; matched here
# for direct dead-driver assertions.
_DEAD_DRIVER_MSG = "Connection closed while reading from the driver"


async def test_lifecycle_recycle_now_closes_and_clears_held_clearance() -> None:
    """``recycle_now()`` closes the live context AND drops the cached Clearance.

    The held clearance is bound to the torn-down session (its cookies and UA were
    captured FROM that browser); the next solve must re-warm against the freshly
    launched context. This is the same invariant ``_close_context`` enforces for
    the time-based watchdog — verifies the crash-driven path keeps it.
    """
    browser = FakeBrowser()
    lc = BrowserLifecycle(launch=browser.launch, solve=browser.solve)

    # Drive one full solve so the lifecycle caches both a context and a Clearance.
    first = await lc.solve()
    assert isinstance(first, Clearance)
    assert browser.launch_count == 1
    assert lc._held is first  # cached for subsequent non-forced callers

    await lc.recycle_now()

    # Cached context cleared (next get_context relaunches), held Clearance cleared
    # (next solve runs a fresh one).
    assert lc._context is None
    assert lc._held is None
    assert browser.contexts[0].closed is True

    # Re-solving relaunches a fresh context (verifies recycle_now actually
    # invalidated the cache rather than just looking like it did).
    second = await lc.solve()
    assert isinstance(second, Clearance)
    assert browser.launch_count == 2

    await lc.aclose()


async def test_lifecycle_recycle_now_is_noop_when_no_context_live() -> None:
    """``recycle_now()`` before any launch is a no-op (no spurious launch/close).

    Belt-and-braces — the production wrapper calls ``recycle_now`` immediately
    after detecting a dead-driver crash, and a concurrent caller may have ALSO
    closed the context first. Idempotent behavior keeps the second caller
    correct without needing additional gating.
    """
    browser = FakeBrowser()
    lc = BrowserLifecycle(launch=browser.launch, solve=browser.solve)

    await lc.recycle_now()
    await lc.recycle_now()  # double call

    assert browser.launch_count == 0
    assert lc._context is None
    assert lc._held is None

    await lc.aclose()


class _DeadDriverContext:
    """A FakeContext whose every ``new_page`` raises a Playwright-shaped
    ``Connection closed while reading from the driver`` error — simulating the
    state after the Node driver process has crashed (issue #54). ``cookies``
    and ``close`` mirror the real shape so the solver's existing teardown path
    works unchanged."""

    def __init__(self) -> None:
        self.new_page_calls = 0
        self.closed = False

    async def new_page(self) -> object:
        self.new_page_calls += 1
        # The real Playwright raises something like ``Error: BrowserContext.new_page:
        # <msg>`` — the surrounding string is irrelevant to the detector; only the
        # marker substring on the exception message matters.
        raise RuntimeError(f"BrowserContext.new_page: {_DEAD_DRIVER_MSG}")

    async def cookies(self) -> list[dict[str, str]]:  # pragma: no cover — unused here
        return []

    async def close(self) -> None:
        self.closed = True


async def test_fetch_via_browser_recycles_on_dead_driver_and_retries_once() -> None:
    """First ``new_page`` raises the dead-driver signal — solver recycles the
    lifecycle and retries against a fresh context, which succeeds.

    This is the #54 happy-path: the upstream Playwright Firefox handler crashed,
    poisoning the cached BrowserContext. Without the shim, every subsequent
    fetch in the same job queue fails with the same poisoned handle. With the
    shim, one crash blast-radius collapses to "one transparent retry".
    """
    dead = _DeadDriverContext()
    healthy_page = _FakeFetchPage(evaluate_result=["page1", "page2"])
    healthy_ctx = _FetchContext([healthy_page])

    contexts_to_launch: list[object] = [dead, healthy_ctx]
    launch_count = 0

    async def _launch() -> object:
        nonlocal launch_count
        launch_count += 1
        return contexts_to_launch.pop(0)

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)

    result = await solver.fetch_via_browser(
        "https://comix.to/title/abc/1-chapter-1", extract="return [1,2];"
    )

    # Retry produced the result.
    assert result == ["page1", "page2"]
    # Dead ctx was opened once (the failing new_page), then closed by recycle.
    assert dead.new_page_calls == 1
    assert dead.closed is True
    # A healthy ctx was launched after the recycle and ran the retry.
    assert launch_count == 2
    assert len(healthy_ctx.opened) == 1
    assert healthy_ctx.opened[0] is healthy_page
    assert healthy_page.closed is True

    await solver.aclose()


async def test_fetch_via_browser_recycles_on_dead_driver_via_evaluate_error() -> None:
    """The dead-driver signal can also surface on ``page.evaluate`` — the engine
    log in issue #54 shows ``page.evaluate failed: Connection closed while
    reading from the driver`` for the first job in the failed run. Same recycle
    + retry-once behavior is required there.
    """
    dead_page = _FakeFetchPage(
        evaluate_error=RuntimeError(f"Page.evaluate: {_DEAD_DRIVER_MSG}")
    )
    dead_ctx = _FetchContext([dead_page])
    healthy_page = _FakeFetchPage(evaluate_result="ok")
    healthy_ctx = _FetchContext([healthy_page])

    contexts_to_launch: list[object] = [dead_ctx, healthy_ctx]

    async def _launch() -> object:
        return contexts_to_launch.pop(0)

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)

    result = await solver.fetch_via_browser("https://x/y", extract="return 1;")

    assert result == "ok"
    # First attempt closed its page (finally clause), second attempt's page closed too.
    assert dead_page.closed is True
    assert healthy_page.closed is True
    # The dead-context was recycled (closed) before the second launch.
    assert dead_ctx.closed is True

    await solver.aclose()


async def test_fetch_via_browser_does_not_retry_on_plain_timeout() -> None:
    """A normal ``TimeoutError`` on ``page.evaluate`` is NOT a dead-driver
    signal — must surface as a single ``BrowserFetchError`` with NO recycle and
    NO second launch. False-positive recycles would silently re-burn a
    Cloudflare solve (cost: ~10s + the cookie cycle, T-04-12) on every transient
    page-load slowdown.

    Implemented as a stub ``evaluate`` that raises ``TimeoutError`` synchronously
    so ``asyncio.wait_for`` doesn't swallow the inner exception.
    """

    class _SlowPage(_FakeFetchPage):
        async def evaluate(self, _script: str, *_args: object) -> object:
            raise TimeoutError("evaluate timed out")

    page = _SlowPage()
    ctx = _FetchContext([page])
    launch_count = 0

    async def _launch() -> object:
        nonlocal launch_count
        launch_count += 1
        return ctx

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)

    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser("https://x/y", extract="return 1;")
    # The wrapper surfaces the original page.evaluate failure unchanged — NOT a
    # dead-driver retry path.
    assert "page.evaluate" in str(exc.value)
    # Single launch, single context use — recycle never fired.
    assert launch_count == 1
    assert ctx.closed is False  # recycle would have closed it
    assert page.closed is True  # finally clause from the single attempt

    await solver.aclose()


async def test_fetch_via_browser_does_not_retry_on_non_dead_driver_error() -> None:
    """A plain RuntimeError without a dead-driver marker (e.g. a thrown JS error
    in the page's ``extract``) must also NOT trigger recycle. Mirrors
    ``test_fetch_via_browser_closes_page_on_evaluate_error`` but adds the
    additional assertion that the lifecycle was not recycled.
    """
    page = _FakeFetchPage(evaluate_error=RuntimeError("DOM read blew up"))
    ctx = _FetchContext([page])
    launch_count = 0

    async def _launch() -> object:
        nonlocal launch_count
        launch_count += 1
        return ctx

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)

    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser("https://x/y", extract="return 1;")
    assert "page.evaluate failed" in str(exc.value)
    # Single launch — no recycle fired.
    assert launch_count == 1
    assert ctx.closed is False

    await solver.aclose()


async def test_fetch_via_browser_second_dead_driver_surfaces_as_fetch_error() -> None:
    """Retry cap: if BOTH attempts die with a dead-driver signal, the wrapper
    raises ``BrowserFetchError`` without a third attempt — the retry-once cap
    is what prevents a hot-loop against a persistently broken driver. Two
    crashes is a clear "browser is broken, give up and let the engine fail
    the job" signal.
    """
    dead1 = _DeadDriverContext()
    dead2 = _DeadDriverContext()
    contexts: list[object] = [dead1, dead2]
    launch_count = 0

    async def _launch() -> object:
        nonlocal launch_count
        launch_count += 1
        return contexts.pop(0)

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)

    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser("https://x/y", extract="return 1;")
    # Both dead contexts were exercised; no third launch.
    assert launch_count == 2
    assert dead1.new_page_calls == 1
    assert dead2.new_page_calls == 1
    # First was recycled; second is dead but the retry-once cap means we don't
    # close it via recycle (only the first crash triggers a recycle).
    assert dead1.closed is True
    # The surfaced error wraps the SECOND attempt's failure (the first was
    # transparently recovered from by the recycle path).
    assert "could not open page for fetch_via_browser" in str(exc.value)
    # The underlying message carries the dead-driver marker so callers (and
    # the engine log) can see this was a driver crash, not a network failure.
    assert _DEAD_DRIVER_MSG in str(exc.value)

    await solver.aclose()


def test_looks_like_dead_driver_matches_top_level_message() -> None:
    """The detector trips on the marker substring appearing on the top-level
    exception. Today's production raise sites (``BrowserFetchError(f"... {exc}")``)
    embed the underlying message into the wrapper string, so this is the
    common path the detector takes.
    """
    err = BrowserFetchError(
        f"could not open page for fetch_via_browser: {_DEAD_DRIVER_MSG}"
    )
    assert _looks_like_dead_driver(err) is True


def test_looks_like_dead_driver_walks_cause_chain() -> None:
    """Regression guard: even if a future refactor stops embedding the
    underlying message into the wrapper, the detector must still recognize
    the dead-driver signal via ``__cause__``. Mirrors the ``raise X from Y``
    discipline already in use throughout ``_fetch_via_browser_once``.
    """
    cause = RuntimeError(f"BrowserContext.new_page: {_DEAD_DRIVER_MSG}")
    wrapper = BrowserFetchError("could not open page for fetch_via_browser")
    wrapper.__cause__ = cause  # opaque wrapper, marker only on cause
    assert _DEAD_DRIVER_MSG not in str(wrapper)
    assert _looks_like_dead_driver(wrapper) is True


def test_looks_like_dead_driver_ignores_unrelated_errors() -> None:
    """Anything without one of the dead-driver markers must NOT trip the
    detector — including plain TimeoutError on evaluate, plain RuntimeError
    on a JS extract failure, and PlaywrightTimeoutError-shaped strings
    (which the prod code translates to ``page.evaluate timed out after Xs``).
    """
    assert _looks_like_dead_driver(TimeoutError("evaluate timed out")) is False
    assert _looks_like_dead_driver(RuntimeError("DOM read blew up")) is False
    assert (
        _looks_like_dead_driver(
            BrowserFetchError("page.evaluate timed out after 30.0s")
        )
        is False
    )


def test_looks_like_dead_driver_handles_cyclic_cause_chain() -> None:
    """Pathological guard: a cyclic ``__cause__`` (a → b → a) must not hang the
    detector. The id-set in the walk terminates after one full pass.
    """
    a = RuntimeError("outer error")
    b = RuntimeError("inner error")
    a.__cause__ = b
    b.__cause__ = a  # cycle
    # Returns False (no marker anywhere) AND terminates — pytest would hang
    # otherwise; a failure here means the loop guard regressed.
    assert _looks_like_dead_driver(a) is False


# ───────────────────── #54 diagnostic browser-event instrumentation ─────────────────
#
# The instrumentation under test attaches ``page.on("pageerror" | "console" |
# "requestfailed")`` handlers in ``_fetch_via_browser_once`` ONLY when
# ``Settings.cloudflare_log_browser_events`` (forwarded as
# ``CloudflareSolver(log_browser_events=...)``) is True. The handlers log
# INFO-level lines for nightly evidence capture investigating the upstream
# Playwright Firefox handler crash (issue #54). Tests below cover:
#
#   1. flag=True → all three handlers registered before goto;
#   2. flag=False (default) → no handlers registered;
#   3. pageerror handler survives a ``location=None`` payload — the very
#      pattern that crashes Playwright's own handler upstream MUST NOT also
#      crash our diagnostic listener (a handler that raised would corrupt
#      its own evidence stream).


async def test_browser_event_handlers_attached_when_flag_true() -> None:
    """When ``log_browser_events=True``, all three handlers register on the
    page BEFORE ``goto`` — early registration matters because we want to
    capture errors thrown during navigation itself, not just post-load."""
    page = _FakeFetchPage(evaluate_result="ok")
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx, log_browser_events=True)

    await solver.fetch_via_browser("https://x/y", extract="return 1;")

    events_registered = [event for event, _ in page.on_calls]
    assert sorted(events_registered) == ["console", "pageerror", "requestfailed"]
    # Exactly one handler per event — no duplicate registration on the single fetch.
    assert len(page.on_calls) == 3

    await solver.aclose()


async def test_browser_event_handlers_not_attached_when_flag_false() -> None:
    """Default (``log_browser_events=False``): NO handlers attached.

    Production must not pay the per-page listener overhead or emit the
    pageerror/console/requestfailed log spam by default — the flag is a
    diagnostic-only opt-in for nightly evidence capture."""
    page = _FakeFetchPage(evaluate_result="ok")
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx)  # log_browser_events defaults to False

    await solver.fetch_via_browser("https://x/y", extract="return 1;")

    # No ``page.on(...)`` call was issued by the production code path.
    assert page.on_calls == []

    await solver.aclose()


async def test_browser_event_pageerror_handler_survives_location_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The diagnostic-safety guarantee: the pageerror handler MUST NOT raise
    when its payload has ``location=None`` — the exact pattern under
    investigation. A handler that crashed on this would (a) defeat the purpose
    (no evidence is captured), (b) silently swallow other still-firing events
    on the same page, and (c) potentially trigger the very driver-crash path
    we're trying to instrument. ``getattr(..., None)`` on every attribute
    read is what protects us; this test asserts the guarantee end-to-end.

    Also covers the matching defensive behavior for the console and
    requestfailed handlers — none of the three handlers may raise on a
    location-less payload, regardless of which surface triggers it."""
    page = _FakeFetchPage(evaluate_result="ok")
    ctx = _FetchContext([page])
    solver = _solver_with_fetch_context(ctx, log_browser_events=True)

    with caplog.at_level(logging.INFO, logger="manga_gateway"):
        await solver.fetch_via_browser(
            "https://comix.to/title/abc/1-chapter-1", extract="return 1;"
        )

        # Payload shape mirrors the #54 trigger: a PageError whose ``location``
        # is None / undefined. ``name`` and ``message`` ARE present but
        # ``stack`` is None to also stress the stack-absent path. Using
        # SimpleNamespace because real Playwright Error objects expose
        # attributes via the same dotted-access surface.
        pageerror_payload = SimpleNamespace(
            name="TypeError",
            message="Script error.",
            stack=None,
            location=None,
        )
        page.fire("pageerror", pageerror_payload)

        # A console message with a None location is also a documented pattern
        # for cross-origin sanitized errors.
        console_payload = SimpleNamespace(
            type="error",
            text="Uncaught (in promise) Script error.",
            location=None,
        )
        page.fire("console", console_payload)

        # A failed request with a None failure dict (Playwright surfaces
        # ``request.failure()`` as a dict-or-None) — same defensive path.
        requestfailed_payload = SimpleNamespace(
            url="https://thirdparty.invalid/widget.js",
            method="GET",
            failure=None,
        )
        page.fire("requestfailed", requestfailed_payload)

    # All three handlers executed without raising (otherwise pytest would
    # have reported the uncaught exception from ``fire``). Confirm the
    # diagnostic stream DID receive each event — proving the handlers ran
    # to completion, not that they early-exited under a try/except that
    # swallowed the work itself.
    info_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.INFO
    ]
    assert any("pageerror" in m and "TypeError" in m for m in info_messages)
    assert any("console" in m and "Script error" in m for m in info_messages)
    assert any("requestfailed" in m and "widget.js" in m for m in info_messages)

    await solver.aclose()
