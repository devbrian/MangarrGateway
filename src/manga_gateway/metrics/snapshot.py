"""aiosqlite metrics snapshot store — periodic durability (OBS-04, D-28-style).

Mirrors :mod:`manga_gateway.jobs.store` EXACTLY in shape: one
:class:`aiosqlite.Connection` + one :class:`asyncio.Lock`, WAL journal mode, an
``open_metric_store(path)`` async factory, per-write
``async with self._write_lock: ... commit()``, a ``rehydrate()`` that reloads the
last snapshot into a fresh :class:`InMemoryStore`, and ``close()``. aiosqlite is
async-native — every call is awaited, NO ``asyncio.to_thread`` (it would only add a
thread hop around a non-blocking driver).

The snapshot DB is a SEPARATE file from ``gateway.db`` (its own ``path``; default
``/state/metrics.db`` via ``Settings.metrics_db_path``, passed by the caller in
Plan 05) so metrics persistence never contends with the job store.

``snapshot(mem)`` flushes the WHOLE store in ONE transaction (DELETE then
``executemany`` then a SINGLE commit) — never a per-event write (the spike-005 716×
hot-path tax). The background snapshot timer (Plan 05) runs this on a ~45s cadence
plus a final flush at shutdown.

**Histogram serialization (CRITICAL — cross-platform).** The spike/PATTERNS
suggested ``HdrHistogram.encode()`` → ``decode_and_add(blob)``, but BOTH ``encode()``
and ``add()`` raise ``OverflowError: Python int too large to convert to C long`` on
the Windows dev host (LLP64 32-bit ``c_long``; the deploy target is Linux but the
``nox -s gate`` must be green on Windows too — see 08-01-SUMMARY known-issue). So the
``hist_blob TEXT`` column instead stores the compact populated ``(value, count)``
pair list (via :func:`histogram_pairs`) as JSON; ``rehydrate()`` replays
``record_value(value, count)`` into a fresh ``HdrHistogram(1, 600_000, 2)`` (via
:func:`histogram_from_pairs`). This round-trips p50/p95/total_count EXACTLY on both
Windows and Linux.

**Durability bound (≤ snapshot interval).** A hard crash between snapshots loses at
most one interval's worth (≤45s default) of in-memory metrics — accepted, since
metrics are diagnostic and ``rehydrate()`` of a fresh/empty DB degrades cleanly to
an empty store. This mirrors ``jobs/store.py``'s "interrupted by restart" honesty.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
  ring   TEXT NOT NULL,   -- 'recent' | 'failures' | 'slow'
  seq    INTEGER NOT NULL,-- preserves order within a ring
  payload TEXT NOT NULL   -- json.dumps(asdict(MetricEvent))
);
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
_RING_INSERT = "INSERT INTO ring_events (ring, seq, payload) VALUES (?, ?, ?)"


class MetricSnapshotStore:
    """Owns one ``aiosqlite.Connection`` for the metrics snapshot (OBS-04).

    Construct via :func:`open_metric_store` (which creates the schema). The single
    :class:`asyncio.Lock` serializes the snapshot transaction so the shared
    connection never sees concurrent statements (Pitfall 7).
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._write_lock = asyncio.Lock()

    async def snapshot(self, mem: InMemoryStore) -> None:
        """Flush the whole in-memory store in ONE transaction (DELETE + insert +
        single commit). Re-snapshot is idempotent (clears then re-inserts)."""
        rollup_rows = [
            _rollup_values(key, rollup) for key, rollup in mem.iter_rollups()
        ]
        ring_rows: list[tuple[str, int, str]] = []
        for ring_name, events in (
            ("recent", mem.iter_recent()),
            ("failures", mem.iter_failures()),
            ("slow", mem.iter_slow()),
        ):
            for seq, ev in enumerate(events):
                ring_rows.append((ring_name, seq, _event_to_json(ev)))

        async with self._write_lock:
            await self._conn.execute("DELETE FROM rollups")
            await self._conn.execute("DELETE FROM ring_events")
            if rollup_rows:
                await self._conn.executemany(_ROLLUP_INSERT, rollup_rows)
            if ring_rows:
                await self._conn.executemany(_RING_INSERT, ring_rows)
            await self._conn.commit()

    async def rehydrate(self) -> InMemoryStore:
        """Reload the last snapshot into a fresh ``InMemoryStore``.

        Ring sizes are intentionally generous on rehydrate: the snapshot already
        bounded what it persisted, so the rehydrated rings can hold exactly the
        persisted rows. The live store built by the lifespan (Plan 05) uses the
        configured bounds; this rehydrate target only needs to re-admit the
        persisted events verbatim (bypassing slow/failure re-classification).
        """
        async with self._conn.execute("SELECT * FROM rollups") as cur:
            rollup_rows = await cur.fetchall()
        async with self._conn.execute(
            "SELECT ring, seq, payload FROM ring_events ORDER BY ring, seq"
        ) as cur:
            ring_rows = await cur.fetchall()

        # Size the rehydrated rings to exactly what was persisted (no eviction).
        counts = {"recent": 0, "failures": 0, "slow": 0}
        for row in ring_rows:
            counts[row["ring"]] += 1
        mem = InMemoryStore(
            recent_max=max(1, counts["recent"]),
            failures_max=max(1, counts["failures"]),
            slow_max=max(1, counts["slow"]),
            slow_factor=3.0,
        )

        for row in rollup_rows:
            key, rollup = _row_to_rollup(row)
            mem.restore_rollup(key, rollup)

        restore = {
            "recent": mem.restore_recent,
            "failures": mem.restore_failure,
            "slow": mem.restore_slow,
        }
        for row in ring_rows:
            # WR-02: salvage a partially-valid snapshot — a single corrupt /
            # schema-drifted row is skipped rather than aborting the whole
            # rehydrate (metrics must never break the service).
            try:
                ev = _event_from_json(row["payload"])
            except Exception:  # noqa: BLE001 — drop the bad row, keep the rest
                _log.warning("skipping corrupt metric snapshot row", exc_info=True)
                continue
            restore[row["ring"]](ev)

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


def _event_from_json(payload: str) -> MetricEvent:
    return MetricEvent(**json.loads(payload))


async def open_metric_store(path: str) -> MetricSnapshotStore:
    """Open (and schema-init) the metrics snapshot store at ``path``.

    Connects, enables WAL (Pitfall 7), sets a ``Row`` factory for dict-shaped
    reads, creates the snapshot tables if absent, and returns a ready
    :class:`MetricSnapshotStore`. Mirrors ``jobs/store.py``'s ``open_store``.
    """
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(_CREATE_SCHEMA)
    await conn.commit()
    return MetricSnapshotStore(conn)
