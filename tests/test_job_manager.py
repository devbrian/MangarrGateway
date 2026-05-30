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
   store's in-flight→failed flip (PLAT-03).
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


# ─────────────────────── 5. rehydrate populates projection (PLAT-03) ─────────


@pytest.mark.asyncio
async def test_rehydrate_populates_projection_from_store(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store = await open_store(db)
    try:
        mgr = await _make_manager(store)
        mgr._engine = _GatedEngine(asyncio.Event())  # never completes; stays live
        await mgr.submit(_record(), _req("h_live"))
    finally:
        # Simulate an unclean shutdown: close without draining the in-flight task.
        await store.close()

    # Reopen — rehydrate flips the live job to failed and projects it.
    store2 = await open_store(db)
    try:
        mgr2 = await _make_manager(store2)
        await mgr2.rehydrate()
        jobs = mgr2.list()
        assert len(jobs) == 1
        assert jobs[0].status == "failed"
    finally:
        await store2.close()


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
        await mgr.drain()  # all five terminate
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


async def _boom(*args: object, **kwargs: object) -> object:
    raise AssertionError("list() must not touch the store (DL-05)")
