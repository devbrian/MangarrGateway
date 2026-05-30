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

import pytest

from manga_gateway.framework.antibot import (
    AntiBotSolver,
    BrowserFetchError,
    Clearance,
    CloudflareSolver,
)
from manga_gateway.framework.decrypt import DecryptError
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


# ─────────────────────── D-45 browser-evaluated decrypt ───────────────────────


class _FakeDecryptPage:
    """Mocks the warm comix.to page; ``evaluate`` returns the staged plaintext."""

    def __init__(
        self,
        *,
        plaintexts: dict[str, str] | None = None,
        evaluate_error: Exception | None = None,
    ) -> None:
        self.plaintexts: dict[str, str] = plaintexts or {}
        self.evaluate_error = evaluate_error
        self.closed = False
        self.evaluate_calls: list[str] = []
        self.concurrent_evaluates = 0
        self.max_concurrent_evaluates = 0

    async def evaluate(self, _script: str, arg: str) -> str:
        self.concurrent_evaluates += 1
        self.max_concurrent_evaluates = max(
            self.max_concurrent_evaluates, self.concurrent_evaluates
        )
        try:
            await asyncio.sleep(0.01)  # widen window so serialization is observable
            if self.evaluate_error is not None:
                raise self.evaluate_error
            self.evaluate_calls.append(arg)
            return self.plaintexts.get(arg, arg + "-decrypted")
        finally:
            self.concurrent_evaluates -= 1

    async def close(self) -> None:
        self.closed = True


def _solver_with_decrypt_page(
    page: _FakeDecryptPage | None = None,
    *,
    warm_error: Exception | None = None,
) -> tuple[CloudflareSolver, FakeBrowser, _FakeDecryptPage | None]:
    """Build a CloudflareSolver wired to a mocked browser + fake decrypt page."""
    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    the_page = page if page is not None else _FakeDecryptPage()

    async def _factory(_context: object, _url: str) -> _FakeDecryptPage:
        if warm_error is not None:
            raise warm_error
        return the_page

    solver = CloudflareSolver(lifecycle=lc, decrypt_page_factory=_factory)
    return solver, fake, the_page if warm_error is None else None


async def test_decrypt_returns_page_evaluate_result() -> None:
    page = _FakeDecryptPage(plaintexts={'{"e":"abc"}': '{"pages":["x"]}'})
    solver, _, _ = _solver_with_decrypt_page(page)
    out = await solver.decrypt(b'{"e":"abc"}')
    assert out == b'{"pages":["x"]}'
    assert page.evaluate_calls == ['{"e":"abc"}']
    await solver.aclose()


async def test_decrypt_serializes_concurrent_calls() -> None:
    page = _FakeDecryptPage()
    solver, _, _ = _solver_with_decrypt_page(page)
    await asyncio.gather(
        *(solver.decrypt(b"cipher-" + str(i).encode()) for i in range(5))
    )
    # page.evaluate must never be called in parallel on a single page.
    assert page.max_concurrent_evaluates <= 1
    await solver.aclose()


async def test_decrypt_warm_page_failure_raises_decrypt_error() -> None:
    solver, _, _ = _solver_with_decrypt_page(warm_error=RuntimeError("nav timeout"))
    with pytest.raises(DecryptError) as exc:
        await solver.decrypt(b"cipher")
    assert "decrypt page warm failed" in str(exc.value)
    await solver.aclose()


async def test_decrypt_translates_js_error_to_decrypt_error() -> None:
    page = _FakeDecryptPage(evaluate_error=RuntimeError("cipher VM threw"))
    solver, _, _ = _solver_with_decrypt_page(page)
    with pytest.raises(DecryptError) as exc:
        await solver.decrypt(b"cipher")
    assert "browser-eval decrypt failed" in str(exc.value)
    await solver.aclose()


async def test_decrypt_entry_point_missing_self_diagnoses() -> None:
    """If globalThis.t is undefined the JS script throws an explicit message we
    re-raise verbatim so the first live-smoke failure is self-diagnosing."""
    page = _FakeDecryptPage(
        evaluate_error=RuntimeError(
            "browser-eval decrypt entry point not found; expected globalThis.t; "
            'async-fn candidates=["a","b"]'
        )
    )
    solver, _, _ = _solver_with_decrypt_page(page)
    with pytest.raises(DecryptError) as exc:
        await solver.decrypt(b"cipher")
    assert "entry point not found" in str(exc.value)
    assert "candidates" in str(exc.value)
    await solver.aclose()


async def test_aclose_closes_decrypt_page() -> None:
    page = _FakeDecryptPage()
    solver, _, _ = _solver_with_decrypt_page(page)
    await solver.decrypt(b"cipher")
    assert page.closed is False
    await solver.aclose()
    assert page.closed is True


async def test_decrypt_page_reused_across_calls() -> None:
    """The warm page is created once and reused — opening a new page per call
    would re-load secure-*.js every time (slow + a re-fingerprinting risk)."""
    page = _FakeDecryptPage()
    factory_calls = 0

    async def _factory(_ctx: object, _url: str) -> _FakeDecryptPage:
        nonlocal factory_calls
        factory_calls += 1
        return page

    fake = FakeBrowser()
    lc = BrowserLifecycle(launch=fake.launch, solve=fake.solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc, decrypt_page_factory=_factory)
    await solver.decrypt(b"a")
    await solver.decrypt(b"b")
    await solver.decrypt(b"c")
    assert factory_calls == 1
    await solver.aclose()


# ─────────────────────── Option A: fetch_via_browser primitive ──────────────────


class _FakeFetchPage:
    """A FakePage for ``fetch_via_browser`` — records goto/wait/evaluate/close
    and lets each test stage the return value or an error per stage."""

    def __init__(
        self,
        *,
        evaluate_result: object = None,
        goto_error: Exception | None = None,
        wait_function_error: Exception | None = None,
        wait_selector_error: Exception | None = None,
        evaluate_error: Exception | None = None,
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
        try:
            await asyncio.sleep(0.01)  # widen window so serialization is observable
            if self.evaluate_error is not None:
                raise self.evaluate_error
            self.evaluate_calls.append(script)
            return self.evaluate_result
        finally:
            self.concurrent_calls -= 1

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

    async def new_page(self) -> _FakeFetchPage:
        if self.new_page_error is not None:
            raise self.new_page_error
        if not self._pages:  # pragma: no cover — test wiring guard
            raise AssertionError("no more queued fetch pages")
        page = self._pages.pop(0)
        self.opened.append(page)
        return page

    async def cookies(self) -> list[dict[str, str]]:  # pragma: no cover - unused here
        return []

    async def close(self) -> None:
        self.closed = True


def _solver_with_fetch_context(ctx: _FetchContext) -> CloudflareSolver:
    """Build a CloudflareSolver whose lifecycle yields the given fetch context.

    The lifecycle's ``solve`` callable is never invoked by these tests (they
    exercise ``fetch_via_browser`` directly), but we still supply a stub that
    would record a Clearance if called.
    """

    async def _launch() -> _FetchContext:
        return ctx

    async def _solve(_ctx: object) -> Clearance:  # pragma: no cover — unused
        return Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    return CloudflareSolver(lifecycle=lc)


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


async def test_fetch_via_browser_serializes_with_decrypt_lock() -> None:
    """fetch_via_browser shares the decrypt lock so a concurrent burst opens a
    fresh page per call but never runs more than one page.evaluate in parallel
    — Patchright is not parallel-safe on a single context's pages under the
    same fingerprint, and queueing here protects the "minimize fingerprinting
    events" invariant (Pitfall 6)."""
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
    # Each page saw at most one in-flight evaluate (the lock serializes the
    # whole goto+wait+evaluate path, so no two pages can race in this fake).
    assert all(p.max_concurrent_calls <= 1 for p in pages)
    # One fresh page per call (a page is opened and closed; the decrypt page
    # is the one that's reused).
    assert len(ctx.opened) == 5
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
