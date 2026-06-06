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

The four event-feed endpoints (``getMetricsRecent``/``getFailures``/``getSlow`` →
``list[MetricEventOut]``; ``getRequestBreakdown`` → ``RequestBreakdownOut``) carry a
``response_model`` so the runtime ``/openapi.json`` documents the snake_case event
shape. The models keep snake_case fields with NO aliases and set ``extra="allow"``,
so the served bytes are byte-identical to the raw ``json.loads(payload)`` echo — the
``response_model`` documents the shape without altering it (see ``models/metrics.py``).
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_metric_ring_store, get_metric_store
from ..models.metrics import MetricEventOut, RequestBreakdownOut
from .snapshot import MetricSnapshotStore
from .store import InMemoryStore

router = APIRouter()

# Upper bound on ``?limit=`` (T-08-18). The disk ring read is a single indexed
# query with this LIMIT (poll-friendly); ge=1 rejects 0 and negatives.
_MAX_LIMIT = 1000
_DEFAULT_LIMIT = 25

LimitQuery = Annotated[int, Query(ge=1, le=_MAX_LIMIT)]
# 260606-0mq: default False = filter unrouted noise (events whose ``endpoint`` is
# null — 404s to unknown paths, /openapi.json, /docs, admin-metrics polling) out of
# the three event feeds. ``?include_unrouted=true`` restores the full output. The
# filter is applied IN SQL in the store so LIMIT bounds the filtered set.
IncludeUnroutedQuery = Annotated[
    bool,
    Query(
        description=(
            "Include unrouted events (endpoint=null: 404s to unknown paths, "
            "/openapi.json, /docs, admin-metrics polling). Default false hides them."
        )
    ),
]
StoreDep = Annotated[InMemoryStore, Depends(get_metric_store)]
# The disk ring store (recent/failures/slow/requests-{id}); ``None`` in the
# degraded mode where the snapshot DB could not be opened — handlers then return
# []/404, never 500 (260604-wm2).
RingDep = Annotated["MetricSnapshotStore | None", Depends(get_metric_ring_store)]


@router.get("/summary", operation_id="getMetricsSummary")
async def get_summary(store: StoreDep) -> dict[str, Any]:
    """Top-line totals across every tracked series (the KPI strip)."""
    return store.summary()


@router.get("/per-source-endpoint", operation_id="getPerSourceEndpoint")
async def get_per_source_endpoint(store: StoreDep) -> list[dict[str, Any]]:
    """One rollup row per (source, endpoint) — the core health table."""
    return store.per_source_per_endpoint()


@router.get(
    "/failures",
    operation_id="getFailures",
    response_model=list[MetricEventOut],
)
async def get_failures(
    ring: RingDep,
    limit: LimitQuery = _DEFAULT_LIMIT,
    include_unrouted: IncludeUnroutedQuery = False,
) -> list[dict[str, Any]]:
    """The most recent failed calls, newest first (the failures feed)."""
    if ring is None:
        return []
    return await ring.latest_failures(limit, include_unrouted=include_unrouted)


@router.get(
    "/slow",
    operation_id="getSlow",
    response_model=list[MetricEventOut],
)
async def get_slow(
    ring: RingDep,
    limit: LimitQuery = _DEFAULT_LIMIT,
    include_unrouted: IncludeUnroutedQuery = False,
) -> list[dict[str, Any]]:
    """The most recent baseline-relative slow calls, newest first."""
    if ring is None:
        return []
    return await ring.latest_slow(limit, include_unrouted=include_unrouted)


@router.get(
    "/recent",
    operation_id="getMetricsRecent",
    response_model=list[MetricEventOut],
)
async def get_recent(
    ring: RingDep,
    limit: LimitQuery = _DEFAULT_LIMIT,
    include_unrouted: IncludeUnroutedQuery = False,
) -> list[dict[str, Any]]:
    """The most recent calls of any outcome, newest first (live activity)."""
    if ring is None:
        return []
    return await ring.recent_calls(limit, include_unrouted=include_unrouted)


@router.get(
    "/requests/{request_id}",
    operation_id="getRequestBreakdown",
    response_model=RequestBreakdownOut,
)
async def get_request_calls(ring: RingDep, request_id: int) -> dict[str, Any]:
    """The per-request breakdown: the child calls made under one ``request_id``.

    Builds the ``RequestBreakdown`` envelope the contract documents (request_id +
    surface/endpoint/ts/total_duration_ms/outcome + ordered ``calls[]``) from the
    on-disk ``ring_events`` table. A ``request_id`` with no retained events (aged
    out under retention, or degraded ``None`` ring store) is a 404 (contract).
    """
    calls = await ring.calls_for_request(request_id) if ring is not None else []
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
