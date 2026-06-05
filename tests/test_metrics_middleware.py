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


def _http_scope(path: str, method: str = "GET") -> MutableMapping[str, Any]:
    return {"type": "http", "path": path, "method": method}


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
async def test_4xx_response_marks_request_client_error(
    store: InMemoryStore, collector: Collector
) -> None:
    """WR-04: a 4xx (e.g. 401) is a non-ok outcome so it reaches the failures
    ring / error_rate, not silently counted as success."""

    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        await send({"type": "http.response.start", "status": 401})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsRequestMiddleware(downstream)
    await mw(_http_scope("/api/v1/search"), _noop_receive, _make_send())

    request_events = [e for e in store.recent_calls(10) if e["kind"] == "request"]
    assert request_events[0]["outcome"] == "client_error"
    assert request_events[0]["status"] == 401
    # A non-ok outcome must have admitted the event to the failures ring.
    failures = [e for e in store.latest_failures(10) if e["kind"] == "request"]
    assert any(e["status"] == 401 for e in failures)


@pytest.mark.asyncio
async def test_get_vs_post_downloads_attribute_distinctly(
    store: InMemoryStore, collector: Collector
) -> None:
    """End-to-end through the middleware: a GET poll and a POST submit of
    /api/v1/downloads carry DISTINCT endpoint labels on their request events."""

    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsRequestMiddleware(downstream)
    await mw(_http_scope("/api/v1/downloads", "GET"), _noop_receive, _make_send())
    await mw(_http_scope("/api/v1/downloads", "POST"), _noop_receive, _make_send())

    request_events = [e for e in store.recent_calls(10) if e["kind"] == "request"]
    endpoints = {e["endpoint"] for e in request_events}
    assert endpoints == {"GET /downloads", "POST /downloads"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_endpoint"),
    [
        ("/api/v1/version", "GET /version"),
        ("/admin/metrics/v1/summary", "GET /admin/metrics"),
    ],
)
async def test_excluded_endpoints_emit_no_request_event(
    store: InMemoryStore,
    collector: Collector,
    path: str,
    expected_endpoint: str,
) -> None:
    """The /version probe and /admin/metrics poll are excluded from dashboard
    metrics: no ``request`` event is emitted, but the contextvar is still set
    (for inward log attribution) and reset afterwards."""
    seen: dict[str, Any] = {}

    async def downstream(
        scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        seen["req"] = current_request.get()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    mw = MetricsRequestMiddleware(downstream)
    await mw(_http_scope(path), _noop_receive, _make_send())

    # Contextvar was set inward with the expected attribution, then reset.
    assert seen["req"] is not None
    assert seen["req"]["endpoint"] == expected_endpoint
    assert current_request.get() is None

    # No request event reached the dashboard rings.
    request_events = [e for e in store.recent_calls(10) if e["kind"] == "request"]
    assert request_events == []


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
    assert _attribution("/api/v1/search", "POST") == ("search", "POST /search")
    assert _attribution("/api/v1/recent", "GET") == ("search", "GET /recent")
    assert _attribution("/admin/metrics/v1/summary", "GET") == (
        "admin",
        "GET /admin/metrics",
    )
    assert _attribution("/docs", "GET") == (None, None)
    assert _attribution("/openapi.json", "GET") == (None, None)


def test_downloads_attribution_is_method_aware() -> None:
    """CodeRabbit: GET /downloads (poll), POST /downloads (submit), per-id GET
    (status) and per-id DELETE (cancel) get DISTINCT endpoint labels instead of all
    collapsing to "POST /downloads"."""
    # Collection path: POST submit vs GET poll are distinct.
    assert _attribution("/api/v1/downloads", "POST") == ("download", "POST /downloads")
    assert _attribution("/api/v1/downloads", "GET") == ("download", "GET /downloads")
    # Per-id path: GET status vs DELETE cancel are distinct.
    assert _attribution("/api/v1/downloads/abc", "GET") == (
        "download",
        "GET /downloads/{id}",
    )
    assert _attribution("/api/v1/downloads/abc", "DELETE") == (
        "download",
        "DELETE /downloads/{id}",
    )
    # An unmapped method on the collection falls back to the default label
    # (still the download surface, never (None, None)).
    assert _attribution("/api/v1/downloads", "PUT")[0] == "download"
