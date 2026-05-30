"""Lifespan-owned ``JobManager`` singleton — the download-surface job orchestrator.

Built ONCE in the FastAPI lifespan and stowed on ``app.state.job_manager`` (mirroring
``SessionManager``/``HandleStore``), read by ``deps.get_job_manager``. It owns the
download surface's moving parts:

* ``_projection`` — the in-memory ``dict[str, Job]`` read model that serves
  ``GET /downloads`` cheaply, with NO disk/SQLite read per poll (DL-05).
* ``_global_sem`` — a global ``asyncio.Semaphore(max_concurrent_chapters)`` bounding
  concurrent running jobs; a per-source ``asyncio.Semaphore`` is layered on top (D-30).
  Extras stay ``queued`` until a slot frees.
* ``_tasks`` — a STRONG-ref set of running background tasks so a fire-and-forget job is
  never garbage-collected mid-fetch (Pitfall 1); each is discarded via
  ``add_done_callback``.
* ``_engine`` — the source-agnostic :class:`~manga_gateway.jobs.engine.JobEngine` that
  drives each job's state machine.

``submit`` mints a stable ``j_``-prefixed jobId (R5, CSPRNG via ``secrets`` — the
``handles/store.py`` discipline), write-through-INSERTs the ``queued`` job to SQLite
BEFORE adding it to the projection (RESEARCH Pattern 2), schedules the background coro,
and returns ``(jobId, "queued")``. The idempotency stat-check, single-job GET, DELETE,
and staging sweep are the next slice (Plan 04) — here ``submit`` just inserts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from ..models.download import DownloadJob
from .engine import JobEngine
from .model import Job, JobStatus

if TYPE_CHECKING:
    from ..config import Settings
    from ..framework.antibot import AntiBotSolver
    from ..framework.health import SourceHealth
    from ..framework.ratelimit import RateLimiter
    from ..framework.registry import SourceRegistry
    from ..framework.session import SessionManager
    from ..handles.store import HandleStore, ResolutionRecord
    from ..models.download import SubmitRequest
    from .store import JobStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class JobManager:
    """Owns the projection, semaphores, task set, and the background engine (R1)."""

    def __init__(
        self,
        *,
        store: JobStore,
        registry: SourceRegistry,
        session: SessionManager,
        ratelimiter: RateLimiter,
        handle_store: HandleStore,
        settings: Settings,
        solver: AntiBotSolver | None = None,
        source_health: dict[str, SourceHealth] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._projection: dict[str, Job] = {}  # DL-05 read model
        self._global_sem = asyncio.Semaphore(settings.max_concurrent_chapters)  # D-30
        self._source_sems: dict[str, asyncio.Semaphore] = {}  # D-30 per-source
        self._tasks: set[asyncio.Task[None]] = set()  # strong refs — Pitfall 1
        self._engine = JobEngine(
            store=store,
            registry=registry,
            session=session,
            ratelimiter=ratelimiter,
            handle_store=handle_store,
            settings=settings,
            solver=solver,
            source_health=source_health,
        )

    # ─────────────────────────── submit ───────────────────────────

    async def submit(
        self, record: ResolutionRecord, req: SubmitRequest
    ) -> tuple[str, str]:
        """Mint a job, write-through-insert, schedule it, return ``(jobId, status)``.

        Write-through ordering (Pattern 2): the SQLite INSERT happens BEFORE the job is
        added to the in-memory projection, so a crash between the two leaves SQLite as
        the durable truth (the projection is rebuilt on restart).

        Idempotency-by-existence (DL-03/D-27): BEFORE minting a new job we check, in
        order, (1) is there a LIVE job for this handle? — return it; (2) is the latest
        terminal job ``completed`` with an output file still on disk? — a SINGLE
        ``os.path.exists`` (DL-05, NOT a per-poll rescan) — return it. Only if neither
        holds do we mint a fresh job (the re-grab path: deleted job or file gone).
        """
        # (1) live job for this handle → idempotent same-id return (D-27).
        live = await self._store.find_live_by_handle(req.release_handle)
        if live is not None:
            return live.job_id, live.status.value
        # (2) completed job whose output still exists → idempotent same-id return.
        latest = await self._store.find_latest_by_handle(req.release_handle)
        if (
            latest is not None
            and latest.status is JobStatus.COMPLETED
            and latest.output_path is not None
            and await asyncio.to_thread(os.path.exists, latest.output_path)  # ONE stat
        ):
            return latest.job_id, latest.status.value

        job_id = "j_" + secrets.token_urlsafe(16)  # R5 stable CSPRNG id
        now = _now_iso()
        job = Job(
            job_id=job_id,
            release_handle=req.release_handle,
            source_key=req.source_key or record.source_key,
            title=req.title or record.title,
            status=JobStatus.QUEUED,
            manga_id=req.manga_id,
            output_format=req.output_format,
            chapter_id=record.chapter_id,
            language=record.language,
            total_pages=None,
            downloaded_pages=None,
            total_bytes=0,
            remaining_bytes=0,
            output_path=None,
            message=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        try:
            await self._store.insert(job)  # write-through FIRST (Pattern 2)
        except aiosqlite.IntegrityError:
            # Belt-and-suspenders for DL-03: a concurrent submit won the partial
            # unique-index race between our find_live check and this insert. Re-read
            # and return the now-existing live job instead of surfacing a 500.
            existing = await self._store.find_live_by_handle(req.release_handle)
            if existing is not None:
                return existing.job_id, existing.status.value
            raise
        self._projection[job_id] = job
        self._spawn(job_id)
        return job_id, JobStatus.QUEUED.value

    # ─────────────────────────── read model (DL-05) ───────────────────────────

    def list(self) -> list[DownloadJob]:
        """Project every in-memory job to its wire DTO — no store/disk read (DL-05)."""
        return [self._to_dto(job) for job in self._projection.values()]

    def get(self, job_id: str) -> Job | None:
        """Projection lookup for a single internal job (DL-06)."""
        return self._projection.get(job_id)

    def get_dto(self, job_id: str) -> DownloadJob | None:
        """Project a single job to its wire DTO, or ``None`` if unknown (DL-06).

        Reads the in-memory projection only — no store/disk read (DL-05). The route
        maps ``None`` to a genuine 404 (Pitfall 8).
        """
        job = self._projection.get(job_id)
        return self._to_dto(job) if job is not None else None

    # ─────────────────────────── delete (DL-07) ───────────────────────────

    async def remove(self, job_id: str, *, delete_data: bool) -> bool:
        """Delete a job's row + projection, optionally unlinking its OWN files (DL-07).

        Returns ``False`` for an unknown id (route maps to 404); otherwise pops the
        in-memory projection, deletes the SQLite row (frees the handle for re-grab,
        D-27), and — when ``delete_data`` — unlinks ONLY the job's gateway-computed
        output path + any staging temp (T-03-12: NEVER a client-supplied path). The
        output path is read from the durable job row (the gateway computed it at
        archive time), never from request input.
        """
        projected = self._projection.get(job_id)
        row = projected if projected is not None else await self._store.get(job_id)
        if row is None:
            return False  # unknown id → 404
        self._projection.pop(job_id, None)
        await self._store.delete(job_id)
        if delete_data:
            await asyncio.to_thread(self._unlink_job_files, row)
        return True

    @staticmethod
    def _unlink_job_files(job: Job) -> None:
        """Blocking: unlink the job's OWN output + sibling staging temp (T-03-12).

        Only paths the GATEWAY produced are touched: the stored ``output_path`` (set
        by the engine at completion) and any leftover ``*.cbz.tmp``/``*.cbt.tmp``
        staging temp in that same directory. No client-supplied path is ever unlinked.
        Offload via ``asyncio.to_thread`` (Pitfall 2). Missing files are ignored.
        """
        if job.output_path is None:
            return
        out = Path(job.output_path)
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)
        # Sweep any sibling staging temp left mid-archive for this job's directory.
        parent = out.parent
        if parent.is_dir():
            for temp in parent.glob("*.cbz.tmp"):
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)
            for temp in parent.glob("*.cbt.tmp"):
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)

    # ─────────────────────────── lifecycle ───────────────────────────

    async def rehydrate(self) -> None:
        """Load store rows into the projection at startup (PLAT-03).

        The store's own ``rehydrate`` has already flipped any in-flight (live) job left
        by an unclean shutdown to ``failed``; here we mirror the durable rows into the
        in-memory read model. (The full staging sweep + DELETE land in Plan 04.)
        """
        rows = await self._store.rehydrate()
        self._projection = {row.job_id: row for row in rows}

    async def drain(self) -> None:
        """Await all in-flight background tasks (lifespan shutdown + test sync)."""
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def cancel_all(self) -> None:
        """Cancel every in-flight task and await its teardown (CR-01).

        The shutdown drain is bounded by a timeout in the lifespan; if it is exceeded
        a job is wedged, so we cancel the stragglers and await their cancellation
        before the store/transport they depend on are released.
        """
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ─────────────────────────── internals ───────────────────────────

    def _spawn(self, job_id: str) -> None:
        """Schedule the guarded engine run, keeping a strong ref (Pitfall 1)."""
        task = asyncio.create_task(self._run_guarded(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_guarded(self, job_id: str) -> None:
        """Acquire the global + per-source semaphores, then drive the engine (D-30)."""
        job = self._projection.get(job_id)
        if job is None:  # pragma: no cover - defensive; submit always projects first
            return
        source_sem = self._source_sems.setdefault(
            job.source_key, asyncio.Semaphore(self._settings.max_concurrent_chapters)
        )
        async with self._global_sem, source_sem:
            await self._engine.run(job)

    def _to_dto(self, job: Job) -> DownloadJob:
        """Project the internal :class:`Job` onto the camelCase wire DTO (DL-04).

        Only contract ``DownloadJob`` fields are emitted — the manifest / page URLs /
        baseUrl never appear (PKG-01/R6).
        """
        return DownloadJob(
            job_id=job.job_id,
            title=job.title,
            source_key=job.source_key,
            status=job.status.value,
            total_bytes=job.total_bytes,
            remaining_bytes=job.remaining_bytes,
            total_pages=job.total_pages,
            downloaded_pages=job.downloaded_pages,
            eta_seconds=None,
            output_path=job.output_path,
            message=job.message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
        )
