"""``JobManager`` unit tests (Task 2 — DL-05, R5, D-30, PLAT-03, Pitfall 1).

Exercises the manager semantics in ISOLATION from the routes/E2E: a JobManager is
constructed against a tmp_path ``JobStore`` and its background ``_engine`` is replaced
with a controllable stub so the five contracted behaviors are asserted without the full
HTTP/fetch stack:

1. ``submit`` mints a ``j_`` jobId, write-through-inserts BEFORE the projection,
   schedules a task, returns ``(jobId, "queued")`` (R5/DL-01).
2. The spawned task is held in a strong-ref set while running and discarded via
   ``add_done_callback`` once done (Pitfall 1).
3. ``list()`` reads only the in-memory projection — no store/disk read (DL-05).
4. A global ``Semaphore(max_concurrent_chapters)`` bounds concurrent running jobs;
   extras stay queued until a slot frees (D-30).
5. ``rehydrate()`` populates the projection from store rows reflecting the
   store's in-flight→requeued flip, and ``resume_interrupted()`` re-spawns the
   requeued jobs so interrupted work survives a restart (PLAT-03,
   download-jobs-failed-23).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from manga_gateway.config import Settings
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore, ResolutionRecord
from manga_gateway.jobs.manager import JobManager
from manga_gateway.jobs.model import Job, JobStatus
from manga_gateway.jobs.store import JobStore, open_store
from manga_gateway.models.download import SubmitRequest


class _NullTransport:
    async def request(self, method: str, url: str, **kwargs: object) -> object:
        raise AssertionError("manager unit test must not perform real HTTP")

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _record() -> ResolutionRecord:
    return ResolutionRecord(
        source_key="mangadex",
        chapter_id="11111111-2222-3333-4444-555555555555",
        language="en",
        title="Solo Leveling - Chapter 1 (en)",
        manga_title="Solo Leveling",
        chapter_number=Decimal("1"),
        volume=None,
        scanlation_group="Team Lumikha",
        page_count=42,
    )


def _req(release_handle: str = "h_abc") -> SubmitRequest:
    return SubmitRequest(
        releaseHandle=release_handle,
        sourceKey="mangadex",
        mangaId=42,
        outputFormat="cbz",
    )


def _req_for(source_key: str, release_handle: str) -> SubmitRequest:
    """A submit request pinned to ``source_key`` (drives ``job.source_key``)."""
    return SubmitRequest(
        releaseHandle=release_handle,
        sourceKey=source_key,
        mangaId=42,
        outputFormat="cbz",
    )


async def _make_manager(
    store: JobStore,
    *,
    max_concurrent: int = 3,
    max_concurrent_per_source: int = 3,
    max_history_jobs: int = 500,
) -> JobManager:
    settings = Settings(
        api_key="k",
        output_root="/tmp/out",
        max_concurrent_chapters=max_concurrent,
        max_concurrent_per_source=max_concurrent_per_source,
        max_history_jobs=max_history_jobs,
    )
    registry = SourceRegistry()
    return JobManager(
        store=store,
        registry=registry,
        session=SessionManager(_NullTransport()),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        settings=settings,
    )


# ─────────────────────── 1. submit: mint + write-through + schedule ───────────


@pytest.mark.asyncio
async def test_submit_mints_prefixed_id_and_returns_queued(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        # Replace the engine with a no-op so the scheduled task completes immediately.
        mgr._engine = _NoopEngine()  # type: ignore[assignment]

        job_id, status = await mgr.submit(_record(), _req())

        assert job_id.startswith("j_")
        assert status == "queued"
        # Write-through: the store row exists immediately after submit returns.
        persisted = await store.get(job_id)
        assert persisted is not None
        assert persisted.release_handle == "h_abc"
        assert persisted.chapter_id == "11111111-2222-3333-4444-555555555555"
        assert persisted.manga_id == 42
        # The projection also holds it.
        assert mgr.get(job_id) is not None
        await mgr.drain()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_submit_sets_manga_title_from_record(tmp_path: Path) -> None:
    """#16: ``submit`` copies the resolved ``record.manga_title`` onto the Job so
    the engine can bucket the output under ``manga-{title}/`` — asserted via the
    store round-trip (``SubmitRequest`` carries no manga_title)."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        mgr._engine = _NoopEngine()  # type: ignore[assignment]

        job_id, _ = await mgr.submit(_record(), _req())

        persisted = await store.get(job_id)
        assert persisted is not None
        assert persisted.manga_title == "Solo Leveling"
        await mgr.drain()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_submit_carries_page_count_from_record(tmp_path: Path) -> None:
    """#83/IN-03: ``submit`` copies the resolved ``record.page_count`` onto the Job so
    the engine can forward it to ``fetch_manifest`` as an integrity hint. Asserted via
    the in-memory projection (``mgr.get``) — page_count is PROJECTION-ONLY, so it does
    NOT round-trip through the store (deliberate; see Job.page_count)."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        mgr._engine = _NoopEngine()  # type: ignore[assignment]

        job_id, _ = await mgr.submit(_record(), _req())

        job = mgr.get(job_id)
        assert job is not None
        assert job.page_count == 42  # from _record()
        await mgr.drain()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_submit_writes_store_before_projection(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        mgr._engine = _NoopEngine()  # type: ignore[assignment]

        seen: dict[str, bool] = {}
        original_insert = store.insert

        async def _spy_insert(job: Job) -> None:
            # At the moment of the store write, the projection must NOT yet hold it.
            seen["projection_empty_at_insert"] = job.job_id not in mgr._projection
            await original_insert(job)

        store.insert = _spy_insert  # type: ignore[method-assign]

        await mgr.submit(_record(), _req())
        assert seen["projection_empty_at_insert"] is True
        await mgr.drain()
    finally:
        await store.close()


# ─────────────────────── 2. task GC safety (Pitfall 1) ───────────────────────


@pytest.mark.asyncio
async def test_spawned_task_held_then_discarded(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        gate = asyncio.Event()
        mgr._engine = _GatedEngine(gate)  # type: ignore[assignment]

        await mgr.submit(_record(), _req())
        # While the engine is gated, the task is retained in the strong-ref set.
        assert len(mgr._tasks) == 1

        gate.set()
        await mgr.drain()
        # After completion the done-callback discarded it.
        assert len(mgr._tasks) == 0
    finally:
        await store.close()


# ─────────────────────── 3. list reads projection only (DL-05) ───────────────


@pytest.mark.asyncio
async def test_list_reads_projection_no_store_access(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        mgr._engine = _NoopEngine()  # type: ignore[assignment]
        await mgr.submit(_record(), _req("h1"))
        await mgr.drain()

        # Any store read during list() is a DL-05 violation.
        store.all = _boom  # type: ignore[method-assign]
        store.get = _boom  # type: ignore[method-assign]
        jobs = mgr.list()
        assert len(jobs) == 1
        assert jobs[0].job_id.startswith("j_")
    finally:
        await store.close()


# ─────────────────────── 4. global concurrency bound (D-30) ──────────────────


@pytest.mark.asyncio
async def test_global_semaphore_bounds_running_jobs(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store, max_concurrent=2)
        engine = _CountingEngine()
        mgr._engine = engine  # type: ignore[assignment]

        for i in range(4):
            await mgr.submit(_record(), _req(f"h{i}"))

        # Give the event loop a chance to start the unblocked tasks.
        await asyncio.sleep(0.05)
        # Only 2 may be running concurrently (the global semaphore cap).
        assert engine.peak_concurrent <= 2
        assert engine.currently_running <= 2

        engine.release_all()
        await mgr.drain()
        assert engine.total_completed == 4
    finally:
        await store.close()


# ──────────── 4b. per-source semaphore bound (WR-02) ─────────────────────────


@pytest.mark.asyncio
async def test_per_source_semaphore_caps_below_global(tmp_path: Path) -> None:
    """``max_concurrent_per_source`` constrains same-source concurrency below
    the global cap (WR-02). Four jobs on the same source with global=4 and
    per-source=1 must run strictly serially."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store, max_concurrent=4, max_concurrent_per_source=1)
        engine = _CountingEngine()
        mgr._engine = engine  # type: ignore[assignment]

        for i in range(4):
            await mgr.submit(_record(), _req(f"h{i}"))

        await asyncio.sleep(0.05)
        # Per-source cap of 1 keeps concurrency at 1 even with global=4.
        assert engine.peak_concurrent <= 1

        engine.release_all()
        await mgr.drain()
        assert engine.total_completed == 4
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_capped_source_deep_queue_does_not_starve_other_sources(
    tmp_path: Path,
) -> None:
    """A capped source's deep queue must not squat global slots (#267).

    With global=2 and a source capped at 1, a deep backlog of that source plus a
    job from a *different* source: the other source must still acquire a global
    slot and run concurrently. Under the old acquire order (global before
    per-source) the capped source's overflow tasks grabbed global permits and
    then blocked on the per-source cap — head-of-line blocking that left the
    other source's job stuck ``queued``."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store, max_concurrent=2, max_concurrent_per_source=1)
        engine = _SourceTrackingEngine()
        mgr._engine = engine  # type: ignore[assignment]

        # 4 jobs of a capped source (per-source=1) — a backlog deeper than global.
        for i in range(4):
            await mgr.submit(_record(), _req_for("comix", f"c{i}"))
        # 1 job of a different, healthy source.
        await mgr.submit(_record(), _req_for("mangadex", "m0"))

        await asyncio.sleep(0.05)

        # The healthy source must have started despite the capped backlog, and the
        # engine must reach 2 active across the two sources (>cap of either).
        assert "mangadex" in engine.started_sources
        assert engine.peak_concurrent == 2

        engine.release_all()
        await mgr.drain()
        assert engine.total_completed == 5
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_per_source_bound_honors_source_override_and_clamps(
    tmp_path: Path,
) -> None:
    """A source's ``max_concurrent_jobs`` override raises its per-source job bound
    above the global default (mangadot=3, measured safe), clamped to
    ``max_concurrent_chapters``; sources without an override (and unknown keys) fall
    back to ``max_concurrent_per_source`` (D-30)."""
    from manga_gateway.sources import register_builtin_sources

    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        registry = SourceRegistry()
        register_builtin_sources(registry)

        def _mgr(*, glob: int, per_source: int) -> JobManager:
            return JobManager(
                store=store,
                registry=registry,
                session=SessionManager(_NullTransport()),
                ratelimiter=RateLimiter(),
                handle_store=HandleStore(),
                settings=Settings(
                    api_key="k",
                    output_root="/tmp/out",
                    max_concurrent_chapters=glob,
                    max_concurrent_per_source=per_source,
                ),
            )

        wide = _mgr(glob=8, per_source=1)
        assert wide._per_source_bound("mangadot") == 3  # override applied
        assert wide._per_source_bound("mangadex") == 1  # no override -> setting
        assert wide._per_source_bound("does-not-exist") == 1  # unknown -> setting

        # Override is clamped to the global ceiling (WR-02 invariant).
        narrow = _mgr(glob=2, per_source=1)
        assert narrow._per_source_bound("mangadot") == 2
    finally:
        await store.close()


# ─────────────────────── 5. rehydrate populates projection (PLAT-03) ─────────


@pytest.mark.asyncio
async def test_rehydrate_requeues_live_job_into_projection(tmp_path: Path) -> None:
    # download-jobs-failed-23: an unclean shutdown leaves a live row; rehydrate
    # REQUEUES it (resume on restart) and projects it as ``queued``, NOT ``failed``.
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        mgr = await _make_manager(store)
        mgr._engine = _GatedEngine(asyncio.Event())  # never completes; stays live
        await mgr.submit(_record(), _req("h_live"))
    finally:
        # Simulate an unclean shutdown: close without draining the in-flight task.
        await store.close()

    # Reopen — rehydrate requeues the live job and projects it (no re-spawn here).
    store2 = await open_store(db)
    try:
        mgr2 = await _make_manager(store2)
        mgr2._engine = _NoopEngine()  # type: ignore[assignment]
        await mgr2.rehydrate()
        jobs = mgr2.list()
        assert len(jobs) == 1
        assert jobs[0].status == "queued"
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_resume_interrupted_respawns_requeued_jobs(tmp_path: Path) -> None:
    # download-jobs-failed-23: after rehydrate requeues interrupted jobs,
    # resume_interrupted re-spawns each ``queued`` row so the work actually resumes
    # ("jobs SHOULD survive restart"). Two live jobs are interrupted, then resumed.
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        mgr = await _make_manager(store)
        mgr._engine = _GatedEngine(asyncio.Event())  # never completes; stays live
        await mgr.submit(_record(), _req("h_a"))
        await mgr.submit(_record(), _req("h_b"))
    finally:
        await store.close()

    store2 = await open_store(db)
    try:
        mgr2 = await _make_manager(store2)
        engine = _CountingEngine()
        mgr2._engine = engine  # type: ignore[assignment]
        await mgr2.rehydrate()
        # Both requeued rows are projected ``queued`` but nothing runs until resume.
        assert {j.status for j in mgr2.list()} == {"queued"}
        assert engine.peak_concurrent == 0

        resumed = mgr2.resume_interrupted()
        assert resumed == 2
        await asyncio.sleep(0.05)
        assert engine.peak_concurrent >= 1  # the resumed jobs are now running

        engine.release_all()
        await mgr2.drain()
        assert engine.total_completed == 2
        assert {j.status for j in mgr2.list()} == {"completed"}
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_resume_interrupted_ignores_terminal_rows(tmp_path: Path) -> None:
    # resume_interrupted must spawn ONLY queued rows — terminal history is left alone.
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        engine = _CountingEngine()
        mgr._engine = engine  # type: ignore[assignment]
        mgr._projection = {
            "j_done": _tel_job("j_done", JobStatus.COMPLETED),
            "j_fail": _tel_job("j_fail", JobStatus.FAILED),
        }

        assert mgr.resume_interrupted() == 0
        await asyncio.sleep(0.05)
        assert engine.peak_concurrent == 0
    finally:
        await store.close()


# ─────────────── 6. rehydrate trims terminal history (IN-05) ─────────────────


@pytest.mark.asyncio
async def test_rehydrate_prunes_terminal_history_to_max_history_jobs(
    tmp_path: Path,
) -> None:
    """``max_history_jobs`` caps persisted terminal rows at rehydrate (IN-05).

    Five terminal jobs persisted; ``max_history_jobs=2`` keeps the two most
    recently updated; live jobs are never pruned."""
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        mgr = await _make_manager(store)
        mgr._engine = _NoopEngine()  # type: ignore[assignment]
        for i in range(5):
            await mgr.submit(_record(), _req(f"h{i}"))
        await mgr.drain()  # all five terminate (in memory)
        # _NoopEngine completes in memory only; persist the terminal status so the
        # store rows are genuinely TERMINAL for the reopen. Otherwise requeue-on-
        # restart (download-jobs-failed-23) keeps the still-live rows and nothing
        # prunes — this seeds the real terminal-history the test means to cap.
        for job in mgr._projection.values():
            await store.update(job)
        assert len(mgr.list()) == 5
    finally:
        await store.close()

    store2 = await open_store(db)
    try:
        mgr2 = await _make_manager(store2, max_history_jobs=2)
        await mgr2.rehydrate()
        # Only the 2 most-recently-updated terminal rows survive.
        assert len(mgr2.list()) == 2
    finally:
        await store2.close()


# ─────────────── 7. telemetry_items: order + cap (260605-wab) ────────────────


def _tel_job(
    job_id: str,
    status: JobStatus,
    *,
    manga_title: str | None = "Solo Leveling",
    chapter_number: float | None = 1.0,
) -> Job:
    """A minimal synthetic Job for telemetry_items() ordering/cap assertions."""
    return Job(
        job_id=job_id,
        release_handle="h_" + job_id,
        source_key="mangadex",
        title="t",
        status=status,
        manga_id=None,
        output_format="cbz",
        chapter_id=None,
        language="en",
        total_pages=None,
        downloaded_pages=None,
        total_bytes=0,
        remaining_bytes=0,
        output_path=None,
        message=None,
        created_at="2026-06-05T00:00:00+00:00",
        updated_at="2026-06-05T00:00:00+00:00",
        completed_at=None,
        manga_title=manga_title,
        chapter_number=chapter_number,
    )


@pytest.mark.asyncio
async def test_telemetry_items_orders_most_progressed_first_and_caps(
    tmp_path: Path,
) -> None:
    """``telemetry_items`` orders most-progressed-first (queued dropped first) and
    caps at exactly 50, reading the in-memory projection only (260605-wab)."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(store)
        # Insertion-ordered projection: one of each terminal/in-flight stage, then a
        # long queued backlog (60 queued) so the cap must drop queued first.
        proj: dict[str, Job] = {}
        proj["j_done"] = _tel_job("j_done", JobStatus.COMPLETED)
        proj["j_fail"] = _tel_job("j_fail", JobStatus.FAILED)
        proj["j_arch"] = _tel_job("j_arch", JobStatus.ARCHIVING)
        proj["j_dl"] = _tel_job("j_dl", JobStatus.DOWNLOADING)
        proj["j_res"] = _tel_job("j_res", JobStatus.RESOLVING)
        for i in range(60):
            proj[f"j_q{i}"] = _tel_job(f"j_q{i}", JobStatus.QUEUED)
        mgr._projection = proj

        items = mgr.telemetry_items()

        # (1) exactly 50 items (capped from 65).
        assert len(items) == 50
        # (2) the most-progressed stages lead; queued entries are the ones dropped.
        assert [it["jobId"] for it in items[:5]] == [
            "j_done",
            "j_fail",
            "j_arch",
            "j_dl",
            "j_res",
        ]
        # 45 queued survive (5 non-queued + 45 queued = 50); the LATEST queued are
        # dropped (stable order keeps the earliest-inserted queued).
        queued_ids = [it["jobId"] for it in items if it["status"] == "queued"]
        assert len(queued_ids) == 45
        # (3) within the queued tier, projection insertion order is preserved.
        assert queued_ids == [f"j_q{i}" for i in range(45)]
        # (4) each item carries exactly the four camelCase keys with status as .value.
        for it in items:
            assert set(it) == {"jobId", "mangaTitle", "chapterNumber", "status"}
        done = items[0]
        assert done["status"] == "completed"
        assert done["mangaTitle"] == "Solo Leveling"
        assert done["chapterNumber"] == 1.0
        # No store/disk read happened — the projection was set directly.
    finally:
        await store.close()


# ─────────────────────────── stub engines ───────────────────────────


class _NoopEngine:
    async def run(self, job: Job) -> None:
        job.status = JobStatus.COMPLETED


class _GatedEngine:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def run(self, job: Job) -> None:
        await self._gate.wait()
        job.status = JobStatus.COMPLETED


class _CountingEngine:
    def __init__(self) -> None:
        self.currently_running = 0
        self.peak_concurrent = 0
        self.total_completed = 0
        self._release = asyncio.Event()

    def release_all(self) -> None:
        self._release.set()

    async def run(self, job: Job) -> None:
        self.currently_running += 1
        self.peak_concurrent = max(self.peak_concurrent, self.currently_running)
        await self._release.wait()
        self.currently_running -= 1
        self.total_completed += 1
        job.status = JobStatus.COMPLETED


class _SourceTrackingEngine:
    """Like ``_CountingEngine`` but records which sources actually started (#267)."""

    def __init__(self) -> None:
        self.started_sources: set[str] = set()
        self.currently_running = 0
        self.peak_concurrent = 0
        self.total_completed = 0
        self._release = asyncio.Event()

    def release_all(self) -> None:
        self._release.set()

    async def run(self, job: Job) -> None:
        self.started_sources.add(job.source_key)
        self.currently_running += 1
        self.peak_concurrent = max(self.peak_concurrent, self.currently_running)
        await self._release.wait()
        self.currently_running -= 1
        self.total_completed += 1
        job.status = JobStatus.COMPLETED


async def _boom(*args: object, **kwargs: object) -> object:
    raise AssertionError("list() must not touch the store (DL-05)")
