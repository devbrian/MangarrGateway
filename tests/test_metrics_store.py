"""InMemoryStore tests (OBS-03/OBS-07 — rollup + ring-classification invariants).

Under the disk-backed ring model (260604-wm2) the store holds ROLLUPS only; the
recent/failures/slow ring events live on disk. So the ring assertions here cover
:meth:`InMemoryStore.classify` — the membership SET an event belongs to — rather
than in-memory deques. Bound/order/payload reads are covered against the disk
store in ``test_metrics_snapshot.py``.

Covers:

* rollup math (count/error_count/avg/min/max/error_rate) is consistent with
  ingested events;
* rollups are keyed on ``(surface, source_key, endpoint, op)`` ONLY — two events
  differing only by ``url`` share one rollup (the no-url-key cardinality cap);
* ``classify`` always includes ``recent``; adds ``failures`` only for a GENUINE
  failure (``is_failure`` — error/timeout, plus a 4xx ``client_error`` on the
  umbrella request event only; NOT cache hit/miss/refetch or the re-solved
  per-source CF-403, debug ``search-error-rate-inflated``);
  adds ``slow`` for a per-baseline outlier (``> max(slow_factor*avg, p95)``) and
  NOT for a steady call — NEVER a global ms constant;
* a failed slow call belongs to ALL THREE rings; a steady ok call to ``recent``
  only;
* ``per_source_per_endpoint`` merges ops under one (source, endpoint).
"""

from __future__ import annotations

from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.store import (
    RING_FAILURES,
    RING_RECENT,
    RING_SLOW,
    InMemoryStore,
)


def _ev(
    *,
    duration_ms: float,
    outcome: str = "ok",
    url: str = "https://h/x",
    request_id: int | None = 1,
    source_key: str | None = "comix",
    endpoint: str = "GET /search",
    op: str = "get_json",
    surface: str = "search",
    kind: str = "http",
    status: int | None = None,
) -> MetricEvent:
    # status defaults are outcome-driven so callers only override when they care:
    # ok → 200, client_error → 403 (the CF-challenge / 4xx shape), else 500.
    if status is None:
        status = 200 if outcome == "ok" else 403 if outcome == "client_error" else 500
    return MetricEvent(
        ts=1.0,
        kind=kind,
        request_id=request_id,
        surface=surface,
        endpoint=endpoint,
        source_key=source_key,
        op=op,
        method="GET",
        url=url,
        status=status,
        outcome=outcome,
        duration_ms=duration_ms,
        attempt=1,
    )


def _store() -> InMemoryStore:
    return InMemoryStore(slow_factor=3.0)


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


def test_classify_ok_call_is_recent_only() -> None:
    s = _store()
    # Establish a baseline so the steady call is not flagged slow.
    for _ in range(50):
        s.classify(_ev(duration_ms=100.0))
    rings = s.classify(_ev(duration_ms=100.0, outcome="ok"))
    assert rings == {RING_RECENT}


def test_classify_failure_adds_failures_ring() -> None:
    s = _store()
    assert s.classify(_ev(duration_ms=100.0, outcome="ok")) == {RING_RECENT}
    assert s.classify(_ev(duration_ms=100.0, outcome="error")) == {
        RING_RECENT,
        RING_FAILURES,
    }
    assert s.classify(_ev(duration_ms=100.0, outcome="timeout")) == {
        RING_RECENT,
        RING_FAILURES,
    }


# ── debug search-error-rate-inflated: non-failure outcomes must NOT count ──────
# The store used a blanket ``outcome != "ok"`` test, so the enumeration cache's
# hit/miss/refetch STATE labels and Comix's expected pre-solve CF-403
# ``client_error`` (re-solved + retried to ok) inflated the per-source
# ``POST /search`` error_rate + the failures ring. They must now be excluded.


def test_classify_cache_state_is_not_a_failure() -> None:
    # kind="cache" hit/miss/refetch are STATE labels, not failures — recent only.
    for outcome in ("hit", "miss", "refetch"):
        rings = _store().classify(
            _ev(duration_ms=0.0, kind="cache", op="enumerate", outcome=outcome)
        )
        assert rings == {RING_RECENT}, outcome


def test_classify_per_source_seam_client_error_is_not_a_failure() -> None:
    # The expected pre-solve Cloudflare 403 rides the per-source http seam
    # (kind="http", source_key set) as outcome="client_error" before it is
    # re-solved + retried to ok — it must NOT admit to the failures ring.
    rings = _store().classify(
        _ev(duration_ms=100.0, kind="http", source_key="comix", outcome="client_error")
    )
    assert rings == {RING_RECENT}


def test_classify_request_level_client_error_still_counts() -> None:
    # WR-04: the gateway's OWN inbound-API 4xx (the umbrella request event,
    # kind="request", source_key=None) MUST still admit to the failures ring so a
    # flood of 401/404/422 stays visible.
    rings = _store().classify(
        _ev(
            duration_ms=5.0,
            kind="request",
            source_key=None,
            op=None,
            outcome="client_error",
        )
    )
    assert rings == {RING_RECENT, RING_FAILURES}


def test_per_source_search_error_rate_excludes_cache_and_resolved_cf403() -> None:
    """End-to-end regression: a HEALTHY per-source POST /search reports ~0%
    error_rate even though its cache lookups (hit/miss/refetch) and a re-solved
    pre-solve CF-403 client_error all roll up under (source, "POST /search")."""
    s = _store()
    common = {"endpoint": "POST /search", "source_key": "comix"}
    # One real successful fetch.
    s.ingest(_ev(duration_ms=120.0, op="get_json", outcome="ok", **common))
    # Cache seam noise — none of these are failures.
    cache_evs = (("resolve", "miss"), ("enumerate", "hit"), ("enumerate", "refetch"))
    for op, outcome in cache_evs:
        s.ingest(_ev(duration_ms=0.0, kind="cache", op=op, outcome=outcome, **common))
    # Comix's expected pre-solve CF-403 at the http seam (re-solved → ok).
    s.ingest(_ev(duration_ms=90.0, op="get_json", outcome="client_error", **common))

    rows = {(r["source"], r["endpoint"]): r for r in s.per_source_per_endpoint()}
    row = rows[("comix", "POST /search")]
    assert row["errors"] == 0
    assert row["error_rate"] == 0.0

    # ...but a GENUINE per-source failure (5xx / transport) still counts.
    s.ingest(_ev(duration_ms=80.0, op="get_json", outcome="error", **common))
    row = {(r["source"], r["endpoint"]): r for r in s.per_source_per_endpoint()}[
        ("comix", "POST /search")
    ]
    assert row["errors"] == 1


def test_classify_slow_is_per_baseline() -> None:
    s = _store()
    # Establish a ~100ms baseline.
    for _ in range(100):
        s.classify(_ev(duration_ms=100.0))
    # A steady ~100ms call is NOT slow (100 < max(3*100, p95)).
    assert RING_SLOW not in s.classify(_ev(duration_ms=100.0))
    # A 5000ms outlier IS slow (5000 > max(3*avg, p95)).
    assert RING_SLOW in s.classify(_ev(duration_ms=5000.0))


def test_classify_failed_slow_call_is_in_all_three_rings() -> None:
    s = _store()
    for _ in range(100):
        s.classify(_ev(duration_ms=100.0))
    rings = s.classify(_ev(duration_ms=9000.0, outcome="error"))
    assert rings == {RING_RECENT, RING_FAILURES, RING_SLOW}


def test_per_source_per_endpoint_merges_ops() -> None:
    s = _store()
    s.ingest(_ev(duration_ms=100.0, op="get_json"))
    s.ingest(_ev(duration_ms=300.0, op="get_bytes"))
    rows = s.per_source_per_endpoint()
    assert len(rows) == 1  # same (source, endpoint), two ops merged
    assert rows[0]["count"] == 2
    assert abs(rows[0]["avg_ms"] - 200.0) < 1e-6
