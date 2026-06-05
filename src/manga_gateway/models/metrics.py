"""``/admin/metrics/v1/*`` read-endpoint response models (OBS-05/06, D-06).

These models document the metrics event shape in the runtime ``/openapi.json``
WITHOUT changing the bytes the read endpoints serve. They deliberately DIVERGE
from the :class:`~manga_gateway.models.common.ApiModel` camelCase contract base
for three reasons specific to this surface:

1. **snake_case wire shape, NO aliases.** The served bytes come from
   ``json.dumps(dataclasses.asdict(MetricEvent))`` → snake_case keys
   (``request_id``, ``duration_ms``, ``request_blob``). The other wire models use
   camelCase ``Field(alias=...)`` because ``manga-gateway.openapi.yaml`` is
   camelCase; THIS surface is snake_case and intentionally OUT of that contract
   file (D-06). So these models declare snake_case field names with NO aliases —
   we copy ``search.py``'s ConfigDict discipline, NOT its camelCase.

2. **``extra="allow"`` passthrough (the no-regression safety net).** A
   ``response_model`` DROPS undeclared keys by default. We declare all 18 known
   :class:`MetricEvent` fields, but set ``extra="allow"`` so if a NEW event/payload
   key ever appears before this model is updated, the route passes it through
   verbatim instead of silently dropping it. The lockstep drift test
   (``tests/test_metrics_models.py``) is the guard that catches the drift;
   ``extra="allow"`` degrades a drift to "undocumented but present", not "dropped".

3. **Out-of-contract surface.** FastAPI serializes ``response_model`` with
   ``by_alias=True`` and does NOT exclude none/unset by default. With snake_case
   fields and no aliases, ``by_alias`` is a no-op (keys stay snake_case) and
   ``None`` fields serialize as JSON ``null`` (matching ``asdict``). The routes
   therefore set ``response_model`` and NOTHING else — the defaults reproduce
   today's bytes exactly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class MetricEventOut(BaseModel):
    """Documents one ``MetricEvent`` row on the wire (snake_case, passthrough).

    Mirrors :class:`manga_gateway.metrics.event.MetricEvent` field-for-field: the
    first 13 fields are required-positional in the dataclass (except ``error`` and
    the four 260605-e9a enrichment fields, which default ``None``). Types and
    optionality match the dataclass exactly so a fully-populated AND a minimal
    recorded event both validate and round-trip with identical keys/values.

    The lockstep test asserts ``set(MetricEventOut.model_fields)`` equals the
    ``MetricEvent`` dataclass field set — this is the single declaration of the
    shape; do NOT duplicate the field list elsewhere.
    """

    model_config = ConfigDict(extra="allow")

    ts: float
    kind: str
    request_id: int | None
    surface: str | None
    endpoint: str | None
    source_key: str | None
    op: str | None
    method: str | None
    url: str | None
    status: int | None
    outcome: str
    duration_ms: float
    attempt: int
    error: str | None = None
    request_blob: dict[str, Any] | None = None
    result_count: int | None = None
    candidates_enumerated: int | None = None
    warnings_summary: list[dict[str, Any]] | None = None


class RequestBreakdownOut(BaseModel):
    """Documents the per-request breakdown envelope ``getRequestBreakdown`` builds.

    Mirrors the hand-built dict in ``metrics/routes.py::get_request_calls``:
    the request_id plus the request-level ``surface``/``endpoint``/``ts``/
    ``total_duration_ms``/``outcome`` and the ordered child ``calls[]`` (each a
    :class:`MetricEventOut`). ``extra="allow"`` for the same passthrough reason.
    """

    model_config = ConfigDict(extra="allow")

    request_id: int
    surface: str | None
    endpoint: str | None
    ts: float | None
    total_duration_ms: float | None
    outcome: str | None
    calls: list[MetricEventOut]
