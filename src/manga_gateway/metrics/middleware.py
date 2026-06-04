"""Pure-ASGI request-scope middleware (OBS-02) — sets ``current_request``.

RESEARCH §Q5: this is a **pure ASGI** middleware class
(``async def __call__(self, scope, receive, send)`` wrapping a captured ``app``),
**NOT** ``BaseHTTPMiddleware``. A value set inside ``BaseHTTPMiddleware.dispatch`` /
``call_next`` runs in a separate anyio-task context copy and will NOT propagate
*inward* to the endpoint, the fan-out children, ``SourceContext._send``, or the
logging filter (Pitfall 2). Setting it here — before ``await self.app(...)`` —
makes ``request_id``/``surface``/``endpoint`` live for the whole request, readable
inward by everything (the OBS-02 propagation guarantee).

The collector is resolved lazily via
:func:`~manga_gateway.metrics.collector.get_collector`
(read at request time, NOT bound at add-time) so the middleware can be installed in
``create_app`` before the lifespan builds the collector. A ``None`` collector makes
``emit_request`` a no-op — the request still gets its contextvar set (so inward log
lines + seam emits are attributed) even before the collector exists.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from .collector import get_collector
from .context import _request_ids, current_request

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# Static path-prefix -> (surface, endpoint-label) map (Open Question 1, RESOLVED).
# Cosmetic labelling derived from the api/routes/* paths; the gateway's own /api/v1
# operations. The admin metrics surface gets its own label. Longest-prefix-first.
_SURFACE_MAP: tuple[tuple[str, tuple[str, str]], ...] = (
    ("/api/v1/search", ("search", "POST /search")),
    ("/api/v1/recent", ("search", "GET /recent")),
    ("/api/v1/caps", ("search", "GET /caps")),
    ("/api/v1/status", ("search", "GET /status")),
    ("/api/v1/version", ("search", "GET /version")),
    ("/api/v1/downloads", ("download", "POST /downloads")),
    ("/admin/metrics", ("admin", "GET /admin/metrics")),
)


def _attribution(path: str) -> tuple[str | None, str | None]:
    """Derive ``(surface, endpoint)`` from the request path (longest-prefix wins).

    Unmatched paths (``/docs``, ``/openapi.json``, an unknown route) attribute to
    ``(None, None)`` — a recordable request with no logical surface label.
    """
    for prefix, (surface, endpoint) in _SURFACE_MAP:
        if path == prefix or path.startswith(prefix + "/"):
            return surface, endpoint
    return None, None


class MetricsRequestMiddleware:
    """Pure-ASGI middleware that sets ``current_request`` for the whole request.

    For every ``http`` scope it mints a monotonic ``request_id``, sets the
    ``current_request`` contextvar (read inward by the seam + logging filter), times
    the request with ``perf_counter``, and in a ``finally`` emits one ``request``
    metric event (via the lazily-resolved collector) and resets the contextvar.
    Non-``http`` scopes (lifespan/websocket) short-circuit untouched.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = next(_request_ids)
        surface, endpoint = _attribution(scope.get("path", ""))
        token = current_request.set(
            {"request_id": request_id, "surface": surface, "endpoint": endpoint}
        )
        status_code = 0

        async def _send(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, _send)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            collector = get_collector()
            if collector is not None:
                outcome = "error" if status_code >= 500 or status_code == 0 else "ok"
                collector.emit_request(
                    endpoint=endpoint,
                    status=status_code or None,
                    outcome=outcome,
                    duration_ms=duration_ms,
                )
            current_request.reset(token)
