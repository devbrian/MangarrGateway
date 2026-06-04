"""``/admin/metrics/v1/*`` — the authenticated read-only metrics JSON (OBS-05/06).

A plain ``APIRouter()`` (the prefix ``/admin/metrics/v1`` is applied at include-time
in ``app.py`` so the router is reusable + UrlBase/root_path-aware) with the six locked
endpoints lifted from spike-004 ``dashboard.py`` MINUS the HTML index (D-06 — no UI
shipped). Every handler reads the lifespan-built :class:`InMemoryStore` via
``Depends(get_metric_store)`` and returns ready-made JSON from the in-memory
rollups/rings — reads are flat-cost (O(rollup count) or O(ring), never O(events)),
so frequent polling is cheap (same world as ``GET /downloads``).

Auth: this router is mounted on the app that carries the global
``dependencies=[Depends(require_api_key)]`` (app.py), so EVERY route here inherits
the 401-without-key guard (SEC-01/AUTH-01, T-08-16). ``?limit=`` is clamped with
``Query(ge=1, le=_MAX_LIMIT)`` (T-08-18) and ``request_id`` is ``int``-typed
(rejects malformed). ``operation_id`` is set per route so the runtime
``/openapi.json`` is clean (D-06) — these stay OUT of ``manga-gateway.openapi.yaml``.

The served JSON is already redacted: the URL/error of every ring event was scrubbed
by ``redact_url`` at the collector ingest boundary (Plan 02, Pitfall 5), so no secret
reaches the dashboard (T-08-04).
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_metric_store
from .store import InMemoryStore

router = APIRouter()

# Upper bound on ``?limit=`` (T-08-18). Generous ceiling: the rings are already
# bounded (default recent=500), so a large limit reads at most ring-size events;
# this only rejects absurd/negative input. ge=1 rejects 0 and negatives.
_MAX_LIMIT = 1000
_DEFAULT_LIMIT = 25

LimitQuery = Annotated[int, Query(ge=1, le=_MAX_LIMIT)]
StoreDep = Annotated[InMemoryStore, Depends(get_metric_store)]


@router.get("/summary", operation_id="getMetricsSummary")
async def get_summary(store: StoreDep) -> dict[str, Any]:
    """Top-line totals across every tracked series (the KPI strip)."""
    return store.summary()


@router.get("/per-source-endpoint", operation_id="getPerSourceEndpoint")
async def get_per_source_endpoint(store: StoreDep) -> list[dict[str, Any]]:
    """One rollup row per (source, endpoint) — the core health table."""
    return store.per_source_per_endpoint()


@router.get("/failures", operation_id="getFailures")
async def get_failures(
    store: StoreDep, limit: LimitQuery = _DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """The most recent failed calls, newest first (the failures feed)."""
    return store.latest_failures(limit)


@router.get("/slow", operation_id="getSlow")
async def get_slow(
    store: StoreDep, limit: LimitQuery = _DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """The most recent baseline-relative slow calls, newest first."""
    return store.latest_slow(limit)


@router.get("/recent", operation_id="getMetricsRecent")
async def get_recent(
    store: StoreDep, limit: LimitQuery = _DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """The most recent calls of any outcome, newest first (live activity)."""
    return store.recent_calls(limit)


@router.get("/requests/{request_id}", operation_id="getRequestBreakdown")
async def get_request_calls(store: StoreDep, request_id: int) -> dict[str, Any]:
    """The per-request breakdown: the child calls made under one ``request_id``.

    Builds the ``RequestBreakdown`` envelope the contract documents (request_id +
    surface/endpoint/ts/total_duration_ms/outcome + ordered ``calls[]``) from the
    ``recent`` ring. A ``request_id`` with no retained events (aged out of the ring)
    is a 404 (contract).
    """
    calls = store.calls_for_request(request_id)
    if not calls:
        raise HTTPException(status_code=404, detail="No events retained for request_id")

    first = calls[0]
    # The ``request`` kind event (emitted by the middleware) carries the whole-request
    # wall time + final outcome; child calls carry per-op timings.
    request_event = next((c for c in calls if c.get("kind") == "request"), None)
    total_duration_ms: float | None = None
    outcome: str | None = None
    if request_event is not None:
        total_duration_ms = float(cast("float", request_event["duration_ms"]))
        outcome = str(request_event["outcome"])

    return {
        "request_id": request_id,
        "surface": first.get("surface"),
        "endpoint": first.get("endpoint"),
        "ts": first.get("ts"),
        "total_duration_ms": total_duration_ms,
        "outcome": outcome,
        "calls": calls,
    }
