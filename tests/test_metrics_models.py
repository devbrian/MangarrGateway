"""``MetricEventOut`` / ``RequestBreakdownOut`` model guards (OBS-05/06, D-06).

Proves the doc models added to the ``/admin/metrics/v1/*`` read endpoints are
provably faithful to the recorded :class:`MetricEvent` shape WITHOUT changing the
bytes those endpoints serve:

* **lockstep drift** — the model field set must equal the dataclass field set, and
  the breakdown sub-shape must equal its fixed key set; a drift fails with an
  actionable, file-naming message.
* **no-byte regression** — ``MetricEventOut`` round-trips ``asdict(MetricEvent)``
  with identical keys, null handling, and int/float values.
* **extra passthrough** — ``extra="allow"`` means a future undeclared key survives
  (degrades to "undocumented but present", never "dropped").
* **OpenAPI documents the fields** — the live ``app.openapi()`` now surfaces the
  snake_case field names under the metrics component schemas.

Plain sync tests for the model/lockstep/byte assertions (no app/network needed);
``create_app(...).openapi()`` is pure (no lifespan, no network) for the doc test.
"""

from __future__ import annotations

import dataclasses
import json

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.metrics.event import MetricEvent
from manga_gateway.models.metrics import MetricEventOut, RequestBreakdownOut


def _populated_event() -> MetricEvent:
    """A fully-populated event (every field set, enrichment fields non-None)."""
    return MetricEvent(
        ts=1780675483.173904,
        kind="request",
        request_id=42,
        surface="search",
        endpoint="POST /search",
        source_key="comix",
        op="get_json",
        method="POST",
        url="https://comix.example/x?q=naruto",
        status=200,
        outcome="ok",
        duration_ms=12.5,
        attempt=1,
        error=None,
        request_blob={
            "method": "POST",
            "path": "/api/v1/search",
            "query_string": "x=1",
            "body": {"type": "chapter", "query": "naruto"},
        },
        result_count=13,
        candidates_enumerated=5,
        warnings_summary=[{"source_key": "comix", "code": "timeout"}],
    )


def _minimal_event() -> MetricEvent:
    """A minimal event — only the required dataclass fields, enrichment defaulted."""
    return MetricEvent(
        ts=1780675400.0,
        kind="http",
        request_id=None,
        surface=None,
        endpoint=None,
        source_key=None,
        op=None,
        method=None,
        url=None,
        status=None,
        outcome="ok",
        duration_ms=1.0,
        attempt=1,
    )


def test_metricevent_fields_lockstep() -> None:
    """MetricEvent ⇔ MetricEventOut field parity (the drift guard).

    Adding/removing a MetricEvent field without updating MetricEventOut MUST fail
    this test with a message naming the offending field(s) AND the file to edit.
    """
    a = {f.name for f in dataclasses.fields(MetricEvent)}
    b = set(MetricEventOut.model_fields)
    assert a == b, (
        f"MetricEvent/MetricEventOut field drift: {sorted(a ^ b)}. Update "
        f"src/manga_gateway/models/metrics.py::MetricEventOut to match "
        f"src/manga_gateway/metrics/event.py::MetricEvent."
    )


def test_breakdown_shape_lockstep() -> None:
    """RequestBreakdownOut documents exactly the envelope the handler builds."""
    assert set(RequestBreakdownOut.model_fields) == {
        "request_id",
        "surface",
        "endpoint",
        "ts",
        "total_duration_ms",
        "outcome",
        "calls",
    }, (
        "RequestBreakdownOut shape drift — update "
        "src/manga_gateway/models/metrics.py::RequestBreakdownOut to match the dict "
        "built in src/manga_gateway/metrics/routes.py::get_request_calls."
    )


def test_response_model_preserves_bytes_populated() -> None:
    """A populated event round-trips byte-identically through MetricEventOut.

    ``model_dump(mode="json")`` of the validated model must equal
    ``json.loads(json.dumps(asdict(ev)))`` — same keys, same null handling, same
    int/float values. This is the no-regression proof at the model level.
    """
    ev = _populated_event()
    expected = json.loads(json.dumps(dataclasses.asdict(ev)))
    actual = MetricEventOut.model_validate(expected).model_dump(mode="json")
    assert actual == expected


def test_response_model_preserves_bytes_minimal() -> None:
    """A minimal event (enrichment fields None → JSON null) round-trips too."""
    ev = _minimal_event()
    expected = json.loads(json.dumps(dataclasses.asdict(ev)))
    # Sanity: the None enrichment fields are present as null, not dropped.
    assert expected["request_blob"] is None
    assert expected["warnings_summary"] is None
    actual = MetricEventOut.model_validate(expected).model_dump(mode="json")
    assert actual == expected


def test_extra_key_passthrough() -> None:
    """extra="allow" → an undeclared future key survives instead of being dropped."""
    base = json.loads(json.dumps(dataclasses.asdict(_minimal_event())))
    base["future_field"] = 7
    dumped = MetricEventOut.model_validate(base).model_dump()
    assert dumped["future_field"] == 7


def test_breakdown_validates_with_calls() -> None:
    """RequestBreakdownOut validates the full envelope incl. calls=[MetricEventOut]."""
    ev = json.loads(json.dumps(dataclasses.asdict(_populated_event())))
    breakdown = RequestBreakdownOut.model_validate(
        {
            "request_id": 42,
            "surface": "search",
            "endpoint": "POST /search",
            "ts": 1780675483.173904,
            "total_duration_ms": 12.5,
            "outcome": "ok",
            "calls": [ev],
        }
    )
    dumped = breakdown.model_dump(mode="json")
    assert dumped["calls"][0] == ev
    assert dumped["request_id"] == 42


def test_openapi_documents_metric_fields() -> None:
    """The live app.openapi() documents the snake_case metric field names."""
    app = create_app(Settings(api_key="test"))
    spec = app.openapi()
    comps = spec.get("components", {}).get("schemas", {})
    blob = str(comps)
    for field in (
        "request_blob",
        "warnings_summary",
        "result_count",
        "candidates_enumerated",
    ):
        assert field in blob, f"{field} not documented in metrics component schemas"
    recent = spec["paths"]["/admin/metrics/v1/recent"]["get"]
    assert recent["responses"]["200"]["content"], "/recent has no documented 200 schema"
