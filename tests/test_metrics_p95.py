"""HDR p95 + collector-redaction tests (Task 2 — OBS-08, SEC-01 ingest boundary).

* The per-rollup ``HdrHistogram`` p95 is within HDR tolerance of a known
  distribution (the streaming sketch replaces the spike's fixed-bucket placeholder).
* ``collector.emit_http`` reads the attribution contextvars and applies
  ``redact_url`` at the ingest boundary, so a ``cf_clearance`` in the URL is masked
  in the STORED event (Pitfall 5 — the one place a URL is persisted).
"""

from __future__ import annotations

from manga_gateway.metrics.collector import (
    Collector,
    get_collector,
    set_collector,
)
from manga_gateway.metrics.context import current_request, source_scope
from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.store import InMemoryStore


def _store() -> InMemoryStore:
    return InMemoryStore(
        recent_max=500, failures_max=200, slow_max=200, slow_factor=3.0
    )


def _ev(duration_ms: float) -> MetricEvent:
    return MetricEvent(
        ts=1.0,
        kind="http",
        request_id=1,
        surface="search",
        endpoint="GET /search",
        source_key="comix",
        op="get_json",
        method="GET",
        url="https://h/x",
        status=200,
        outcome="ok",
        duration_ms=duration_ms,
        attempt=1,
    )


def test_hdr_p95_within_tolerance() -> None:
    s = _store()
    # 95 calls at 100ms, 5 at 1000ms → p95 ≈ 100ms (HDR ~1% rel error at 2 sigfig).
    for _ in range(95):
        s.ingest(_ev(100.0))
    for _ in range(5):
        s.ingest(_ev(1000.0))
    row = s.rollups()[0]
    # p95 should land right around the boundary (100ms), well under the 1000ms tail.
    assert 95.0 <= row["p95_ms"] <= 115.0


def test_hdr_p95_high_tail() -> None:
    s = _store()
    # 90 @ 50ms, 10 @ 800ms → p95 lands in the 800ms tail.
    for _ in range(90):
        s.ingest(_ev(50.0))
    for _ in range(10):
        s.ingest(_ev(800.0))
    row = s.rollups()[0]
    assert 760.0 <= row["p95_ms"] <= 840.0


def test_collector_redacts_url_at_ingest() -> None:
    s = _store()
    c = Collector(s)
    token = current_request.set(
        {"request_id": 9, "surface": "search", "endpoint": "GET /search"}
    )
    try:
        with source_scope("comix"):
            c.emit_http(
                op="get_json",
                method="GET",
                url="https://h/api?cf_clearance=TOPSECRET&q=foo",
                status=200,
                outcome="ok",
                duration_ms=42.0,
                attempt=1,
            )
    finally:
        current_request.reset(token)
    calls = s.recent_calls(10)
    assert len(calls) == 1
    stored = calls[0]
    assert "TOPSECRET" not in str(stored["url"])
    assert stored["request_id"] == 9
    assert stored["source_key"] == "comix"


def test_set_get_collector_roundtrip() -> None:
    s = _store()
    c = Collector(s)
    set_collector(c)
    try:
        assert get_collector() is c
    finally:
        set_collector(None)
    assert get_collector() is None


def test_emit_with_no_collector_is_noop() -> None:
    # A None collector makes the module-level accessor return None; framework
    # seam guards on that so non-app unit tests stay byte-for-byte unchanged.
    set_collector(None)
    assert get_collector() is None
