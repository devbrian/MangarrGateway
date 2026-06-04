"""Pure-ASGI ``MetricsRequestMiddleware`` — the OBS-02 inward-propagation guarantee.

Proves the contextvar set in the middleware's ``__call__`` is readable INWARD (the
direction ``BaseHTTPMiddleware`` would break, Pitfall 2): a fake downstream ASGI app
calls ``get_collector().emit_*`` and the resulting event carries the middleware's
``request_id``/``surface``/``endpoint``. Also covers the non-http short-circuit, the
emit_request-in-finally + reset, and the static surface/endpoint map.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest

from manga_gateway.metrics.collector import Collector, get_collector, set_collector
from manga_gateway.metrics.context import current_request
from manga_gateway.metrics.middleware import MetricsRequestMiddleware, _attribution
from manga_gateway.metrics.store import InMemoryStore


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(
        recent_max=100, failures_max=100, slow_max=100, slow_factor=3.0
    )


@pytest.fixture
def collector(store: InMemoryStore) -> Iterator[Collector]:
    coll = Collector(store=store)
    set_collector(coll)
    yield coll
    set_collector(None)


def _http_scope(path: str) -> MutableMapping[str, Any]:
    return {"type": "http", "path": path}


async def _noop_receive() -> MutableMapping[str, Any]:
    return {"type": "http.request"}


def _make_send() -> Any:
    async def _send(message: MutableMapping[str, Any]) -> None:
        return None

    return _send


@pytest.mark.asyncio
async def test_contextvar_readable_inward_via_collector_emit(
    store: InMemoryStore, collector: Collector
) -> None:
    """A downstream app reads the contextvar through a collector emit (OBS-02)."""
    seen: dict[str, Any] = {}

    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        # Emit an http event from "inside" the request — the collector reads
        # current_request, so the event must carry the middleware's attribution.
        get_collector().emit_http(  # type: ignore[union-attr]
            op="get_json",
            method="GET",
            url="https://x.example/a",
            status=200,
            outcome="ok",
            duration_ms=5.0,
            attempt=1,
        )
        seen["req"] = current_request.get()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsRequestMiddleware(downstream)
    await mw(_http_scope("/api/v1/recent"), _noop_receive, _make_send())

    # The contextvar was set while downstream ran.
    assert seen["req"] is not None
    assert seen["req"]["surface"] == "search"
    assert seen["req"]["endpoint"] == "GET /recent"

    # The http event ingested inward carries the middleware's request_id + surface.
    events = [e for e in store.recent_calls(10) if e["kind"] == "http"]
    assert len(events) == 1
    assert events[0]["surface"] == "search"
    assert events[0]["endpoint"] == "GET /recent"
    assert isinstance(events[0]["request_id"], int)


@pytest.mark.asyncio
async def test_emits_request_event_and_resets_contextvar(
    store: InMemoryStore, collector: Collector
) -> None:
    """A ``request`` event is emitted in finally; the contextvar resets after."""

    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsRequestMiddleware(downstream)
    await mw(_http_scope("/api/v1/status"), _noop_receive, _make_send())

    request_events = [e for e in store.recent_calls(10) if e["kind"] == "request"]
    assert len(request_events) == 1
    assert request_events[0]["outcome"] == "ok"
    assert request_events[0]["status"] == 200
    # Contextvar reset to the top-level default after the request.
    assert current_request.get() is None


@pytest.mark.asyncio
async def test_500_response_marks_request_error(
    store: InMemoryStore, collector: Collector
) -> None:
    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        await send({"type": "http.response.start", "status": 500})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsRequestMiddleware(downstream)
    await mw(_http_scope("/api/v1/search"), _noop_receive, _make_send())

    request_events = [e for e in store.recent_calls(10) if e["kind"] == "request"]
    assert request_events[0]["outcome"] == "error"
    assert request_events[0]["status"] == 500


@pytest.mark.asyncio
async def test_non_http_scope_short_circuits() -> None:
    """A lifespan/websocket scope passes straight through, untouched."""
    called: dict[str, Any] = {}

    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        called["scope_type"] = scope["type"]

    mw = MetricsRequestMiddleware(downstream)
    await mw({"type": "lifespan"}, _noop_receive, _make_send())
    assert called["scope_type"] == "lifespan"
    # No request scope set for a non-http scope.
    assert current_request.get() is None


@pytest.mark.asyncio
async def test_no_collector_is_noop_but_contextvar_still_set() -> None:
    """With no collector installed the request still gets its contextvar (for logs)."""
    set_collector(None)
    seen: dict[str, Any] = {}

    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        seen["req"] = current_request.get()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsRequestMiddleware(downstream)
    await mw(_http_scope("/admin/metrics/v1/summary"), _noop_receive, _make_send())
    assert seen["req"] is not None
    assert seen["req"]["surface"] == "admin"
    assert current_request.get() is None


def test_attribution_map() -> None:
    assert _attribution("/api/v1/search") == ("search", "POST /search")
    assert _attribution("/api/v1/recent") == ("search", "GET /recent")
    assert _attribution("/api/v1/downloads/abc") == ("download", "POST /downloads")
    assert _attribution("/admin/metrics/v1/summary") == ("admin", "GET /admin/metrics")
    assert _attribution("/docs") == (None, None)
    assert _attribution("/openapi.json") == (None, None)
