"""MetricSnapshotStore round-trip tests (Task 3 — OBS-04, aiosqlite persistence).

Mirrors ``jobs/store.py`` shape into a SEPARATE sqlite file:

* ``open_metric_store(tmp)`` connects, sets WAL, creates the schema; ``rehydrate()``
  on a fresh DB returns an empty ``InMemoryStore`` (no-op);
* ``snapshot(mem)`` → ``rehydrate()`` round-trips rollups (count/error_count/sum_ms/
  min/max AND p95 from the rehydrated HDR blob, within HDR tolerance) plus the
  recent/failures/slow rings (order + bound preserved);
* the snapshot is ONE transaction (DELETE + executemany + single commit).

CRITICAL (08-01 known-issue): the HDR blob is the cross-platform ``(value, count)``
pair list, NOT ``HdrHistogram.encode()`` (which raises OverflowError on the Windows
dev host). This test MUST pass on Windows.
"""

from __future__ import annotations

from pathlib import Path

from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.snapshot import open_metric_store
from manga_gateway.metrics.store import InMemoryStore


def _ev(
    *,
    duration_ms: float,
    outcome: str = "ok",
    request_id: int = 1,
    op: str = "get_json",
) -> MetricEvent:
    return MetricEvent(
        ts=1.0,
        kind="http",
        request_id=request_id,
        surface="search",
        endpoint="GET /search",
        source_key="comix",
        op=op,
        method="GET",
        url="https://h/x",
        status=200 if outcome == "ok" else 500,
        outcome=outcome,
        duration_ms=duration_ms,
        attempt=1,
    )


def _make_store() -> InMemoryStore:
    s = InMemoryStore(recent_max=500, failures_max=200, slow_max=200, slow_factor=3.0)
    for _ in range(95):
        s.ingest(_ev(duration_ms=100.0))
    for _ in range(5):
        s.ingest(_ev(duration_ms=1000.0, outcome="error"))
    # A second op so per-rollup distinction round-trips.
    s.ingest(_ev(duration_ms=50.0, op="get_bytes"))
    return s


async def test_open_fresh_db_rehydrates_empty(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = await snap.rehydrate()
        assert mem.summary()["total_calls"] == 0
        assert mem.rollups() == []
        assert mem.recent_calls(10) == []
    finally:
        await snap.close()


async def test_snapshot_rehydrate_roundtrips_rollups(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = _make_store()
        before = {
            (r["surface"], r["source"], r["endpoint"], r["op"]): r
            for r in mem.rollups()
        }
        before_summary = mem.summary()
        await snap.snapshot(mem)
        restored = await snap.rehydrate()
        after = {
            (r["surface"], r["source"], r["endpoint"], r["op"]): r
            for r in restored.rollups()
        }
        assert set(after) == set(before)
        for key, b in before.items():
            a = after[key]
            assert a["count"] == b["count"]
            assert a["errors"] == b["errors"]
            assert a["avg_ms"] == b["avg_ms"]
            assert a["min_ms"] == b["min_ms"]
            assert a["max_ms"] == b["max_ms"]
            # p95 from the rehydrated HDR pairs — exact at this distribution.
            assert abs(a["p95_ms"] - b["p95_ms"]) <= max(1.0, 0.02 * b["p95_ms"])
        assert restored.summary() == before_summary
    finally:
        await snap.close()


async def test_snapshot_rehydrate_roundtrips_rings(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = _make_store()
        # Add a genuine slow outlier so the slow ring is non-empty.
        mem.ingest(_ev(duration_ms=9000.0, request_id=42))
        recent_before = mem.recent_calls(1000)
        failures_before = mem.latest_failures(1000)
        slow_before = mem.latest_slow(1000)
        assert failures_before  # sanity: there ARE failures
        assert slow_before  # sanity: there IS a slow call

        await snap.snapshot(mem)
        restored = await snap.rehydrate()

        assert restored.recent_calls(1000) == recent_before
        assert restored.latest_failures(1000) == failures_before
        assert restored.latest_slow(1000) == slow_before
    finally:
        await snap.close()


async def test_rehydrate_skips_unknown_ring_label(tmp_path: Path) -> None:
    """CodeRabbit: a forward/corrupt snapshot carrying an UNKNOWN ring label must be
    skipped row-by-row, not KeyError out of the whole rehydrate."""
    import json

    from manga_gateway.metrics.snapshot import _RING_INSERT, _event_to_json

    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = _make_store()
        good = _ev(duration_ms=100.0, request_id=7)
        await snap.snapshot(mem)
        # Inject one good 'recent' row and one row with an unknown ring label.
        await snap._conn.execute(_RING_INSERT, ("recent", 9999, _event_to_json(good)))
        await snap._conn.execute(
            _RING_INSERT, ("from_the_future", 0, json.dumps({"garbage": True}))
        )
        await snap._conn.commit()

        # Must NOT raise; the unknown-ring row is dropped, the good row survives.
        restored = await snap.rehydrate()
        recent = restored.recent_calls(1000)
        assert any(e["request_id"] == 7 for e in recent)
    finally:
        await snap.close()


async def test_snapshot_is_idempotent_over_resnapshot(tmp_path: Path) -> None:
    # Re-snapshotting (DELETE + re-insert) must not double rows.
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = _make_store()
        await snap.snapshot(mem)
        await snap.snapshot(mem)
        restored = await snap.rehydrate()
        assert restored.summary() == mem.summary()
        assert len(restored.recent_calls(1000)) == len(mem.recent_calls(1000))
    finally:
        await snap.close()
