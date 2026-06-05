"""aiosqlite metrics store — rollup snapshot + APPEND-ONLY disk ring (OBS-04).

Mirrors :mod:`manga_gateway.jobs.store` in shape: one
:class:`aiosqlite.Connection` + one :class:`asyncio.Lock`, WAL journal mode, an
``open_metric_store(path)`` async factory, per-write
``async with self._write_lock: ... commit()``, and ``close()``. aiosqlite is
async-native — every call is awaited, NO ``asyncio.to_thread``.

The snapshot DB is a SEPARATE file from ``gateway.db`` (default
``/state/metrics.db`` via ``Settings.metrics_db_path``) so metrics persistence
never contends with the job store.

**Two halves with DIFFERENT durability models (260604-wm2):**

* **Rollups** stay IN MEMORY and keep their full-rewrite snapshot: ``snapshot(mem)``
  flushes the whole ``rollups`` table in ONE transaction (DELETE + ``executemany``
  + single commit — never per-event), and ``rehydrate()`` reloads them into a
  fresh :class:`InMemoryStore`. Rollups are tiny (~16 rows) so the full rewrite is
  cheap and idempotent. Worst-case loss on a hard crash is one snapshot interval.

* **Ring events** are the on-disk system of record, written APPEND-ONLY. The hot
  path is O(1): :meth:`enqueue` does a plain ``list.append`` of ``(event, rings)``
  and returns (no await, no I/O). A background flusher drains the queue and
  bulk-``INSERT``s one row per (event, ring) membership via ``executemany``
  (:meth:`flush`), triggered by either a ~5s timer OR the queue reaching
  ``flush_max_batch`` (an :class:`asyncio.Event` set by ``enqueue``). Reads are
  indexed (``recent_calls`` / ``latest_failures`` / ``latest_slow`` /
  ``calls_for_request``); :meth:`prune` enforces BOTH a row bound and an age bound;
  :meth:`max_request_id` seeds the restart-monotonic request-id counter. The OLD
  full-DELETE+reinsert of ``ring_events`` is GONE — it would clobber appended
  history. Up to ~``flush_interval_s`` (5s default) of ring events may be lost on a
  hard crash (accepted, mirrors the "≤ snapshot interval" honesty).

**Schema migration (no migration framework, mirrors jobs/store.py).** The new
``ring_events`` shape promotes ``ts``/``request_id`` to real indexed columns and
adds an ``id INTEGER PRIMARY KEY`` (unambiguous oldest-row prune). A pre-existing
``metrics.db`` with the OLD ring_events shape (``ring/seq/payload`` only) is
detected via ``PRAGMA table_info`` (no ``ts``/``id`` column) and the table is
DROP+recreated under the new schema on open — the discarded rows are at most one
post-restart deque-worth of the OLD ephemeral model, never the new large history.
The ``rollups`` table is untouched.

**Histogram serialization (CRITICAL — cross-platform).** ``HdrHistogram.encode()``
AND ``add()`` raise ``OverflowError`` on the Windows dev host (LLP64 32-bit
``c_long``). So the ``hist_blob TEXT`` column stores the compact populated
``(value, count)`` pair list (via :func:`histogram_pairs`) as JSON; ``rehydrate()``
replays ``record_value(value, count)`` into a fresh ``HdrHistogram(1, 600_000, 2)``
(via :func:`histogram_from_pairs`). This round-trips p50/p95/total_count EXACTLY on
Windows and Linux.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict

import aiosqlite

from .event import MetricEvent
from .store import (
    InMemoryStore,
    Rollup,
    RollupKey,
    histogram_from_pairs,
    histogram_pairs,
)

_log = logging.getLogger("manga_gateway")

# ring_events is APPEND-ONLY (260604-wm2): id is the unambiguous oldest-row prune
# key; ts + request_id are promoted to indexed columns (were inside the payload
# JSON in the OLD shape); payload still carries the full redacted event the API
# echoes back verbatim (response-shape fidelity). Newest-first = ORDER BY ts DESC,
# id DESC. Indexes: (ring, ts) per-ring LIMIT reads; (request_id) breakdown; (ts)
# age prune.
_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rollups (
  surface     TEXT,
  source_key  TEXT,
  endpoint    TEXT,
  op          TEXT,
  count       INTEGER NOT NULL,
  error_count INTEGER NOT NULL,
  sum_ms      REAL NOT NULL,
  min_ms      REAL NOT NULL,
  max_ms      REAL NOT NULL,
  hist_blob   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ring_events (
  id         INTEGER PRIMARY KEY,
  ring       TEXT NOT NULL,    -- 'recent' | 'failures' | 'slow'
  ts         REAL NOT NULL,
  request_id INTEGER,
  payload    TEXT NOT NULL     -- json.dumps(asdict(MetricEvent))
);
CREATE INDEX IF NOT EXISTS ix_ring_events_ring_ts ON ring_events(ring, ts);
CREATE INDEX IF NOT EXISTS ix_ring_events_request_id ON ring_events(request_id);
CREATE INDEX IF NOT EXISTS ix_ring_events_ts ON ring_events(ts);
"""

_ROLLUP_COLS = (
    "surface",
    "source_key",
    "endpoint",
    "op",
    "count",
    "error_count",
    "sum_ms",
    "min_ms",
    "max_ms",
    "hist_blob",
)
_ROLLUP_INSERT = (
    f"INSERT INTO rollups ({', '.join(_ROLLUP_COLS)}) "
    f"VALUES ({', '.join('?' for _ in _ROLLUP_COLS)})"
)
_RING_INSERT = (
    "INSERT INTO ring_events (ring, ts, request_id, payload) VALUES (?, ?, ?, ?)"
)

_SECONDS_PER_DAY = 86_400


class MetricSnapshotStore:
    """Owns one ``aiosqlite.Connection`` for metrics persistence (OBS-04).

    Construct via :func:`open_metric_store` (which creates/migrates the schema).
    The single :class:`asyncio.Lock` serializes every write so the shared
    connection never sees concurrent statements (Pitfall 7). Reads share the
    same connection.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._write_lock = asyncio.Lock()
        # In-memory append queue (the O(1) hot path). Each entry is one
        # (event, rings) pair; flush() expands it to one row per membership.
        self._queue: list[tuple[MetricEvent, set[str]]] = []
        self._flush_max_batch = 1000
        # Set by enqueue when the queue reaches the batch size so the flusher
        # loop wakes immediately instead of waiting the full interval.
        self.flush_signal = asyncio.Event()

    def configure(self, *, flush_max_batch: int) -> None:
        """Set the batch-size flush trigger (from Settings, called by the lifespan)."""
        self._flush_max_batch = max(1, flush_max_batch)

    # ── ring write path (append-only) ─────────────────────────────────────────
    def enqueue(self, ev: MetricEvent, rings: set[str]) -> None:
        """O(1) hot-path append — NO await, NO I/O.

        Appends ``(event, rings)`` to the in-memory queue and, when the queue
        reaches ``flush_max_batch``, sets ``flush_signal`` so the background
        flusher drains it without waiting the full interval. The actual disk
        write happens only in :meth:`flush`.
        """
        self._queue.append((ev, rings))
        if len(self._queue) >= self._flush_max_batch:
            self.flush_signal.set()

    async def flush(self) -> int:
        """Drain the queue and bulk-INSERT one row per (event, ring) membership.

        Runs under the write lock; returns the number of ROWS written (≥ the
        number of events, since one event in N rings writes N rows). A no-op when
        the queue is empty. Clears ``flush_signal`` after draining.
        """
        # Swap the queue out cheaply so concurrent enqueues during the await
        # accumulate into a fresh list (they are not lost — next flush drains them).
        pending = self._queue
        self._queue = []
        self.flush_signal.clear()
        if not pending:
            return 0
        rows: list[tuple[str, float, int | None, str]] = []
        for ev, rings in pending:
            payload = _event_to_json(ev)
            for ring in rings:
                rows.append((ring, ev.ts, ev.request_id, payload))
        async with self._write_lock:
            await self._conn.executemany(_RING_INSERT, rows)
            await self._conn.commit()
        return len(rows)

    # ── ring read path (indexed) ──────────────────────────────────────────────
    async def recent_calls(self, limit: int) -> list[dict[str, object]]:
        """Newest-first events in the ``recent`` ring, bounded to ``limit``."""
        return await self._latest_in_ring("recent", limit)

    async def latest_failures(self, limit: int) -> list[dict[str, object]]:
        """Newest-first events in the ``failures`` ring, bounded to ``limit``."""
        return await self._latest_in_ring("failures", limit)

    async def latest_slow(self, limit: int) -> list[dict[str, object]]:
        """Newest-first events in the ``slow`` ring, bounded to ``limit``."""
        return await self._latest_in_ring("slow", limit)

    async def _latest_in_ring(self, ring: str, limit: int) -> list[dict[str, object]]:
        async with self._conn.execute(
            "SELECT payload FROM ring_events WHERE ring = ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (ring, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [json.loads(row["payload"]) for row in rows]

    async def calls_for_request(self, request_id: int) -> list[dict[str, object]]:
        """Every row WHERE request_id=? — the per-request breakdown.

        Ordered ts ASC, id ASC to preserve the child-call order within the
        request (the breakdown lists calls oldest-first). Deduplicated by event:
        an event in multiple rings has multiple rows, but the breakdown wants one
        entry per (kind, ts, op) call — dedupe on the (ts, id-of-recent-row) so a
        slow failure is not listed 3×. We restrict to the ``recent`` ring (every
        event is in recent exactly once) so each call appears once.
        """
        async with self._conn.execute(
            "SELECT payload FROM ring_events WHERE request_id = ? AND ring = 'recent' "
            "ORDER BY ts ASC, id ASC",
            (request_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [json.loads(row["payload"]) for row in rows]

    async def max_request_id(self) -> int:
        """Largest persisted ``request_id`` (0 on an empty DB) — the reseed source."""
        async with self._conn.execute(
            "SELECT MAX(request_id) AS m FROM ring_events"
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["m"] is None:
            return 0
        return int(row["m"])

    async def prune(self, max_rows: int, max_age_days: int) -> int:
        """Enforce BOTH retention bounds; return total rows deleted (260604-wm2).

        Deletes rows older than ``now - max_age_days*86400`` (by ``ts``), then
        deletes the oldest ``id``s beyond ``max_rows`` (mirrors
        ``jobs/store.prune_terminal``'s keep-the-newest-N subquery idiom). Both
        deletes are indexed. ``max_rows``/``max_age_days`` < 1 are floored to 1
        (Settings already enforces ge=1; belt-and-suspenders).
        """
        keep_rows = max(1, max_rows)
        keep_days = max(1, max_age_days)
        cutoff_ts = time.time() - keep_days * _SECONDS_PER_DAY
        deleted = 0
        async with self._write_lock:
            cur = await self._conn.execute(
                "DELETE FROM ring_events WHERE ts < ?", (cutoff_ts,)
            )
            deleted += cur.rowcount or 0
            await cur.close()
            cur = await self._conn.execute(
                "DELETE FROM ring_events WHERE id NOT IN ("
                "  SELECT id FROM ring_events ORDER BY ts DESC, id DESC LIMIT ?"
                ")",
                (keep_rows,),
            )
            deleted += cur.rowcount or 0
            await cur.close()
            await self._conn.commit()
        return deleted

    # ── rollup snapshot/rehydrate (UNCHANGED full-rewrite — rollups stay tiny) ─
    async def snapshot(self, mem: InMemoryStore) -> None:
        """Flush the ROLLUPS in ONE transaction (DELETE + insert + single commit).

        Re-snapshot is idempotent (clears then re-inserts). Ring events are NOT
        touched here anymore — they are append-only via :meth:`flush` (260604-wm2;
        the old full-DELETE+reinsert of ring_events would clobber appended
        history).
        """
        rollup_rows = [
            _rollup_values(key, rollup) for key, rollup in mem.iter_rollups()
        ]
        async with self._write_lock:
            await self._conn.execute("DELETE FROM rollups")
            if rollup_rows:
                await self._conn.executemany(_ROLLUP_INSERT, rollup_rows)
            await self._conn.commit()

    async def rehydrate(self, *, slow_factor: float) -> InMemoryStore:
        """Reload the last ROLLUP snapshot into a fresh ``InMemoryStore``.

        Rings are NEVER loaded into memory under the disk-as-source-of-truth model
        (260604-wm2) — the ring read path is this store's indexed queries. This
        only restores the in-memory rollups so summary/per-source survive restart.

        ``slow_factor`` is forwarded from ``Settings.metrics_slow_factor`` so the
        returned store classifies "slow" consistently with the live store if it is
        ever used for ingest. (Today the caller only drains its rollups, but we
        thread the setting through end-to-end rather than baking in a constant.)
        """
        async with self._conn.execute("SELECT * FROM rollups") as cur:
            rollup_rows = await cur.fetchall()
        mem = InMemoryStore(slow_factor=slow_factor)
        for row in rollup_rows:
            # WR-02: salvage a partially-valid snapshot — a single corrupt /
            # schema-drifted rollup row is skipped rather than aborting the whole
            # rehydrate (metrics must never break the service).
            try:
                key, rollup = _row_to_rollup(row)
            except Exception:  # noqa: BLE001 — drop the bad row, keep the rest
                _log.warning("skipping corrupt rollup snapshot row", exc_info=True)
                continue
            mem.restore_rollup(key, rollup)
        return mem

    async def close(self) -> None:
        """Close the backing connection (lifespan teardown)."""
        await self._conn.close()


def _rollup_values(key: RollupKey, rollup: Rollup) -> tuple[object, ...]:
    surface, source_key, endpoint, op = key
    return (
        surface,
        source_key,
        endpoint,
        op,
        rollup.count,
        rollup.error_count,
        rollup.sum_ms,
        rollup.min_ms,
        rollup.max_ms,
        json.dumps(histogram_pairs(rollup.hist)),
    )


def _row_to_rollup(row: aiosqlite.Row) -> tuple[RollupKey, Rollup]:
    key: RollupKey = (
        row["surface"],
        row["source_key"],
        row["endpoint"],
        row["op"],
    )
    pairs = [(int(v), int(c)) for v, c in json.loads(row["hist_blob"])]
    rollup = Rollup(
        count=row["count"],
        error_count=row["error_count"],
        sum_ms=row["sum_ms"],
        min_ms=row["min_ms"],
        max_ms=row["max_ms"],
        hist=histogram_from_pairs(pairs),
    )
    return key, rollup


def _event_to_json(ev: MetricEvent) -> str:
    return json.dumps(asdict(ev))


async def _migrate_ring_events(conn: aiosqlite.Connection) -> None:
    """DROP+recreate ``ring_events`` if it predates the append-only schema.

    There is NO migration framework (mirrors ``jobs/store._migrate_add_manga_title``).
    The OLD ring_events shape (``ring/seq/payload``) was ephemeral-by-design
    (full-rewrite, bounded by the deque maxlen, repopulated each snapshot), so any
    rows in an OLD-shape ``metrics.db`` are at most one post-restart deque-worth —
    safe to discard. Detect the old shape via ``PRAGMA table_info`` (absent ``ts``
    or ``id`` column) and DROP the table, then recreate it under the new schema +
    indexes. The ``rollups`` table is untouched. A fresh DB already has the new
    shape from ``_CREATE_SCHEMA``, so the PRAGMA check makes this a no-op there.
    """
    async with conn.execute("PRAGMA table_info(ring_events)") as cur:
        cols = {row["name"] for row in await cur.fetchall()}
    if not cols:
        return  # table absent (will be created by _CREATE_SCHEMA / already created)
    if "ts" in cols and "id" in cols:
        return  # already the new shape
    _log.warning(
        "metrics.db ring_events is the pre-260604-wm2 shape (cols=%s); "
        "dropping + recreating under the append-only schema (the discarded rows "
        "are at most one post-restart deque-worth of the old ephemeral model)",
        sorted(cols),
    )
    await conn.execute("DROP TABLE ring_events")
    await conn.commit()
    await conn.executescript(_CREATE_SCHEMA)
    await conn.commit()


async def open_metric_store(path: str) -> MetricSnapshotStore:
    """Open (and schema-init/migrate) the metrics store at ``path``.

    Connects, enables WAL (Pitfall 7), sets a ``Row`` factory for dict-shaped
    reads, creates the tables + indexes if absent, runs the idempotent
    ring_events migration for a pre-260604-wm2 DB, and returns a ready
    :class:`MetricSnapshotStore`. Mirrors ``jobs/store.py``'s ``open_store``.
    """
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(_CREATE_SCHEMA)
    await conn.commit()
    # Migrate an existing OLD-shape ring_events (no migration framework). Fresh DBs
    # already have the new shape from _CREATE_SCHEMA, so this is a no-op there.
    await _migrate_ring_events(conn)
    return MetricSnapshotStore(conn)
