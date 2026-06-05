"""comix browser-read ring events (#125 / 260605-e9a Task 2).

The browser-read metric is emitted at the ``CloudflareSolver._fetch_via_browser_once``
seam (so EVERY browser-based source is covered with no per-source code). These tests
drive the REAL ``CloudflareSolver.fetch_via_browser`` over an injected
``BrowserLifecycle`` whose ``launch`` returns a FAKE Playwright context/page (no real
browser, D-42), inside a ``source_scope("comix")`` with ``current_request`` set, and
assert a ``kind="browser"`` ring event with source_key/url/outcome/attempt/request_id
attribution — plus a ``BrowserFetchError`` failure case emitting ``outcome="error"``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from manga_gateway.framework.antibot import BrowserFetchError, CloudflareSolver
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

_COMIX_URL = "https://comix.to/title/mr3m0-some-slug?cf_clearance=LEAKED"


# ───────────────────────────── fake browser ──────────────────────────────────


class _FakePage:
    def __init__(self, *, result: object, raise_on_goto: bool = False) -> None:
        self._result = result
        self._raise_on_goto = raise_on_goto
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        if self._raise_on_goto:
            raise RuntimeError("nav exploded")

    async def wait_for_selector(self, sel: str, **kwargs: Any) -> None:
        pass

    async def wait_for_function(self, fn: str, **kwargs: Any) -> None:
        pass

    async def evaluate(self, script: str) -> object:
        return self._result

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, *, result: object, raise_on_goto: bool = False) -> None:
        self._result = result
        self._raise_on_goto = raise_on_goto

    async def new_page(self) -> _FakePage:
        return _FakePage(result=self._result, raise_on_goto=self._raise_on_goto)


def _solver_with_fake_context(
    *, result: object, raise_on_goto: bool = False
) -> CloudflareSolver:
    """Build a real CloudflareSolver whose lifecycle launches a FAKE context."""

    async def _launch() -> object:
        return _FakeContext(result=result, raise_on_goto=raise_on_goto)

    async def _solve(_ctx: object, _key: str) -> Any:  # never called in these tests
        raise AssertionError("solve should not run for a browser fetch")

    lifecycle = BrowserLifecycle(launch=_launch, solve=_solve)
    return CloudflareSolver(cloudflare_keys=("comix",), lifecycle=lifecycle)


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


# ───────────────────────────── success ───────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_via_browser_emits_browser_event_ok(
    collector: CapturingRingWriter,
) -> None:
    solver = _solver_with_fake_context(result=[{"id": "c1", "chapter": "1"}])
    token = current_request.set(
        {"request_id": 7, "surface": "search", "endpoint": "POST /search"}
    )
    try:
        with source_scope("comix"):
            out = await solver.fetch_via_browser(
                _COMIX_URL, extract="return [];", wait_for=".mchap-row"
            )
    finally:
        current_request.reset(token)

    assert out == [{"id": "c1", "chapter": "1"}]
    evs = [e for e in collector.iter_recent() if e.kind == "browser"]
    assert len(evs) == 1
    ev = evs[0]
    assert ev.source_key == "comix"
    assert ev.request_id == 7
    assert ev.outcome == "ok"
    assert ev.attempt == 1
    assert ev.url is not None
    assert "comix.to" in ev.url
    assert "LEAKED" not in ev.url  # redacted at the ingest boundary
    assert ev.duration_ms >= 0.0


# ───────────────────────────── failure ───────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_via_browser_emits_browser_event_error(
    collector: CapturingRingWriter,
) -> None:
    solver = _solver_with_fake_context(result=None, raise_on_goto=True)
    token = current_request.set(
        {"request_id": 9, "surface": "search", "endpoint": "POST /search"}
    )
    try:
        with source_scope("comix"):
            with pytest.raises(BrowserFetchError):
                await solver.fetch_via_browser(_COMIX_URL, extract="return [];")
    finally:
        current_request.reset(token)

    evs = [e for e in collector.iter_recent() if e.kind == "browser"]
    assert len(evs) == 1
    assert evs[0].outcome == "error"
    assert evs[0].source_key == "comix"
    assert evs[0].error is not None


@pytest.mark.asyncio
async def test_fetch_via_browser_noop_without_collector() -> None:
    """With no collector installed, fetch_via_browser is byte-for-byte unchanged."""
    set_collector(None)
    solver = _solver_with_fake_context(result=[1, 2, 3])
    out = await solver.fetch_via_browser(_COMIX_URL, extract="return [];")
    assert out == [1, 2, 3]
