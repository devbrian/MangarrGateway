"""The framework metrics capture seam (Plan 08-04 — OBS-01/02/05).

Proves the spike-003 bet: THREE edits (source-scope bind in ``fanout._guarded`` +
``emit_http`` at the one ``SourceContext._send`` choke point) plus a handful of
additive ``emit_*`` calls instrument the whole gateway with zero per-source code,
and — critically — concurrent sibling sources NEVER cross-attribute their metrics
(T-08-12, the OBS-02 guarantee).

Network-free: the http path drives a ``SourceContext`` over a recording fake
transport (mirroring ``tests/test_context_get_json_array.py``); the solve / job /
package emits drive their own choke points with fakes. A real ``Collector`` +
``InMemoryStore`` is installed via ``set_collector`` so each emitted event's
attribution is read back from ``iter_recent()``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import httpx
import pytest

from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.fanout import fan_out
from manga_gateway.handles.store import HandleStore
from manga_gateway.metrics.collector import Collector, set_collector
from manga_gateway.metrics.context import current_source
from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.store import InMemoryStore

# ───────────────────────────── transport / source fakes ──────────────────────────


class _RecordingTransport:
    """Fake Transport returning queued responses (mirrors test_context_*)."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        # Yield control so concurrent siblings genuinely interleave (proves the
        # contextvar isolation under real task switching, not just sequentially).
        await asyncio.sleep(0)
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _FakeSource:
    def __init__(self, key: str) -> None:
        self.key = key


def _ctx(source_key: str, transport: _RecordingTransport) -> SourceContext:
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager

    return SourceContext(
        source_key=source_key,
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
    )


@pytest.fixture
def collector() -> Iterator[tuple[Collector, InMemoryStore]]:
    """Install a real Collector + store for the duration of one test, then clear."""
    store = InMemoryStore(
        recent_max=500, failures_max=200, slow_max=200, slow_factor=3.0
    )
    c = Collector(store)
    set_collector(c)
    try:
        yield c, store
    finally:
        set_collector(None)
        # reset the source contextvar in case a test left it bound
        current_source.set(None)


def _http_events(store: InMemoryStore) -> list[MetricEvent]:
    return [e for e in store.iter_recent() if e.kind == "http"]


@contextlib.contextmanager
def _source_bound(key: str) -> Iterator[None]:
    """Bind ``current_source`` outside a fan-out (the error test calls ``get_bytes``
    directly, not through ``_guarded``)."""
    token = current_source.set(key)
    try:
        yield
    finally:
        current_source.reset(token)


# ─────────────────── source-scope attribution under concurrent fan-out ────────────


@pytest.mark.asyncio
async def test_concurrent_fanout_never_cross_attributes_http_source_key(
    collector: tuple[Collector, InMemoryStore],
) -> None:
    """Each http event's ``source_key`` matches the source that produced it (T-08-12).

    Two sources fan out concurrently; each makes several ``get_json`` calls through
    its OWN ``SourceContext``. Because ``_guarded`` binds ``current_source`` per Task
    and asyncio copies the context per Task, sibling sources can interleave freely
    (``transport.request`` yields) yet never clobber each other's attribution.
    """
    _c, store = collector
    n_calls = 6
    ctx_a = _ctx(
        "alpha",
        _RecordingTransport(
            [
                httpx.Response(
                    200, json={"ok": True}, request=httpx.Request("GET", "u")
                )
                for _ in range(n_calls)
            ]
        ),
    )
    ctx_b = _ctx(
        "beta",
        _RecordingTransport(
            [
                httpx.Response(
                    200, json={"ok": True}, request=httpx.Request("GET", "u")
                )
                for _ in range(n_calls)
            ]
        ),
    )
    ctx_by_key = {"alpha": ctx_a, "beta": ctx_b}
    url_by_key = {"alpha": "https://alpha.test/api", "beta": "https://beta.test/api"}

    async def run_one(src: _FakeSource) -> list[None]:
        ctx = ctx_by_key[src.key]
        url = url_by_key[src.key]
        for _ in range(n_calls):
            await ctx.get_json(url)
        return []

    await fan_out([_FakeSource("alpha"), _FakeSource("beta")], run_one)

    events = _http_events(store)
    assert len(events) == 2 * n_calls
    # EVERY event self-attributes to the source whose URL it carried — no crossing.
    for ev in events:
        assert ev.url is not None
        host = "alpha" if "alpha.test" in ev.url else "beta"
        assert ev.source_key == host, (
            f"http event for {ev.url} mis-attributed to {ev.source_key}"
        )
    # both sources are represented (sanity: the fan-out actually ran both)
    assert {e.source_key for e in events} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_no_collector_is_a_noop() -> None:
    """With no collector installed, ``_send`` is byte-for-byte unchanged (no emit)."""
    set_collector(None)
    transport = _RecordingTransport(
        [httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "u"))]
    )
    ctx = _ctx("alpha", transport)
    out = await ctx.get_json("https://alpha.test/api")
    assert out == {"ok": True}
    assert len(transport.calls) == 1  # the request still went out exactly once


@pytest.mark.asyncio
async def test_http_error_emits_error_outcome_then_reraises(
    collector: tuple[Collector, InMemoryStore],
) -> None:
    """A transport error emits ``outcome="error"`` then re-raises the original exc."""
    _c, store = collector

    class _Boom(_RecordingTransport):
        async def request(
            self, method: str, url: str, **kwargs: object
        ) -> httpx.Response:
            self.calls.append({"method": method, "url": url})
            raise httpx.ConnectError("boom")

    ctx = _ctx("alpha", _Boom([]))
    with _source_bound("alpha"):
        with pytest.raises(httpx.ConnectError):
            # get_bytes retries via tenacity (4 attempts) then reraises
            await ctx.get_bytes("https://alpha.test/img.webp")

    errs = [e for e in _http_events(store) if e.outcome == "error"]
    assert errs, "an error outcome http event should have been emitted"
    assert all(e.source_key == "alpha" for e in errs)
    assert all(e.error is not None for e in errs)
