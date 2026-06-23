"""Focused unit test for ``CloudflareSolver.fetch_via_browser_typed`` (D-01/D-03).

Drives the real-keyboard browser primitive against a hand-rolled
FakeContext/FakePage double — NO real browser is launched (D-42). The fake page
models the ONE new behavior over ``fetch_via_browser`` (``page.click`` +
``page.keyboard.type(text, delay=...)``) on top of the goto/wait/evaluate surface
the existing one-shot primitive already uses, and records an ordered event log so
the test can prove the post-type ``wait_for`` runs AFTER the keyboard type and
before the ``evaluate`` read.

Coverage (plan Task 2 behavior block):
  1. happy path — click at ``type_selector`` + type ``type_text`` + evaluate
     returns the page-produced (captured vrf) value;
  2. post-type wait — a provided ``wait_for`` runs AFTER keyboard.type, before
     evaluate;
  3. dead-driver retry-once (#54) — a first-attempt dead-driver
     ``BrowserFetchError`` triggers exactly one ``recycle_now()`` + a second
     attempt (attempt=2); a non-dead-driver error raises WITHOUT recycling;
  4. context unavailable — a launch/context failure surfaces as
     ``BrowserFetchError`` containing "context unavailable";
  5. exactly one ``_emit_browser(op="fetch_via_browser_typed")`` per call (ok
     and error branches);
  6. ``fetch_via_browser_typed`` is detectable via ``hasattr`` but is NOT a
     member of the ``runtime_checkable AntiBotSolver`` Protocol (D-41).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest

from manga_gateway.framework import antibot
from manga_gateway.framework.antibot import (
    AntiBotSolver,
    BrowserFetchError,
    CloudflareSolver,
    NoopSolver,
)
from manga_gateway.framework.solver_lifecycle import BrowserLifecycle
from manga_gateway.metrics.collector import Collector, set_collector
from manga_gateway.metrics.context import (
    current_request,
    current_source,
    source_scope,
)
from manga_gateway.metrics.snapshot import MetricSnapshotStore
from manga_gateway.metrics.store import InMemoryStore

from ._metrics_helpers import CapturingRingWriter

_HOME = "https://mangafire.to/home"
_SELECTOR = ".search-inner input[name=keyword]"
_QUERY = "blue lock"
_EXTRACT = "return window.__captured_vrf__;"
_CAPTURED_VRF = "vrf-abc123"
_DEAD_DRIVER_MSG = "Connection closed while reading from the driver"


# ───────────────────────────── fake browser ──────────────────────────────────


class _FakeKeyboard:
    """Models ``page.keyboard.type(text, delay=...)`` — records every call."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.type_calls: list[tuple[str, object]] = []

    async def type(self, text: str, *, delay: float | None = None) -> None:
        self.type_calls.append((text, delay))
        self._log.append("type")


class _FakeTypedPage:
    """A FakePage modeling the typed primitive's goto→click→type→wait→evaluate.

    Records an ordered ``log`` of the interaction so the post-type wait ordering
    is observable, plus the click selector and keyboard.type calls. ``evaluate``
    returns the canned captured value for the extract script.
    """

    def __init__(
        self,
        *,
        evaluate_result: object = _CAPTURED_VRF,
        click_error: Exception | None = None,
        type_error: Exception | None = None,
        evaluate_error: Exception | None = None,
        tracker: _InFlightTracker | None = None,
    ) -> None:
        self.evaluate_result = evaluate_result
        self.click_error = click_error
        self.type_error = type_error
        self.evaluate_error = evaluate_error
        self._tracker = tracker
        self.log: list[str] = []
        self.keyboard = _FakeKeyboard(self.log)
        self.goto_calls: list[tuple[str, dict[str, object]]] = []
        self.click_calls: list[str] = []
        self.wait_function_calls: list[str] = []
        self.wait_selector_calls: list[str] = []
        self.evaluate_scripts: list[str] = []
        self.url = "https://mangafire.to/home"
        self.closed = False

    async def goto(self, url: str, **kwargs: object) -> None:
        self.goto_calls.append((url, kwargs))
        self.log.append("goto")

    async def click(self, selector: str, **_: object) -> None:
        if self.click_error is not None:
            raise self.click_error
        self.click_calls.append(selector)
        self.log.append("click")

    async def wait_for_function(self, predicate: str, **_: object) -> None:
        self.wait_function_calls.append(predicate)
        self.log.append("wait")

    async def wait_for_selector(self, selector: str, **_: object) -> None:
        self.wait_selector_calls.append(selector)
        self.log.append("wait")

    async def evaluate(self, script: str, *_args: object) -> object:
        if self.evaluate_error is not None:
            raise self.evaluate_error
        self.evaluate_scripts.append(script)
        self.log.append("evaluate")
        # Instrument the awaited read so concurrent bodies are observable: a
        # shared tracker brackets the ``await`` point reached only INSIDE the
        # locked body (after click+type), so its ``max_in_flight`` reveals
        # whether two calls overlapped here.
        if self._tracker is not None:
            self._tracker.enter()
            try:
                await asyncio.sleep(0.02)
                return self.evaluate_result
            finally:
                self._tracker.exit()
        return self.evaluate_result

    async def close(self) -> None:
        self.closed = True


class _InFlightTracker:
    """Records the peak number of bodies awaiting concurrently (max_in_flight).

    Mirrors ``tests/test_antibot_parallel_pages.py``: an instrumented awaited
    region calls ``enter()`` / ``await asyncio.sleep(...)`` / ``exit()`` so two
    overlapping calls push ``max_in_flight`` to 2, while serialized calls keep it
    at 1.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    def enter(self) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def exit(self) -> None:
        self.in_flight -= 1


class _FakeTypedContext:
    def __init__(
        self,
        page: Any | None,
        *,
        new_page_error: Exception | None = None,
        page_factory: Any | None = None,
    ) -> None:
        self._page = page
        self.new_page_error = new_page_error
        # When set, each ``new_page()`` mints a FRESH page (so two concurrent
        # calls get separate pages of the one shared warm context), like
        # ``_ParallelContext`` in test_antibot_parallel_pages.py.
        self._page_factory = page_factory
        self.pages: list[Any] = []
        self.closed = False

    async def new_page(self) -> Any:
        if self.new_page_error is not None:
            raise self.new_page_error
        if self._page_factory is not None:
            page = self._page_factory()
            self.pages.append(page)
            return page
        return self._page

    async def cookies(self) -> list[dict[str, str]]:  # pragma: no cover — unused
        return []

    async def close(self) -> None:
        self.closed = True


def _solver_with(
    ctx: _FakeTypedContext, *, fetch_concurrency: int = 1
) -> CloudflareSolver:
    async def _launch() -> _FakeTypedContext:
        return ctx

    async def _solve(_ctx: object, _key: str) -> antibot.Clearance:  # pragma: no cover
        return antibot.Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    return CloudflareSolver(lifecycle=lc, fetch_concurrency=fetch_concurrency)


def _solver_with_contexts(contexts: list[Any]) -> tuple[CloudflareSolver, list[int]]:
    """Build a solver whose lifecycle launches each queued context in turn.

    Returns the solver plus a one-element ``launches`` list tracking how many
    times ``launch`` ran (so the dead-driver recycle+relaunch can be asserted).
    """
    launches = [0]

    async def _launch() -> Any:
        launches[0] += 1
        return contexts.pop(0)

    async def _solve(_ctx: object, _key: str) -> antibot.Clearance:  # pragma: no cover
        return antibot.Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    return CloudflareSolver(lifecycle=lc), launches


@pytest.fixture
def collector() -> Iterator[CapturingRingWriter]:
    ring = CapturingRingWriter()
    c = Collector(
        InMemoryStore(slow_factor=3.0),
        ring_writer=cast("MetricSnapshotStore", ring),
    )
    set_collector(c)
    try:
        yield ring
    finally:
        set_collector(None)
        current_source.set(None)
        current_request.set(None)


# ───────────────────────────── (1) happy path ───────────────────────────────


async def test_typed_clicks_types_and_returns_captured_value() -> None:
    page = _FakeTypedPage(evaluate_result=_CAPTURED_VRF)
    ctx = _FakeTypedContext(page)
    solver = _solver_with(ctx)

    result = await solver.fetch_via_browser_typed(
        _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
    )

    assert result == _CAPTURED_VRF
    assert page.goto_calls and page.goto_calls[0][0] == _HOME
    assert page.click_calls == [_SELECTOR]
    assert [t[0] for t in page.keyboard.type_calls] == [_QUERY]
    # The extract was wrapped by the framework as an async arrow body.
    assert page.evaluate_scripts == ["async () => { " + _EXTRACT + " }"]
    assert page.closed is True
    # Click precedes the keyboard type precedes the evaluate read.
    assert page.log == ["goto", "click", "type", "evaluate"]
    await solver.aclose()


# ─────────────────────────── (2) post-type wait ─────────────────────────────


async def test_typed_wait_for_runs_after_keyboard_type() -> None:
    page = _FakeTypedPage()
    ctx = _FakeTypedContext(page)
    solver = _solver_with(ctx)

    await solver.fetch_via_browser_typed(
        _HOME,
        type_selector=_SELECTOR,
        type_text=_QUERY,
        extract=_EXTRACT,
        wait_for="() => !!window.__captured_vrf__",
    )

    # The wait routed to wait_for_function (JS predicate) and ran AFTER the type,
    # before the evaluate read. The goto did NOT carry the wait (post-type only).
    assert page.wait_function_calls == ["() => !!window.__captured_vrf__"]
    assert page.log == ["goto", "click", "type", "wait", "evaluate"]
    await solver.aclose()


async def test_typed_wait_for_css_selector_routes_to_wait_for_selector() -> None:
    page = _FakeTypedPage()
    ctx = _FakeTypedContext(page)
    solver = _solver_with(ctx)

    await solver.fetch_via_browser_typed(
        _HOME,
        type_selector=_SELECTOR,
        type_text=_QUERY,
        extract=_EXTRACT,
        wait_for=".result-card",
    )
    assert page.wait_selector_calls == [".result-card"]
    assert page.wait_function_calls == []
    assert page.log == ["goto", "click", "type", "wait", "evaluate"]
    await solver.aclose()


# ─────────────────────── (3) dead-driver retry-once ──────────────────────────


async def test_typed_recycles_on_dead_driver_and_retries_once() -> None:
    # First context's new_page raises the dead-driver signal → recycle + retry
    # against a fresh, healthy context which returns the captured value.
    dead = _FakeTypedContext(
        None,
        new_page_error=RuntimeError(f"BrowserContext.new_page: {_DEAD_DRIVER_MSG}"),
    )
    healthy_page = _FakeTypedPage(evaluate_result=_CAPTURED_VRF)
    healthy = _FakeTypedContext(healthy_page)
    solver, launches = _solver_with_contexts([dead, healthy])

    result = await solver.fetch_via_browser_typed(
        _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
    )

    assert result == _CAPTURED_VRF
    assert launches[0] == 2  # original launch + one relaunch after recycle
    assert dead.closed is True  # recycle closed the dead context
    assert healthy_page.closed is True
    await solver.aclose()


async def test_typed_does_not_retry_on_non_dead_driver_error() -> None:
    # A plain click failure (no dead-driver marker) surfaces as BrowserFetchError
    # WITHOUT a recycle/relaunch.
    page = _FakeTypedPage(click_error=RuntimeError("element not found"))
    ctx = _FakeTypedContext(page)
    solver, launches = _solver_with_contexts([ctx])

    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser_typed(
            _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
        )
    assert "click" in str(exc.value)
    assert launches[0] == 1  # no recycle/relaunch
    assert ctx.closed is False
    assert page.closed is True  # finally clause from the single attempt
    await solver.aclose()


# ─────────────────────── (4) context unavailable ─────────────────────────────


async def test_typed_surfaces_context_failure_as_browser_fetch_error() -> None:
    async def _bad_launch() -> object:
        raise RuntimeError("patchright failed to start")

    async def _solve(_ctx: object, _key: str) -> antibot.Clearance:  # pragma: no cover
        return antibot.Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_bad_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)
    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser_typed(
            _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
        )
    assert "context unavailable" in str(exc.value)
    await solver.aclose()


# ─────────────────── (5) exactly one browser metric emit ─────────────────────


@pytest.mark.asyncio
async def test_typed_emits_single_browser_event_ok(
    collector: CapturingRingWriter,
) -> None:
    page = _FakeTypedPage(evaluate_result=_CAPTURED_VRF)
    ctx = _FakeTypedContext(page)
    solver = _solver_with(ctx)
    with source_scope("mangafire"):
        out = await solver.fetch_via_browser_typed(
            _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
        )
    assert out == _CAPTURED_VRF
    evs = [e for e in collector.iter_recent() if e.kind == "browser"]
    assert len(evs) == 1
    assert evs[0].op == "fetch_via_browser_typed"
    assert evs[0].outcome == "ok"
    assert evs[0].source_key == "mangafire"
    await solver.aclose()


@pytest.mark.asyncio
async def test_typed_emits_single_browser_event_error(
    collector: CapturingRingWriter,
) -> None:
    page = _FakeTypedPage(evaluate_error=RuntimeError("evaluate blew up"))
    ctx = _FakeTypedContext(page)
    solver = _solver_with(ctx)
    with source_scope("mangafire"):
        with pytest.raises(BrowserFetchError):
            await solver.fetch_via_browser_typed(
                _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
            )
    evs = [e for e in collector.iter_recent() if e.kind == "browser"]
    assert len(evs) == 1
    assert evs[0].op == "fetch_via_browser_typed"
    assert evs[0].outcome == "error"
    assert evs[0].error is not None
    await solver.aclose()


# ──────────── (7) typed serialization vs cross-primitive concurrency ──────────


async def test_two_concurrent_typed_calls_serialize_at_fetch_concurrency_3() -> None:
    # fetch_concurrency=3 means _browser_lock alone would permit 2+ overlapping
    # bodies — so any serialization observed here comes PURELY from _typed_lock.
    tracker = _InFlightTracker()
    ctx = _FakeTypedContext(None, page_factory=lambda: _FakeTypedPage(tracker=tracker))
    solver = _solver_with(ctx, fetch_concurrency=3)

    async def _typed() -> object:
        return await solver.fetch_via_browser_typed(
            _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
        )

    results = await asyncio.gather(_typed(), _typed())

    # _typed_lock forced the two typed bodies sequential despite fetch_concurrency=3.
    # Without the lock (Semaphore(3) only) this would observe max_in_flight == 2.
    assert tracker.max_in_flight == 1
    assert results == [_CAPTURED_VRF, _CAPTURED_VRF]
    await solver.aclose()


async def test_one_shot_fetch_overlaps_a_typed_call_at_fetch_concurrency_3() -> None:
    # The one-shot fetch_via_browser is NOT gated by _typed_lock — bounded only by
    # _browser_lock (Semaphore(3) here), it overlaps a concurrent typed call.
    tracker = _InFlightTracker()
    ctx = _FakeTypedContext(None, page_factory=lambda: _FakeTypedPage(tracker=tracker))
    solver = _solver_with(ctx, fetch_concurrency=3)

    async def _typed() -> object:
        return await solver.fetch_via_browser_typed(
            _HOME, type_selector=_SELECTOR, type_text=_QUERY, extract=_EXTRACT
        )

    async def _one_shot() -> object:
        return await solver.fetch_via_browser(_HOME, extract=_EXTRACT)

    results = await asyncio.gather(_typed(), _one_shot())

    # Both bodies reach the instrumented evaluate at the same time — the one-shot
    # is free to overlap the typed call (only _browser_lock bounds it).
    assert tracker.max_in_flight == 2
    assert results == [_CAPTURED_VRF, _CAPTURED_VRF]
    await solver.aclose()


# ─────────────── (6) off-Protocol seam (D-41) — hasattr, not Protocol ─────────


def test_typed_is_detectable_via_hasattr_but_off_protocol() -> None:
    page = _FakeTypedPage()
    ctx = _FakeTypedContext(page)
    solver = _solver_with(ctx)
    # Detectable on the concrete solver via hasattr (the source's gate).
    assert hasattr(solver, "fetch_via_browser_typed")
    # But OFF the runtime_checkable Protocol — NoopSolver (only get_clearance)
    # still satisfies AntiBotSolver, proving the typed primitive is not a member.
    assert isinstance(NoopSolver(), AntiBotSolver)
    assert not hasattr(NoopSolver(), "fetch_via_browser_typed")
