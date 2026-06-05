"""The flat ``MetricEvent`` row — the locked Phase 8 event schema.

Lifted verbatim from the spike-003 collector dataclass, ADDING the ``method``
field (the spike omitted it; the CONTEXT.md-locked schema includes it, placed
after ``op``). Deliberately flat + JSON-native so nothing downstream parses text:
the store rolls these up by ``(surface, source_key, endpoint, op)`` and bounds the
full rows in the recent/failures/slow rings; the dashboard reads ready-made JSON.

``@dataclass(slots=True)`` — no ``__dict__`` per row keeps the high-volume ring
events compact; ``dataclasses.asdict`` still round-trips for the JSON serializer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MetricEvent:
    """One structured row consumed by the store/dashboard — flat + JSON-native.

    ``method`` is the schema addition relative to spike-003 (locked CONTEXT.md
    schema), positioned after ``op``. ``request_id``/``surface``/``endpoint`` come
    from the request contextvar; ``source_key`` from the source contextvar; the
    rest from the emit call site (see :mod:`collector`).
    """

    ts: float
    kind: str  # "http" | "solve" | "package" | "limiter-wait" | "job" | "request"
    request_id: int | None
    surface: str | None  # "search" | "download" | ...
    endpoint: str | None  # logical route, e.g. "GET /search"
    source_key: str | None  # "comix" | "mangadex" | None for request-level
    op: str | None  # "get_json" | "get_bytes" | "solve" | ...
    method: str | None  # HTTP verb (GET/POST/...) — added vs spike-003
    url: str | None  # redacted at the collector ingest boundary
    status: int | None  # upstream HTTP status (None for non-HTTP ops)
    outcome: str  # "ok" | "retry" | "cf_resolve" | "error" | "timeout"
    duration_ms: float
    attempt: int  # 1-based; >1 means a tenacity retry / forced re-solve
    error: str | None = None
    # ── 260605-e9a payload-only enrichment fields (all default None) ──────────
    # Additive + optional so asdict() round-trips for every existing emit
    # unchanged and no positional-arg construction past ``error`` breaks. These
    # ride ``json.dumps(asdict(MetricEvent))`` into ring_events.payload and
    # surface through the read endpoints automatically — NO new ring_events
    # column, NO DB migration (the locked design).
    request_blob: dict[str, object] | None = (
        None  # {method, path, query_string, body} on the umbrella request event
    )
    result_count: int | None = (
        None  # final merged count (request) / pre-merge count (source-result)
    )
    candidates_enumerated: int | None = (
        None  # how many ≤5 title candidates a source deep-enumerated
    )
    warnings_summary: list[dict[str, object]] | None = (
        None  # [{source_key, code}, …] on the umbrella request event
    )
