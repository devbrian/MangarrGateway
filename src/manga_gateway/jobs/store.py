"""aiosqlite raw-SQL ``JobStore`` — durable job persistence (PLAT-03, D-27/28).

A single :class:`aiosqlite.Connection` owned by the lifespan backs the ``jobs``
table. The mechanic differs from :mod:`manga_gateway.handles.store` (SQLite vs
in-memory ``TTLCache``) but the SHAPE is the same: one class owning one backing
store, narrow methods, docstring citing D-numbers.

Two structural guarantees:

* **One LIVE job per ``releaseHandle`` (D-27)** — a PARTIAL unique index on
  ``release_handle`` restricted to live statuses. A duplicate live insert raises
  ``aiosqlite.IntegrityError``; once a job reaches a TERMINAL status the handle is
  freed so the release can be re-grabbed (DELETE/failure path).
* **Restart rehydration (PLAT-03/D-28)** — :meth:`rehydrate` REQUEUES every job
  left in a live status by an unclean shutdown back to ``queued`` (clearing its
  transient progress) so the gateway can RESUME it ("jobs SHOULD survive restart");
  the :class:`~manga_gateway.jobs.manager.JobManager` re-spawns the requeued rows.
  TERMINAL jobs survive untouched.

NO at-home ``baseUrl``/cookie column exists (HDL-01/D-17) — volatile tokens are
never persisted. Writes are serialized behind a single ``asyncio.Lock`` and run
under WAL (Pitfall 7); aiosqlite is async-native so every call is awaited (no
``to_thread``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiosqlite

from .model import _LIVE_STATUSES, Job, JobStatus

# One live job per handle (D-27): the index applies ONLY while a job is live, so a
# handle becomes re-grabbable once its job is completed/failed. Built from the
# single source of truth in model.py so the index, find_live, and rehydrate agree.
_LIVE_SET_SQL = "(" + ",".join(f"'{s}'" for s in _LIVE_STATUSES) + ")"

_CREATE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
  job_id           TEXT PRIMARY KEY,
  release_handle   TEXT NOT NULL,
  source_key       TEXT NOT NULL,
  title            TEXT NOT NULL,
  status           TEXT NOT NULL,
  manga_id         INTEGER,
  output_format    TEXT NOT NULL,
  chapter_id       TEXT,
  language         TEXT,
  total_pages      INTEGER,
  downloaded_pages INTEGER,
  total_bytes      INTEGER NOT NULL DEFAULT 0,
  remaining_bytes  INTEGER NOT NULL DEFAULT 0,
  output_path      TEXT,
  message          TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  completed_at     TEXT,
  manga_title      TEXT,
  chapter_number   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_live_handle
  ON jobs(release_handle)
  WHERE status IN {_LIVE_SET_SQL};
"""

_COLUMNS = (
    "job_id",
    "release_handle",
    "source_key",
    "title",
    "status",
    "manga_id",
    "output_format",
    "chapter_id",
    "language",
    "total_pages",
    "downloaded_pages",
    "total_bytes",
    "remaining_bytes",
    "output_path",
    "message",
    "created_at",
    "updated_at",
    "completed_at",
    "manga_title",
    "chapter_number",
)

_INSERT_SQL = (
    f"INSERT INTO jobs ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)

_UPDATE_SQL = (
    "UPDATE jobs SET "
    + ", ".join(f"{c} = ?" for c in _COLUMNS if c != "job_id")
    + " WHERE job_id = ?"
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        release_handle=row["release_handle"],
        source_key=row["source_key"],
        title=row["title"],
        status=JobStatus(row["status"]),
        manga_id=row["manga_id"],
        output_format=row["output_format"],
        chapter_id=row["chapter_id"],
        language=row["language"],
        total_pages=row["total_pages"],
        downloaded_pages=row["downloaded_pages"],
        total_bytes=row["total_bytes"],
        remaining_bytes=row["remaining_bytes"],
        output_path=row["output_path"],
        message=row["message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        manga_title=row["manga_title"],
        chapter_number=row["chapter_number"],
    )


def _job_values(job: Job) -> tuple[object, ...]:
    return (
        job.job_id,
        job.release_handle,
        job.source_key,
        job.title,
        job.status.value,
        job.manga_id,
        job.output_format,
        job.chapter_id,
        job.language,
        job.total_pages,
        job.downloaded_pages,
        job.total_bytes,
        job.remaining_bytes,
        job.output_path,
        job.message,
        job.created_at,
        job.updated_at,
        job.completed_at,
        job.manga_title,
        job.chapter_number,
    )


class JobStore:
    """Owns one ``aiosqlite.Connection`` for the ``jobs`` table (PLAT-03).

    Construct via :func:`open_store` (which creates the schema). All writes go
    through a single :class:`asyncio.Lock` so the shared connection never sees
    concurrent statements (Pitfall 7); reads share the same connection.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._write_lock = asyncio.Lock()

    async def insert(self, job: Job) -> None:
        """Persist a new job. Raises ``aiosqlite.IntegrityError`` on a duplicate
        live ``release_handle`` (the partial unique index, D-27).
        """
        async with self._write_lock:
            await self._conn.execute(_INSERT_SQL, _job_values(job))
            await self._conn.commit()

    async def update(self, job: Job) -> None:
        """Write-through a status/progress transition (called BEFORE the in-memory
        projection mutates, RESEARCH Pattern 2). Stamps ``updated_at``.
        """
        job.updated_at = _now_iso()
        values = _job_values(job)
        # _UPDATE_SQL sets every column except job_id, then matches on job_id.
        params = (*values[1:], job.job_id)
        async with self._write_lock:
            cur = await self._conn.execute(_UPDATE_SQL, params)
            # Fail fast on a zero-row update: an unknown/deleted job_id must not be
            # treated as a durable transition, or in-memory and SQLite state would
            # desync across restart (CR).
            if cur.rowcount != 1:
                raise KeyError(f"job not found: {job.job_id}")
            await self._conn.commit()

    async def get(self, job_id: str) -> Job | None:
        """Return the job by id, or ``None`` if unknown."""
        async with self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row is not None else None

    async def find_live_by_handle(self, release_handle: str) -> Job | None:
        """Return the single LIVE job for ``release_handle``, or ``None`` (D-27)."""
        async with self._conn.execute(
            f"SELECT * FROM jobs WHERE release_handle = ? "
            f"AND status IN {_LIVE_SET_SQL}",
            (release_handle,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row is not None else None

    async def find_latest_by_handle(self, release_handle: str) -> Job | None:
        """Return the most-recently-updated job for ``release_handle`` (D-27).

        Used by the idempotency-by-existence check: after no LIVE job is found, the
        manager inspects the latest job for this handle and, if it is ``completed``
        with its output file still on disk, returns its id (single os.stat per submit,
        DL-05). Ordered by ``updated_at`` descending, then ``rowid`` descending as a
        deterministic tiebreak so same-timestamp rows resolve consistently (WR-01:
        freshest insert wins).
        """
        async with self._conn.execute(
            "SELECT * FROM jobs WHERE release_handle = ? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (release_handle,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row is not None else None

    async def delete(self, job_id: str) -> None:
        """Remove a job row (DELETE /downloads path — frees the handle, D-27)."""
        async with self._write_lock:
            await self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            await self._conn.commit()

    async def all(self) -> list[Job]:
        """Project every persisted job (queue + history read model, DL-05)."""
        async with self._conn.execute("SELECT * FROM jobs") as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def rehydrate(self) -> list[Job]:
        """Restart recovery (PLAT-03/D-28): REQUEUE live jobs, project all.

        Every job left in a live status by an unclean shutdown (a redeploy/crash)
        is set back to ``queued`` so the gateway can RESUME it rather than leaving
        it permanently ``failed`` — the CLAUDE.md edge case "jobs SHOULD survive
        restart". A requeued job re-runs identically to a fresh submit: the engine
        drives the manifest off the durable ``chapter_id`` column (no dependency on
        the volatile in-memory HandleStore, which is wiped on restart), and the
        atomic-publish writers (D-26) mean a partially-fetched/archived job simply
        re-fetches with no partial-CBZ risk. The transient in-flight progress
        (``message``, ``total_pages``/``downloaded_pages``, byte counters,
        ``output_path``) is cleared so the resumed job starts from a clean baseline;
        the durable resolution snapshot (chapter id, title, manga/series ids,
        handle, format) is preserved. TERMINAL jobs (completed/failed) are untouched
        and survive the restart.

        Returns all rows projected for rehydrating the in-memory queue; the
        :class:`~manga_gateway.jobs.manager.JobManager` re-spawns the now-``queued``
        rows via :meth:`~manga_gateway.jobs.manager.JobManager.resume_interrupted`.
        """
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE jobs SET status = 'queued', message = NULL, "
                "total_pages = NULL, downloaded_pages = NULL, "
                "total_bytes = 0, remaining_bytes = 0, output_path = NULL, "
                f"updated_at = ? WHERE status IN {_LIVE_SET_SQL}",
                (_now_iso(),),
            )
            await self._conn.commit()
        return await self.all()

    async def prune_terminal(self, keep_last: int) -> int:
        """Trim terminal-status rows so at most ``keep_last`` survive (IN-05).

        Keeps the ``keep_last`` most-recently-updated TERMINAL (completed/failed)
        rows; older terminal rows are deleted. LIVE rows are never touched.
        Returns the number of rows deleted. ``keep_last < 1`` is treated as 1 to
        keep the bound meaningful (``Field(ge=1)`` already enforces this on the
        Settings side, this is belt-and-suspenders).
        """
        keep = max(1, keep_last)
        async with self._write_lock:
            cur = await self._conn.execute(
                f"DELETE FROM jobs WHERE status NOT IN {_LIVE_SET_SQL} "
                "AND job_id NOT IN ("
                f"  SELECT job_id FROM jobs WHERE status NOT IN {_LIVE_SET_SQL} "
                "   ORDER BY updated_at DESC, rowid DESC LIMIT ?"
                ")",
                (keep,),
            )
            deleted = cur.rowcount or 0
            await cur.close()
            await self._conn.commit()
        return deleted

    async def close(self) -> None:
        """Close the backing connection (lifespan teardown)."""
        await self._conn.close()


async def _migrate_add_manga_title(conn: aiosqlite.Connection) -> None:
    """Idempotent additive migration: ensure the ``jobs.manga_title`` column exists.

    There is NO migration framework here — ``open_store`` only runs
    ``CREATE TABLE IF NOT EXISTS`` (``_CREATE_SCHEMA``), which leaves an EXISTING
    production ``jobs.db`` (created before #16) without the new ``manga_title``
    column. This additive ALTER is the deliberate mechanism for evolving the live
    schema: we PRAGMA-check the current columns and only ``ALTER TABLE ... ADD
    COLUMN`` when absent, so a re-open of an already-migrated DB is a no-op and never
    raises (T-q7x-02). The duplicate-column ``OperationalError`` is also caught as a
    belt-and-suspenders guard against a concurrent add. SQLite appends the column
    physically at the end — which is why ``_COLUMNS`` (SELECT-by-name) is the source
    of truth for read order, not physical column position.
    """
    async with conn.execute("PRAGMA table_info(jobs)") as cur:
        cols = {row["name"] for row in await cur.fetchall()}
    if "manga_title" in cols:
        return
    try:
        await conn.execute("ALTER TABLE jobs ADD COLUMN manga_title TEXT")
        await conn.commit()
    except aiosqlite.OperationalError as exc:  # pragma: no cover - race guard
        if "duplicate column name" not in str(exc).lower():
            raise


async def _migrate_add_chapter_number(conn: aiosqlite.Connection) -> None:
    """Idempotent additive migration: ensure the ``jobs.chapter_number`` column exists.

    Mirrors :func:`_migrate_add_manga_title` VERBATIM (260605-nqo): there is NO
    migration framework, so an EXISTING ``jobs.db`` (created before this column)
    needs the additive ALTER. We PRAGMA-check the current columns and only
    ``ALTER TABLE jobs ADD COLUMN chapter_number REAL`` when absent, so a re-open of
    an already-migrated/fresh DB is a no-op and never raises. The duplicate-column
    ``OperationalError`` is caught as a belt-and-suspenders race guard. SQLite appends
    the column physically at the end — ``_COLUMNS`` (SELECT-by-name) stays the source
    of truth for read order, not physical column position. Pre-migration rows read back
    ``chapter_number=NULL`` (no backfill).
    """
    async with conn.execute("PRAGMA table_info(jobs)") as cur:
        cols = {row["name"] for row in await cur.fetchall()}
    if "chapter_number" in cols:
        return
    try:
        await conn.execute("ALTER TABLE jobs ADD COLUMN chapter_number REAL")
        await conn.commit()
    except aiosqlite.OperationalError as exc:  # pragma: no cover - race guard
        if "duplicate column name" not in str(exc).lower():
            raise


async def open_store(path: str) -> JobStore:
    """Open (and schema-init) the job store at ``path``.

    Connects, enables WAL (Pitfall 7), sets a ``Row`` factory for dict-shaped
    reads, creates the table + partial unique index if absent, runs the idempotent
    additive ``manga_title`` (pre-#16) and ``chapter_number`` (pre-260605-nqo)
    migrations, and returns a ready :class:`JobStore`.
    """
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(_CREATE_SCHEMA)
    await conn.commit()
    # Additive migration for an existing column-less jobs.db (no migration
    # framework — see _migrate_add_manga_title). Fresh DBs already have the column
    # from _CREATE_SCHEMA, so the PRAGMA check makes this a no-op there.
    await _migrate_add_manga_title(conn)
    await _migrate_add_chapter_number(conn)
    return JobStore(conn)
