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
from .context import current_request, next_request_id

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# Static path-prefix -> (surface, endpoint-label) map (Open Question 1, RESOLVED).
# Cosmetic labelling derived from the api/routes/* paths; the gateway's own /api/v1
# operations. The admin metrics surface gets its own label. Longest-prefix-first.
#
# /api/v1/downloads is METHOD-AWARE (CodeRabbit): the SAME path prefix serves the
# frequent GET poll, the POST submit, the per-id GET status, and the per-id DELETE
# cancel. Hardcoding it to "POST /downloads" mis-attributed every GET poll +
# DELETE as POST, contaminating the endpoint rollups. The ``methods`` mapping below
# resolves (method, path) -> endpoint for those; ``None`` means method-agnostic.
_SURFACE_MAP: tuple[tuple[str, str, dict[str, str] | None, str], ...] = (
    # (prefix, surface, per-method endpoints | None, default endpoint)
    ("/api/v1/search", "search", None, "POST /search"),
    ("/api/v1/recent", "search", None, "GET /recent"),
    ("/api/v1/caps", "search", None, "GET /caps"),
    ("/api/v1/status", "search", None, "GET /status"),
    ("/api/v1/version", "search", None, "GET /version"),
    (
        "/api/v1/downloads",
        "download",
        {
            # Collection: POST submit, GET list (the frequent poll).
            "POST /api/v1/downloads": "POST /downloads",
            "GET /api/v1/downloads": "GET /downloads",
            # Per-id: GET status, DELETE cancel (path is /api/v1/downloads/{id}).
            "GET /api/v1/downloads/*": "GET /downloads/{id}",
            "DELETE /api/v1/downloads/*": "DELETE /downloads/{id}",
        },
        "POST /downloads",
    ),
    ("/admin/metrics", "admin", None, "GET /admin/metrics"),
)

# Endpoint labels excluded from the dashboard's request metrics. The /version
# health probe and /admin/metrics (the dashboard's OWN poll target) are
# operational noise that would otherwise dominate the recent/failures/slow rings
# and the request counters. Excluded requests still get their contextvar set (so
# inward log lines + seam emits stay attributed) — only the per-request emit is
# suppressed.
_METRICS_EXCLUDED_ENDPOINTS: frozenset[str] = frozenset(
    {"GET /version", "GET /admin/metrics"}
)


def _attribution(path: str, method: str = "GET") -> tuple[str | None, str | None]:
    """Derive ``(surface, endpoint)`` from ``(method, path)`` (longest-prefix wins).

    For most routes the endpoint label is method-agnostic. For ``/api/v1/downloads``
    the label is resolved per-method so the GET poll, POST submit, per-id GET status,
    and per-id DELETE cancel get DISTINCT endpoint labels instead of all collapsing
    to ``POST /downloads`` (CodeRabbit).

    Unmatched paths (``/docs``, ``/openapi.json``, an unknown route) attribute to
    ``(None, None)`` — a recordable request with no logical surface label.
    """
    for prefix, surface, methods, default in _SURFACE_MAP:
        if path == prefix or path.startswith(prefix + "/"):
            if methods is None:
                return surface, default
            # Method-aware: distinguish the collection path from a per-id path.
            is_collection = path == prefix
            suffix = "" if is_collection else "/*"
            endpoint = methods.get(f"{method} {prefix}{suffix}")
            return surface, endpoint if endpoint is not None else default
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

        # Mint via the accessor (NOT a by-value import) so the lifespan reseed
        # from MAX(request_id)+1 is observed (restart-monotonic, 260604-wm2).
        request_id = next_request_id()
        surface, endpoint = _attribution(
            scope.get("path", ""), scope.get("method", "GET")
        )
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
            if collector is not None and endpoint not in _METRICS_EXCLUDED_ENDPOINTS:
                # WR-04: a 4xx (401 unauthorized, 404, 422 validation) is NOT a
                # success — it must admit to the failures ring + error_rate so a
                # flood of 401s/404s (the prime observability signal) is visible.
                # The store treats any outcome != "ok" as a failure, so a distinct
                # "client_error" label counts without conflating with 5xx "error".
                if status_code == 0 or status_code >= 500:
                    outcome = "error"
                elif status_code >= 400:
                    outcome = "client_error"
                else:
                    outcome = "ok"
                collector.emit_request(
                    status=status_code or None,
                    outcome=outcome,
                    duration_ms=duration_ms,
                )
            current_request.reset(token)
