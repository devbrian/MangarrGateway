"""aiosqlite ``JobStore`` tests (Task 2 — PLAT-03, D-27/28, JOB-01).

Covers the schema + the partial unique index (one LIVE job per ``releaseHandle``,
D-27), write-through round-trip (insert/get/update/delete/find_live_by_handle),
the duplicate-live-handle integrity guard, the re-grab-after-terminal allowance,
and the restart rehydration that flips live jobs to ``failed`` while preserving
terminal jobs (D-28).

aiosqlite is async-native, so every store call is awaited; tests run under
``asyncio_mode = "auto"`` (pyproject) with no explicit event-loop plumbing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from manga_gateway.jobs.model import Job, JobStatus
from manga_gateway.jobs.store import open_store


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _make_job(
    job_id: str,
    release_handle: str,
    status: JobStatus = JobStatus.QUEUED,
    *,
    manga_title: str | None = None,
    chapter_number: float | None = None,
) -> Job:
    ts = _now()
    return Job(
        job_id=job_id,
        release_handle=release_handle,
        source_key="mangadex",
        title="Chapter 1",
        status=status,
        manga_id=42,
        output_format="cbz",
        chapter_id="11111111-2222-3333-4444-555555555555",
        language="en",
        total_pages=None,
        downloaded_pages=None,
        total_bytes=0,
        remaining_bytes=0,
        output_path=None,
        message=None,
        created_at=ts,
        updated_at=ts,
        completed_at=None,
        manga_title=manga_title,
        chapter_number=chapter_number,
    )


# OLD column-less jobs schema (pre-#16): a literal snapshot of _CREATE_SCHEMA
# WITHOUT the manga_title column, used to seed a "production" DB and prove the
# additive ALTER migration in open_store. Kept inline (not imported) so it stays a
# fixed historical artifact even as _CREATE_SCHEMA evolves.
_OLD_SCHEMA_NO_MANGA_TITLE = """
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
  completed_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_live_handle
  ON jobs(release_handle)
  WHERE status IN ('queued','resolving','downloading','archiving');
"""


# OLD schema (post-#16, pre-260605-nqo): HAS manga_title but LACKS chapter_number.
# Used to prove the additive chapter_number ALTER in open_store on a DB that already
# went through the manga_title migration. Kept inline as a fixed historical artifact.
_OLD_SCHEMA_NO_CHAPTER_NUMBER = """
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
  manga_title      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_live_handle
  ON jobs(release_handle)
  WHERE status IN ('queued','resolving','downloading','archiving');
"""


async def _table_columns(db: str) -> set[str]:
    conn = await aiosqlite.connect(db)
    try:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("PRAGMA table_info(jobs)") as cur:
            rows = await cur.fetchall()
        return {row["name"] for row in rows}
    finally:
        await conn.close()


async def test_open_store_creates_table_and_partial_index(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        conn = store._conn  # introspect schema via sqlite_master
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ) as cur:
            assert await cur.fetchone() is not None
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ix_jobs_live_handle'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        # It is a PARTIAL unique index (D-27 — only live statuses).
        assert "WHERE" in row[0].upper()
        assert "UNIQUE" in row[0].upper()
    finally:
        await store.close()


async def test_insert_then_get_round_trips(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        job = _make_job("j_a", "handle-a")
        await store.insert(job)
        got = await store.get("j_a")
        assert got is not None
        assert got.job_id == "j_a"
        assert got.status is JobStatus.QUEUED
        assert got.title == "Chapter 1"
        assert got.release_handle == "handle-a"
        assert got.source_key == "mangadex"
        assert got.chapter_id == job.chapter_id
    finally:
        await store.close()


async def test_insert_round_trips_manga_title(tmp_path: Path) -> None:
    # #16: manga_title is durable submit-time data — it survives insert→get.
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        await store.insert(_make_job("j_mt", "h-mt", manga_title="Some Series"))
        got = await store.get("j_mt")
        assert got is not None
        assert got.manga_title == "Some Series"
        # Fresh DB carries the column.
        assert "manga_title" in await _table_columns(db)
    finally:
        await store.close()


async def test_insert_round_trips_chapter_number(tmp_path: Path) -> None:
    # 260605-nqo: chapter_number is durable submit-time data — it survives insert→get.
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        await store.insert(_make_job("j_cn", "h-cn", chapter_number=1.5))
        got = await store.get("j_cn")
        assert got is not None
        assert got.chapter_number == 1.5
        # Fresh DB carries the column.
        assert "chapter_number" in await _table_columns(db)
    finally:
        await store.close()


async def test_open_store_alter_migrates_chapter_numberless_db(tmp_path: Path) -> None:
    # 260605-nqo: an EXISTING jobs.db that has manga_title but predates chapter_number
    # must gain the column via the idempotent additive ALTER in open_store.
    db = str(tmp_path / "jobs.db")
    seed = await aiosqlite.connect(db)
    try:
        await seed.executescript(_OLD_SCHEMA_NO_CHAPTER_NUMBER)
        # Seed a legacy row (no chapter_number column) to prove it reads back NULL.
        await seed.execute(
            "INSERT INTO jobs (job_id, release_handle, source_key, title, status, "
            "output_format, total_bytes, remaining_bytes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
            (
                "j_legacy",
                "h-legacy",
                "mangadex",
                "Chapter 1",
                "completed",
                "cbz",
                _now(),
                _now(),
            ),
        )
        await seed.commit()
    finally:
        await seed.close()
    # Sanity: the seeded DB lacks chapter_number (but has manga_title).
    cols_before = await _table_columns(db)
    assert "chapter_number" not in cols_before
    assert "manga_title" in cols_before

    store = await open_store(db)
    try:
        # open_store added the column.
        assert "chapter_number" in await _table_columns(db)
        # Pre-migration rows read back chapter_number=NULL (no backfill).
        legacy = await store.get("j_legacy")
        assert legacy is not None
        assert legacy.chapter_number is None
        # ...and read/write through the migrated column round-trips.
        await store.insert(_make_job("j_cm", "h-cm", chapter_number=72.8))
        got = await store.get("j_cm")
        assert got is not None
        assert got.chapter_number == 72.8
    finally:
        await store.close()


async def test_open_store_alter_migrates_columnless_db(tmp_path: Path) -> None:
    # #16: an EXISTING production jobs.db created BEFORE the column must gain it via
    # the idempotent additive ALTER in open_store (no migration framework).
    db = str(tmp_path / "jobs.db")
    seed = await aiosqlite.connect(db)
    try:
        await seed.executescript(_OLD_SCHEMA_NO_MANGA_TITLE)
        await seed.commit()
    finally:
        await seed.close()
    # Sanity: the seeded DB lacks both additive columns.
    cols_before = await _table_columns(db)
    assert "manga_title" not in cols_before
    assert "chapter_number" not in cols_before

    store = await open_store(db)
    try:
        # open_store added BOTH additive columns (manga_title + chapter_number).
        cols_after = await _table_columns(db)
        assert "manga_title" in cols_after
        assert "chapter_number" in cols_after
        # ...and read/write through them round-trips.
        await store.insert(
            _make_job(
                "j_m", "h-m", manga_title="Migrated Series", chapter_number=30.1
            )
        )
        got = await store.get("j_m")
        assert got is not None
        assert got.manga_title == "Migrated Series"
        assert got.chapter_number == 30.1
    finally:
        await store.close()


async def test_open_store_alter_is_idempotent_on_reopen(tmp_path: Path) -> None:
    # #16: re-opening an already-migrated DB must NOT raise (the guard swallows the
    # duplicate-column case).
    db = str(tmp_path / "jobs.db")
    store1 = await open_store(db)
    await store1.close()
    store2 = await open_store(db)  # second open over the migrated DB
    try:
        assert "manga_title" in await _table_columns(db)
    finally:
        await store2.close()


async def test_get_unknown_returns_none(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        assert await store.get("nope") is None
    finally:
        await store.close()


async def test_update_persists_status_transition(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        job = _make_job("j_u", "handle-u")
        await store.insert(job)
        job.status = JobStatus.DOWNLOADING
        job.downloaded_pages = 3
        job.message = "in progress"
        await store.update(job)
        got = await store.get("j_u")
        assert got is not None
        assert got.status is JobStatus.DOWNLOADING
        assert got.downloaded_pages == 3
        assert got.message == "in progress"
    finally:
        await store.close()


async def test_duplicate_live_handle_raises(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        await store.insert(_make_job("j_1", "same-handle", JobStatus.QUEUED))
        # A second LIVE job for the SAME handle violates the partial unique index.
        with pytest.raises(aiosqlite.IntegrityError):
            await store.insert(_make_job("j_2", "same-handle", JobStatus.RESOLVING))
    finally:
        await store.close()


async def test_same_handle_inserts_after_terminal(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        first = _make_job("j_term", "regrab-handle", JobStatus.QUEUED)
        await store.insert(first)
        # Move the first job to a TERMINAL status — frees the handle (D-27).
        first.status = JobStatus.COMPLETED
        await store.update(first)
        # Re-grab: a new live job with the same handle now inserts cleanly.
        await store.insert(_make_job("j_regrab", "regrab-handle", JobStatus.QUEUED))
        assert await store.get("j_regrab") is not None
    finally:
        await store.close()


async def test_find_live_by_handle(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        await store.insert(_make_job("j_live", "find-handle", JobStatus.DOWNLOADING))
        live = await store.find_live_by_handle("find-handle")
        assert live is not None
        assert live.job_id == "j_live"
        assert await store.find_live_by_handle("unknown-handle") is None
    finally:
        await store.close()


async def test_find_live_by_handle_ignores_terminal(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        job = _make_job("j_done", "done-handle", JobStatus.QUEUED)
        await store.insert(job)
        job.status = JobStatus.FAILED
        await store.update(job)
        # A terminal job is NOT "live" — find_live_by_handle returns None (D-27).
        assert await store.find_live_by_handle("done-handle") is None
    finally:
        await store.close()


async def test_find_latest_by_handle_returns_most_recently_updated(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        old = _make_job("j_old", "h-dup", JobStatus.QUEUED)
        await store.insert(old)
        old.status = JobStatus.FAILED  # terminal first, so the next live insert is ok
        await store.update(old)

        new = _make_job("j_new", "h-dup", JobStatus.QUEUED)
        await store.insert(new)
        new.status = JobStatus.COMPLETED
        await store.update(new)  # stamps a strictly later updated_at

        latest = await store.find_latest_by_handle("h-dup")
        assert latest is not None
        assert latest.job_id == "j_new"  # freshest updated_at wins
        assert await store.find_latest_by_handle("no-such-handle") is None
    finally:
        await store.close()


async def test_find_latest_by_handle_breaks_ties_by_rowid(tmp_path: Path) -> None:
    # WR-01: with an identical updated_at the deterministic ", rowid DESC" tiebreak
    # returns the later-inserted row, never an arbitrary SQLite row order.
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        await store.insert(_make_job("j_first", "h-tie", JobStatus.COMPLETED))
        await store.insert(_make_job("j_second", "h-tie", JobStatus.COMPLETED))
        # Force a clock tie across both rows.
        await store._conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE release_handle = ?",
            ("2026-01-01T00:00:00+00:00", "h-tie"),
        )
        await store._conn.commit()

        latest = await store.find_latest_by_handle("h-tie")
        assert latest is not None
        assert latest.job_id == "j_second"  # higher rowid wins the tie
    finally:
        await store.close()


async def test_delete_removes_row(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        await store.insert(_make_job("j_del", "del-handle"))
        await store.delete("j_del")
        assert await store.get("j_del") is None
    finally:
        await store.close()


async def test_all_returns_every_row(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        await store.insert(_make_job("j_x", "h-x"))
        await store.insert(_make_job("j_y", "h-y"))
        rows = await store.all()
        assert {j.job_id for j in rows} == {"j_x", "j_y"}
    finally:
        await store.close()


async def test_rehydrate_flips_live_to_failed(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        await store.insert(_make_job("j_q", "h-q", JobStatus.QUEUED))
        await store.insert(_make_job("j_r", "h-r", JobStatus.RESOLVING))
        await store.insert(_make_job("j_d", "h-d", JobStatus.DOWNLOADING))
        await store.insert(_make_job("j_a", "h-a", JobStatus.ARCHIVING))
    finally:
        await store.close()

    # Simulate a restart: re-open and rehydrate.
    store2 = await open_store(db)
    try:
        projected = await store2.rehydrate()
        by_id = {j.job_id: j for j in projected}
        for jid in ("j_q", "j_r", "j_d", "j_a"):
            assert by_id[jid].status is JobStatus.FAILED
            assert by_id[jid].message == "interrupted by restart"
    finally:
        await store2.close()


async def test_rehydrate_preserves_terminal_jobs(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        done = _make_job("j_done", "h-done", JobStatus.QUEUED)
        await store.insert(done)
        done.status = JobStatus.COMPLETED
        done.message = "ok"
        await store.update(done)

        failed = _make_job("j_fail", "h-fail", JobStatus.QUEUED)
        await store.insert(failed)
        failed.status = JobStatus.FAILED
        failed.message = "page 4 unrecoverable"
        await store.update(failed)
    finally:
        await store.close()

    store2 = await open_store(db)
    try:
        projected = await store2.rehydrate()
        by_id = {j.job_id: j for j in projected}
        # Terminal jobs survive a restart untouched (D-28).
        assert by_id["j_done"].status is JobStatus.COMPLETED
        assert by_id["j_done"].message == "ok"
        assert by_id["j_fail"].status is JobStatus.FAILED
        assert by_id["j_fail"].message == "page 4 unrecoverable"
    finally:
        await store2.close()
