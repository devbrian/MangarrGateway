"""InMemoryStore tests (Task 2 — OBS-03/OBS-07, rollup + ring invariants).

Covers:

* rollup math (count/error_count/avg/min/max/error_rate) is consistent with
  ingested events;
* rollups are keyed on ``(surface, source_key, endpoint, op)`` ONLY — two events
  differing only by ``url`` share one rollup (the no-url-key cardinality cap);
* the failures ring admits ``outcome != "ok"`` and rejects ``"ok"``;
* the slow ring admits a per-baseline outlier (``> max(slow_factor*avg, p95)``)
  and rejects a steady call — NEVER a global ms constant;
* all three rings are bounded at their configured maxlen (oldest evicted);
* ``calls_for_request(rid)`` returns every recent-ring event for that request id.
"""

from __future__ import annotations

from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.store import InMemoryStore


def _ev(
    *,
    duration_ms: float,
    outcome: str = "ok",
    url: str = "https://h/x",
    request_id: int | None = 1,
    source_key: str = "comix",
    endpoint: str = "GET /search",
    op: str = "get_json",
    surface: str = "search",
) -> MetricEvent:
    return MetricEvent(
        ts=1.0,
        kind="http",
        request_id=request_id,
        surface=surface,
        endpoint=endpoint,
        source_key=source_key,
        op=op,
        method="GET",
        url=url,
        status=200 if outcome == "ok" else 500,
        outcome=outcome,
        duration_ms=duration_ms,
        attempt=1,
    )


def _store() -> InMemoryStore:
    return InMemoryStore(
        recent_max=500, failures_max=200, slow_max=200, slow_factor=3.0
    )


def test_rollup_math() -> None:
    s = _store()
    for d in (100.0, 200.0, 300.0):
        s.ingest(_ev(duration_ms=d))
    s.ingest(_ev(duration_ms=400.0, outcome="error"))
    summary = s.summary()
    assert summary["total_calls"] == 4
    assert summary["total_errors"] == 1
    assert abs(summary["error_rate"] - 0.25) < 1e-9
    rows = s.rollups()
    assert len(rows) == 1
    row = rows[0]
    assert row["count"] == 4
    assert row["errors"] == 1
    assert abs(row["avg_ms"] - 250.0) < 1e-6
    assert row["min_ms"] == 100.0
    assert row["max_ms"] == 400.0


def test_rollup_not_keyed_on_url() -> None:
    s = _store()
    s.ingest(_ev(duration_ms=100.0, url="https://h/a?cf_clearance=X"))
    s.ingest(_ev(duration_ms=100.0, url="https://h/b?q=2"))
    # Two different URLs, same (surface, source, endpoint, op) → ONE rollup.
    assert len(s.rollups()) == 1


def test_failures_ring_admits_non_ok_only() -> None:
    s = _store()
    s.ingest(_ev(duration_ms=100.0, outcome="ok"))
    s.ingest(_ev(duration_ms=100.0, outcome="error"))
    s.ingest(_ev(duration_ms=100.0, outcome="timeout"))
    failures = s.latest_failures(50)
    assert len(failures) == 2
    assert all(f["outcome"] != "ok" for f in failures)


def test_slow_ring_admits_per_baseline_outlier() -> None:
    s = _store()
    # Establish a ~100ms baseline.
    for _ in range(100):
        s.ingest(_ev(duration_ms=100.0))
    slow_before = len(s.latest_slow(500))
    # A steady ~100ms call is NOT slow (100 < max(3*100, p95)).
    s.ingest(_ev(duration_ms=100.0))
    assert len(s.latest_slow(500)) == slow_before
    # A 5000ms outlier IS slow (5000 > max(3*avg, p95)).
    s.ingest(_ev(duration_ms=5000.0))
    slow = s.latest_slow(500)
    assert len(slow) == slow_before + 1
    assert slow[0]["duration_ms"] == 5000.0


def test_all_rings_bounded() -> None:
    s = InMemoryStore(recent_max=10, failures_max=10, slow_max=10, slow_factor=3.0)
    # Establish a dominant ~100ms baseline so a sparse 5000ms outlier stays an
    # outlier relative to the series' own avg/p95 (per-baseline admission).
    for i in range(500):
        s.ingest(_ev(duration_ms=100.0, request_id=i))
    # Push way past every bound: a failure every iter, an outlier every iter.
    for i in range(60):
        s.ingest(_ev(duration_ms=100.0, outcome="error", request_id=1000 + i))
        s.ingest(_ev(duration_ms=5000.0, request_id=2000 + i))
    assert len(s.recent_calls(1000)) == 10
    assert len(s.latest_failures(1000)) == 10
    assert len(s.latest_slow(1000)) == 10


def test_calls_for_request_correlation() -> None:
    s = _store()
    s.ingest(_ev(duration_ms=100.0, request_id=7))
    s.ingest(_ev(duration_ms=200.0, request_id=7, op="get_bytes"))
    s.ingest(_ev(duration_ms=100.0, request_id=8))
    calls = s.calls_for_request(7)
    assert len(calls) == 2
    assert all(c["request_id"] == 7 for c in calls)


def test_per_source_per_endpoint_merges_ops() -> None:
    s = _store()
    s.ingest(_ev(duration_ms=100.0, op="get_json"))
    s.ingest(_ev(duration_ms=300.0, op="get_bytes"))
    rows = s.per_source_per_endpoint()
    assert len(rows) == 1  # same (source, endpoint), two ops merged
    assert rows[0]["count"] == 2
    assert abs(rows[0]["avg_ms"] - 200.0) < 1e-6
