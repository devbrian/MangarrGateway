"""MetricSnapshotStore tests (OBS-04 — rollup snapshot + APPEND-ONLY disk ring).

Two halves with different durability models (260604-wm2):

* **Rollups** keep their full-rewrite snapshot/rehydrate: ``snapshot(mem)`` →
  ``rehydrate()`` round-trips count/error_count/sum_ms/min/max AND p95 from the
  rehydrated HDR blob (within HDR tolerance), in ONE transaction. ``rehydrate()``
  of a fresh DB is empty. Rings are NEVER rehydrated into memory.

* **Ring events** are the on-disk system of record, written APPEND-ONLY:
  ``enqueue`` is O(1) (no await, no I/O); ``flush`` bulk-INSERTs one row per
  (event, ring) membership; the indexed reads return newest-first, bounded,
  filtered by the persisted ``ring`` tag; ``calls_for_request`` returns every
  recent-ring row for a request id; ``max_request_id`` seeds the restart-monotonic
  counter; ``prune`` enforces BOTH the row bound and the age bound; payload reads
  back byte-identical to ``asdict(MetricEvent)`` (response-shape fidelity).

CRITICAL (08-01 known-issue): the HDR blob is the cross-platform ``(value, count)``
pair list, NOT ``HdrHistogram.encode()`` (which raises OverflowError on the Windows
dev host). This test MUST pass on Windows.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

import aiosqlite

from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.snapshot import MetricSnapshotStore, open_metric_store
from manga_gateway.metrics.store import InMemoryStore


def _ev(
    *,
    duration_ms: float,
    outcome: str = "ok",
    request_id: int = 1,
    op: str = "get_json",
    ts: float = 1.0,
    kind: str = "http",
    endpoint: str | None = "GET /search",
) -> MetricEvent:
    return MetricEvent(
        ts=ts,
        kind=kind,
        request_id=request_id,
        surface="search",
        endpoint=endpoint,
        source_key="comix",
        op=op,
        method="GET",
        url="https://h/x",
        status=200 if outcome == "ok" else 500,
        outcome=outcome,
        duration_ms=duration_ms,
        attempt=1,
    )


def _make_rollup_store() -> InMemoryStore:
    s = InMemoryStore(slow_factor=3.0)
    for _ in range(95):
        s.ingest(_ev(duration_ms=100.0))
    for _ in range(5):
        s.ingest(_ev(duration_ms=1000.0, outcome="error"))
    # A second op so per-rollup distinction round-trips.
    s.ingest(_ev(duration_ms=50.0, op="get_bytes"))
    return s


async def _enqueue_via_classify(
    snap: MetricSnapshotStore, mem: InMemoryStore, ev: MetricEvent
) -> None:
    """Classify (rollup update + membership set) then enqueue — the collector path."""
    rings = mem.classify(ev)
    snap.enqueue(ev, rings)


# ───────────────────────────────── rollups ───────────────────────────────────


async def test_open_fresh_db_rehydrates_empty(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = await snap.rehydrate(slow_factor=3.0)
        assert mem.summary()["total_calls"] == 0
        assert mem.rollups() == []
        # Fresh ring is empty too.
        assert await snap.recent_calls(10) == []
        assert await snap.max_request_id() == 0
    finally:
        await snap.close()


async def test_snapshot_rehydrate_roundtrips_rollups(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = _make_rollup_store()
        before = {
            (r["surface"], r["source"], r["endpoint"], r["op"]): r
            for r in mem.rollups()
        }
        before_summary = mem.summary()
        await snap.snapshot(mem)
        restored = await snap.rehydrate(slow_factor=3.0)
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
            assert abs(a["p95_ms"] - b["p95_ms"]) <= max(1.0, 0.02 * b["p95_ms"])
        assert restored.summary() == before_summary
    finally:
        await snap.close()


async def test_snapshot_is_idempotent_over_resnapshot(tmp_path: Path) -> None:
    # Re-snapshotting rollups (DELETE + re-insert) must not double rows.
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = _make_rollup_store()
        await snap.snapshot(mem)
        await snap.snapshot(mem)
        restored = await snap.rehydrate(slow_factor=3.0)
        assert restored.summary() == mem.summary()
    finally:
        await snap.close()


async def test_snapshot_does_not_touch_ring_events(tmp_path: Path) -> None:
    """Rollup snapshot is now ring-agnostic — it must NOT clobber appended ring
    history (the whole point of the append-only model, 260604-wm2)."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = _make_rollup_store()
        await _enqueue_via_classify(snap, mem, _ev(duration_ms=100.0, request_id=7))
        await snap.flush()
        # A rollup snapshot must leave the appended ring rows intact.
        await snap.snapshot(mem)
        recent = await snap.recent_calls(10)
        assert any(e["request_id"] == 7 for e in recent)
    finally:
        await snap.close()


# ─────────────────────────── append-only ring writes ─────────────────────────


async def test_enqueue_is_sync_and_flush_persists(tmp_path: Path) -> None:
    """enqueue does no I/O (returns synchronously); flush makes rows readable."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        # enqueue is a plain method (no await) — the O(1) hot path.
        for i in range(10):
            snap.enqueue(_ev(duration_ms=100.0, request_id=i), {"recent"})
        # Nothing is readable until flush.
        assert await snap.recent_calls(100) == []
        written = await snap.flush()
        assert written == 10
        assert len(await snap.recent_calls(100)) == 10
    finally:
        await snap.close()


async def test_flush_signal_set_at_batch_size(tmp_path: Path) -> None:
    """Enqueuing flush_max_batch events sets the flush signal so the flusher does
    not wait the full interval."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        snap.configure(flush_max_batch=5)
        for i in range(4):
            snap.enqueue(_ev(duration_ms=100.0, request_id=i), {"recent"})
        assert not snap.flush_signal.is_set()
        snap.enqueue(_ev(duration_ms=100.0, request_id=4), {"recent"})
        assert snap.flush_signal.is_set()
        # flush clears the signal.
        await snap.flush()
        assert not snap.flush_signal.is_set()
    finally:
        await snap.close()


async def test_indexed_reads_newest_first_and_bounded(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        # Ascending ts so newest-first is well-defined.
        for i in range(20):
            snap.enqueue(_ev(duration_ms=100.0, request_id=i, ts=float(i)), {"recent"})
        await snap.flush()
        recent = await snap.recent_calls(5)
        assert len(recent) == 5  # bounded to the limit
        # Newest-first: highest ts first.
        assert [e["ts"] for e in recent] == [19.0, 18.0, 17.0, 16.0, 15.0]
    finally:
        await snap.close()


async def test_multi_ring_membership_writes_one_row_per_ring(tmp_path: Path) -> None:
    """A failed slow call lands in recent AND failures AND slow."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        mem = InMemoryStore(slow_factor=3.0)
        for _ in range(100):
            await _enqueue_via_classify(snap, mem, _ev(duration_ms=100.0))
        await _enqueue_via_classify(
            snap, mem, _ev(duration_ms=9000.0, outcome="error", request_id=42, ts=99.0)
        )
        await snap.flush()
        recent = await snap.recent_calls(1000)
        failures = await snap.latest_failures(1000)
        slow = await snap.latest_slow(1000)
        assert any(e["request_id"] == 42 for e in recent)
        assert any(e["request_id"] == 42 for e in failures)
        assert any(e["request_id"] == 42 for e in slow)
        # A steady ok call is only in recent.
        assert failures == [f for f in failures if f["outcome"] != "ok"]
    finally:
        await snap.close()


# ─────────── include_unrouted filter (260606-0mq — hide endpoint=null) ────────


async def test_recent_calls_default_hides_unrouted(tmp_path: Path) -> None:
    """recent_calls at the default (include_unrouted=False per the route) returns
    ONLY routed events (endpoint set); include_unrouted=True restores ALL events
    (routed + unrouted), matching today's full output."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        # Two routed + two unrouted (endpoint=None) events.
        snap.enqueue(_ev(duration_ms=100.0, request_id=1, ts=1.0), {"recent"})
        snap.enqueue(
            _ev(duration_ms=100.0, request_id=2, ts=2.0, endpoint=None), {"recent"}
        )
        snap.enqueue(_ev(duration_ms=100.0, request_id=3, ts=3.0), {"recent"})
        snap.enqueue(
            _ev(duration_ms=100.0, request_id=4, ts=4.0, endpoint=None), {"recent"}
        )
        await snap.flush()

        filtered = await snap.recent_calls(100, include_unrouted=False)
        assert all(e["endpoint"] is not None for e in filtered)
        assert {e["request_id"] for e in filtered} == {1, 3}

        unfiltered = await snap.recent_calls(100, include_unrouted=True)
        assert {e["request_id"] for e in unfiltered} == {1, 2, 3, 4}
        # The public accessor default keeps the full pre-260606 output (True).
        assert await snap.recent_calls(100) == unfiltered
    finally:
        await snap.close()


async def test_limit_applies_to_filtered_set(tmp_path: Path) -> None:
    """LIMIT bounds the FILTERED set: 3 routed + 20 unrouted with limit=3 →
    exactly 3 routed events, zero unrouted. A Python post-filter would have
    returned fewer real rows here — this proves the filter is IN SQL before LIMIT.
    """
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        # 20 unrouted with the NEWEST timestamps (would dominate a post-filter top-3).
        for i in range(20):
            snap.enqueue(
                _ev(duration_ms=100.0, request_id=100 + i, ts=100.0 + i, endpoint=None),
                {"recent"},
            )
        # 3 routed with OLDER timestamps.
        for i in range(3):
            snap.enqueue(_ev(duration_ms=100.0, request_id=i, ts=float(i)), {"recent"})
        await snap.flush()

        filtered = await snap.recent_calls(3, include_unrouted=False)
        assert len(filtered) == 3
        assert all(e["endpoint"] is not None for e in filtered)
        assert {e["request_id"] for e in filtered} == {0, 1, 2}
    finally:
        await snap.close()


async def test_latest_failures_and_slow_honor_include_unrouted(tmp_path: Path) -> None:
    """latest_failures and latest_slow hide an unrouted error/slow event by default
    and surface it under include_unrouted=True (all three feeds at the snapshot
    layer)."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        # Enqueue an unrouted (endpoint=None) event directly into BOTH the failures
        # and slow rings (explicit membership — we are testing the read-path SQL
        # filter, not the live slow classifier's per-baseline threshold).
        unrouted = _ev(
            duration_ms=9000.0,
            outcome="error",
            request_id=42,
            ts=99.0,
            endpoint=None,
        )
        snap.enqueue(unrouted, {"recent", "failures", "slow"})
        # A routed failure/slow event in the same rings so the filtered output is
        # non-empty (proves the filter hides ONLY the unrouted row).
        routed = _ev(duration_ms=9000.0, outcome="error", request_id=7, ts=50.0)
        snap.enqueue(routed, {"recent", "failures", "slow"})
        await snap.flush()

        # Default hides the unrouted event on both feeds (but keeps the routed one).
        failures_default = await snap.latest_failures(1000, include_unrouted=False)
        slow_default = await snap.latest_slow(1000, include_unrouted=False)
        assert {e["request_id"] for e in failures_default} == {7}
        assert {e["request_id"] for e in slow_default} == {7}

        # include_unrouted=True restores it on both feeds.
        failures_all = await snap.latest_failures(1000, include_unrouted=True)
        slow_all = await snap.latest_slow(1000, include_unrouted=True)
        assert any(e["request_id"] == 42 for e in failures_all)
        assert any(e["request_id"] == 42 for e in slow_all)
    finally:
        await snap.close()


async def test_calls_for_request_returns_request_rows(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        snap.enqueue(_ev(duration_ms=100.0, request_id=7, ts=1.0), {"recent"})
        snap.enqueue(
            _ev(duration_ms=200.0, request_id=7, op="get_bytes", ts=2.0), {"recent"}
        )
        snap.enqueue(_ev(duration_ms=100.0, request_id=8, ts=3.0), {"recent"})
        await snap.flush()
        calls = await snap.calls_for_request(7)
        assert len(calls) == 2
        assert all(c["request_id"] == 7 for c in calls)
        # Oldest-first within the request.
        assert [c["ts"] for c in calls] == [1.0, 2.0]
    finally:
        await snap.close()


async def test_payload_roundtrips_byte_identical(tmp_path: Path) -> None:
    """A row read back equals asdict(MetricEvent) — the API echoes payload verbatim."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        ev = _ev(duration_ms=123.0, request_id=5, op="get_bytes")
        snap.enqueue(ev, {"recent"})
        await snap.flush()
        recent = await snap.recent_calls(10)
        assert recent == [asdict(ev)]
    finally:
        await snap.close()


async def test_enriched_request_event_roundtrips_verbatim(tmp_path: Path) -> None:
    """A ``request`` event WITH request_blob + result_count + warnings_summary
    serializes through json.dumps(asdict(MetricEvent)), persists to
    ring_events.payload, and reads back VERBATIM via calls_for_request — NO schema
    migration, NO new ring_events column (260605-e9a)."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        ev = MetricEvent(
            ts=5.0,
            kind="request",
            request_id=11,
            surface="search",
            endpoint="POST /search",
            source_key=None,
            op=None,
            method=None,
            url=None,
            status=200,
            outcome="ok",
            duration_ms=42.0,
            attempt=1,
            request_blob={
                "method": "POST",
                "path": "/api/v1/search",
                "query_string": "x=1",
                "body": {"type": "chapter", "query": "naruto"},
            },
            result_count=37,
            warnings_summary=[{"source_key": "comix", "code": "timeout"}],
        )
        snap.enqueue(ev, {"recent"})
        await snap.flush()
        calls = await snap.calls_for_request(11)
        assert calls == [asdict(ev)]
        # The new fields survive the JSON round-trip verbatim.
        got = calls[0]
        assert got["request_blob"]["body"] == {"type": "chapter", "query": "naruto"}
        assert got["result_count"] == 37
        assert got["warnings_summary"] == [{"source_key": "comix", "code": "timeout"}]
    finally:
        await snap.close()


async def test_ring_events_has_no_promoted_enrichment_columns(tmp_path: Path) -> None:
    """The four enrichment fields live ONLY in payload JSON — PRAGMA confirms the
    ring_events table has NO new column for them (the locked no-migration design)."""
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        conn = await aiosqlite.connect(db)
        conn.row_factory = aiosqlite.Row
        async with conn.execute("PRAGMA table_info(ring_events)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        await conn.close()
        assert cols == {"id", "ring", "ts", "request_id", "payload"}
        for promoted in (
            "request_blob",
            "result_count",
            "candidates_enumerated",
            "warnings_summary",
        ):
            assert promoted not in cols
    finally:
        await snap.close()


# ─────────────────────────── request_id seed + prune ─────────────────────────


async def test_max_request_id(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        assert await snap.max_request_id() == 0  # empty DB
        for rid in (3, 7, 5):
            snap.enqueue(_ev(duration_ms=100.0, request_id=rid), {"recent"})
        await snap.flush()
        assert await snap.max_request_id() == 7
    finally:
        await snap.close()


async def test_seed_request_ids_climbs_above_persisted(tmp_path: Path) -> None:
    """seed_request_ids(max+1) makes next_request_id climb above the persisted max;
    an empty DB seeds to 1 (the restart-monotonic fix, 260604-wm2)."""
    from manga_gateway.metrics import context

    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        for rid in (3, 7, 5):
            snap.enqueue(_ev(duration_ms=100.0, request_id=rid), {"recent"})
        await snap.flush()
        context.seed_request_ids(await snap.max_request_id() + 1)
        assert context.next_request_id() == 8
        assert context.next_request_id() == 9
    finally:
        await snap.close()
        # Restore the import-time default so this test does not leak into others.
        context.seed_request_ids(1)


def test_seed_request_ids_floors_at_one() -> None:
    """An empty/zero seed still yields ids ≥ 1 (max(1, start) guard)."""
    from manga_gateway.metrics import context

    try:
        context.seed_request_ids(0)
        assert context.next_request_id() == 1
    finally:
        context.seed_request_ids(1)


async def test_prune_row_bound(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        # Realistic recent ts (now-relative) so the age bound does NOT also fire —
        # this isolates the row-bound delete.
        base = time.time()
        for i in range(50):
            snap.enqueue(_ev(duration_ms=100.0, request_id=i, ts=base + i), {"recent"})
        await snap.flush()
        deleted = await snap.prune(max_rows=10, max_age_days=3650)
        assert deleted == 40
        recent = await snap.recent_calls(1000)
        assert len(recent) == 10
        # The 10 newest (highest ts) survive.
        assert {e["ts"] for e in recent} == {base + i for i in range(40, 50)}
    finally:
        await snap.close()


async def test_prune_age_bound(tmp_path: Path) -> None:
    db = str(tmp_path / "metrics.db")
    snap = await open_metric_store(db)
    try:
        now = time.time()
        old = now - 40 * 86_400  # 40 days old
        fresh = now - 1 * 86_400  # 1 day old
        snap.enqueue(_ev(duration_ms=100.0, request_id=1, ts=old), {"recent"})
        snap.enqueue(_ev(duration_ms=100.0, request_id=2, ts=fresh), {"recent"})
        await snap.flush()
        deleted = await snap.prune(max_rows=1_000_000, max_age_days=30)
        assert deleted == 1
        recent = await snap.recent_calls(1000)
        assert [e["request_id"] for e in recent] == [2]
    finally:
        await snap.close()


# ───────────────────────────── degraded / boot ───────────────────────────────


async def test_create_app_boots_when_metrics_db_unwritable(tmp_path: Path) -> None:
    """Open-resilience: an UNOPENABLE ``metrics_db_path`` must degrade to
    in-memory-only metrics, NOT abort gateway startup.

    Regression for the CI ``/state`` failure: ``metrics_db_path`` defaults under
    ``/state`` (the docker volume), which is unwritable outside the container, so
    ``open_metric_store`` raised ``sqlite3.OperationalError`` and took down the
    whole lifespan. Reproduced cross-platform by pointing the path at a
    non-existent parent dir (sqlite won't ``mkdir`` it).
    """
    from manga_gateway.app import create_app
    from manga_gateway.config import Settings

    unopenable = tmp_path / "missing_dir" / "metrics.db"  # parent absent → open fails
    app = create_app(
        Settings(
            api_key="test-key-deterministic-0123456789",
            metrics_db_path=str(unopenable),
            log_dir=str(tmp_path / "logs"),
            output_root=str(tmp_path / "out"),
            db_path=str(tmp_path / "jobs.db"),
            handle_db_path=str(tmp_path / "handles.db"),
        )
    )
    async with app.router.lifespan_context(app):
        assert app.state.collector is not None
        assert app.state.metric_store is not None
    assert not unopenable.exists()


async def test_open_migrates_old_shape_ring_events(tmp_path: Path) -> None:
    """A pre-260604-wm2 ``metrics.db`` (ring_events = ring/seq/payload, no ts/id)
    must migrate cleanly on open — NOT raise ``no such column: ts``.

    Regression (prod deploy 260604-wm2): ``open_metric_store`` ran
    ``executescript(_CREATE_SCHEMA)`` — which creates indexes on the NEW columns
    (``ring_events(ring, ts)``) — BEFORE ``_migrate_ring_events``. Against a real
    upgraded DB the old-shape table survived ``CREATE TABLE IF NOT EXISTS`` and the
    index DDL exploded with ``sqlite3.OperationalError: no such column: ts``,
    forcing the metrics subsystem into degraded in-memory-only mode on every boot.
    The migration must run FIRST so the stale table is dropped before the schema +
    indexes apply.
    """
    db = str(tmp_path / "metrics.db")
    # Hand-build the OLD-shape DB: an old ring_events (+ a row) and the unchanged
    # rollups table, exactly as a pre-260604-wm2 gateway would have left it.
    conn = await aiosqlite.connect(db)
    await conn.executescript(
        """
        CREATE TABLE rollups (
          surface TEXT, source_key TEXT, endpoint TEXT, op TEXT,
          count INTEGER NOT NULL, error_count INTEGER NOT NULL,
          sum_ms REAL NOT NULL, min_ms REAL NOT NULL, max_ms REAL NOT NULL,
          hist_blob TEXT NOT NULL
        );
        CREATE TABLE ring_events (
          ring TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL
        );
        """
    )
    await conn.execute(
        "INSERT INTO ring_events (ring, seq, payload) VALUES (?, ?, ?)",
        ("recent", 0, '{"ts": 1.0, "kind": "request", "request_id": 7}'),
    )
    await conn.commit()
    await conn.close()

    # Must NOT raise (was: sqlite3.OperationalError: no such column: ts).
    snap = await open_metric_store(db)
    try:
        # New schema is in place and writable: an enqueue→flush→indexed-read
        # round-trips, proving the table was migrated to the append-only shape.
        mem = InMemoryStore(slow_factor=3.0)
        await _enqueue_via_classify(snap, mem, _ev(duration_ms=100.0, request_id=42))
        await snap.flush()
        recent = await snap.recent_calls(10)
        assert [r["request_id"] for r in recent] == [42]
        # The old ephemeral row was discarded by the migration (not the new one).
        assert all(r["request_id"] != 7 for r in recent)
        # The restart-monotonic seed reads the migrated table's MAX.
        assert await snap.max_request_id() == 42
    finally:
        await snap.close()
