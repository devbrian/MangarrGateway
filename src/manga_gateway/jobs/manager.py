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
and returns ``(jobId, "queued")`` — short-circuiting to an existing job on the
idempotency-by-existence stat-check (DL-03). Single-job GET (``get``/``get_dto``),
DELETE (``remove``, with the per-job staging unlink), and restart resume
(``rehydrate``/``resume_interrupted``) also live here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from ..models.download import DownloadJob
from .engine import JobEngine
from .model import Job, JobStatus
from .package import staging_temp_glob

_log = logging.getLogger("manga_gateway.jobs.manager")

# 260605-wab telemetry queue-item ordering + cap.
#
# Lower rank sorts FIRST → most-progressed lifecycle stages lead and ``queued``
# items sit at the bottom, so they are dropped first when the projection exceeds
# the cap. ``failed`` is placed among the terminal/top tier (the user did not name
# it) so terminal RESULTS are retained over a long queued backlog.
_TELEMETRY_TIER: dict[JobStatus, int] = {
    JobStatus.COMPLETED: 0,
    JobStatus.FAILED: 1,
    JobStatus.ARCHIVING: 2,
    JobStatus.DOWNLOADING: 3,
    JobStatus.RESOLVING: 4,
    JobStatus.QUEUED: 5,
}
# Cap the per-item telemetry list so a huge queued backlog cannot bloat the ring
# payload; queued items (highest tier) are dropped first by the ordering above.
_TELEMETRY_QUEUE_CAP = 50

if TYPE_CHECKING:
    from ..config import Settings
    from ..framework.antibot import AntiBotSolver
    from ..framework.health import SourceHealth
    from ..framework.proxy_pool import ProxyPool
    from ..framework.ratelimit import RateLimiter
    from ..framework.registry import SourceRegistry
    from ..framework.session import SessionManager
    from ..framework.session_prep import SessionPrep
    from ..handles.store import HandleStore, ResolutionRecord
    from ..models.download import SubmitRequest
    from .store import JobStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _eta_seconds(job: Job, remaining_bytes: int) -> int | None:
    """ETA in seconds from the observed download rate, or ``None`` (WR-08, guarded).

    ``rate = downloaded_bytes / elapsed_seconds`` where ``elapsed`` is from the
    job's ``download_started_at`` marker to now. Returns ``None`` — never raises —
    when the marker is unset/unparseable, ``elapsed <= 0``, ``downloaded_bytes == 0``,
    or the rate is non-positive (no divide-by-zero on any path). ``remaining_bytes``
    is already clamped at 0 by the caller, so the ETA is never negative.
    """
    started = job.download_started_at
    if not started or job.downloaded_bytes <= 0:
        return None
    try:
        start_dt = datetime.fromisoformat(started)
    except ValueError:
        return None
    elapsed = (datetime.now(UTC) - start_dt).total_seconds()
    if elapsed <= 0:
        return None
    rate = job.downloaded_bytes / elapsed
    if rate <= 0:
        return None
    return round(remaining_bytes / rate)


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
        session_prep: SessionPrep | None = None,
        image_proxy_pool: ProxyPool | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._registry = registry  # D-30: read per-source max_concurrent_jobs overrides
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
            session_prep=session_prep,
            image_proxy_pool=image_proxy_pool,
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

        WR-03 TOCTOU note (documented as accepted): the (2) branch holds no lock
        across ``find_latest_by_handle`` → ``os.path.exists`` → return. A concurrent
        ``DELETE /downloads/{id}?deleteData=true`` for ``latest.job_id`` between the
        store read and the caller's follow-up ``GET /downloads/{id}`` will yield a
        404 on the GET (the row + file are gone). This is an inconsistent-response
        race under concurrent submit + delete-with-data, NOT corruption — no
        partial CBZ is written and the caller is free to re-submit. The fix
        (per-handle locking) is not warranted for v1 (low impact, low frequency).
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
            # #16: the series title lives on the resolved record (SubmitRequest has
            # none); persist it so the engine can bucket mangaId-less grabs per-series.
            manga_title=record.manga_title,
            # 260605-nqo: persist the resolved chapter number so DELETE/GET-by-id
            # telemetry carries it across a restart. record.chapter_number is a
            # Decimal — float() at the Job boundary (None stays None).
            chapter_number=(
                float(record.chapter_number)
                if record.chapter_number is not None
                else None
            ),
            # #83/IN-03: carry the record's declared page count so the engine can
            # forward it to fetch_manifest as an integrity hint (projection-only).
            page_count=record.page_count,
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
        _log.info(
            "job=%s source=%s submitted format=%s manga_id=%s",
            job_id,
            job.source_key,
            job.output_format,
            job.manga_id,
        )
        return job_id, JobStatus.QUEUED.value

    # ─────────────────────────── read model (DL-05) ───────────────────────────

    def telemetry_items(self) -> list[dict[str, object]]:
        """Per-item GET /downloads telemetry list (260605-wab) — most-progressed first.

        Built from the INTERNAL projection (``self._projection``), NOT the wire DTOs:
        ``DownloadJob`` has no ``manga_title``/``chapter_number``, but the operator
        wants to see the actual queue (jobId + manga title + chapter + status). Each
        entry is a flat JSON-native map ``{jobId, mangaTitle, chapterNumber, status}``
        with camelCase INNER keys (these ride the JSON payload the read endpoints echo).

        Ordering is by ``_TELEMETRY_TIER`` (completed/failed → archiving → downloading
        → resolving → queued). ``sorted`` is STABLE, so same-tier items keep their
        projection insertion order; the list is then capped at
        ``_TELEMETRY_QUEUE_CAP`` so a long queued backlog is dropped FIRST.

        Poll-friendly (DL-05): O(n log n) over the in-memory projection only — NO
        disk/SQLite read. The route uses this so the route layer stays thin and the
        ordering/cap is unit-testable in isolation.

        Defined BEFORE ``list`` so the ``list[...]`` return annotation resolves to the
        builtin, not this class's ``list`` method (mypy ``valid-type``).
        """
        ordered = sorted(
            self._projection.values(),
            key=lambda j: _TELEMETRY_TIER[j.status],
        )
        return [
            {
                "jobId": j.job_id,
                "mangaTitle": j.manga_title,
                "chapterNumber": j.chapter_number,
                "status": j.status.value,
            }
            for j in ordered[:_TELEMETRY_QUEUE_CAP]
        ]

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
        """Blocking: unlink ONLY this job's OWN output + its OWN staging temp.

        Only paths the GATEWAY produced for THIS job are touched: the stored
        ``output_path`` (set by the engine at completion) and any leftover staging
        temp this job itself created — matched by the per-job ``staging_temp_glob``
        (``j-{job_id}-*.{fmt}.tmp``), NEVER a blanket ``*.cbz.tmp`` sibling sweep.

        The old sibling glob deleted EVERY ``*.cbz.tmp``/``*.cbt.tmp`` in the
        directory, so removing one finished job (``DELETE ?deleteData=true``) wiped
        a CONCURRENT job's in-flight staging temp and that job's ``os.replace`` then
        raised ``FileNotFoundError(2)`` (bug output-path-group-slash-filenotfound,
        Bug B). Scoping the glob to ``job_id`` removes that cross-job race. No
        client-supplied path is ever unlinked (T-03-12). Offload via
        ``asyncio.to_thread`` (Pitfall 2). Missing files are ignored.
        """
        if job.output_path is None:
            return
        out = Path(job.output_path)
        with contextlib.suppress(OSError):
            # A ``folder``-format output_path is a DIRECTORY, not a file —
            # ``Path.unlink`` would raise IsADirectoryError (an OSError, silently
            # suppressed) and leak the directory. rmtree it; cbz/cbt are files.
            if out.is_dir():
                shutil.rmtree(out, ignore_errors=True)
            else:
                out.unlink(missing_ok=True)
        # Remove ONLY this job's own staging temp (matched by job_id), never a
        # sibling's in-flight temp. The startup orphan sweep (app.py _STAGING_GLOBS,
        # startup-only) still catches any temp orphaned by a crash.
        parent = out.parent
        if parent.is_dir():
            pattern = staging_temp_glob(job.job_id, output_format=job.output_format)
            for temp in parent.glob(pattern):
                with contextlib.suppress(OSError):
                    if temp.is_dir():  # folder-format staging dir (j-...-.folder.tmp)
                        shutil.rmtree(temp, ignore_errors=True)
                    else:
                        temp.unlink(missing_ok=True)

    # ─────────────────────────── lifecycle ───────────────────────────

    async def rehydrate(self) -> None:
        """Load store rows into the projection at startup (PLAT-03).

        Order matters (IN-05): (1) ``store.rehydrate`` REQUEUES any live row left
        by an unclean shutdown back to ``queued`` (clearing its transient progress)
        so the gateway can resume it — "jobs SHOULD survive restart"; (2) we then
        prune TERMINAL rows down to ``Settings.max_history_jobs`` so a long-running
        gateway does not grow the projection / ``GET /downloads`` payload without
        bound (requeued live rows are never pruned); (3) finally we read the
        surviving rows into the in-memory read model.

        This method does NOT re-spawn the requeued jobs — that is
        :meth:`resume_interrupted`, called by the lifespan AFTER the orphan-staging
        sweep so a resumed job's fresh ``*.tmp`` archive can never be swept away by
        the previous run's cleanup.
        """
        await self._store.rehydrate()
        pruned = await self._store.prune_terminal(self._settings.max_history_jobs)
        if pruned:
            _log.info(
                "pruned %s terminal job rows (max_history_jobs=%s)",
                pruned,
                self._settings.max_history_jobs,
            )
        rows = await self._store.all()
        self._projection = {row.job_id: row for row in rows}

    def resume_interrupted(self) -> int:
        """Re-spawn every ``queued`` projection row so interrupted jobs resume.

        Called by the lifespan once, AFTER :meth:`rehydrate` (which requeued the
        jobs a redeploy/crash interrupted) and AFTER the orphan-staging sweep. Each
        requeued job is scheduled exactly like a fresh ``submit`` (``_spawn`` →
        global + per-source semaphores → engine), so the global concurrency bound
        keeps a large resumed backlog from saturating the box — extras stay
        ``queued`` until a slot frees (D-30). Returns the count resumed.

        A job that fails again ends ``terminal`` (``failed``) and is therefore NOT
        requeued on the next restart, so a genuinely-broken release can never cause
        a resume loop (per-job isolation in :meth:`JobEngine.run`).
        """
        resumed = [
            job_id
            for job_id, job in self._projection.items()
            if job.status is JobStatus.QUEUED
        ]
        for job_id in resumed:
            self._spawn(job_id)
        return len(resumed)

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
        """Acquire the per-source + global semaphores, then drive the engine (D-30).

        The per-source semaphore is sized from ``max_concurrent_per_source`` (WR-02)
        — a distinct, intentionally tighter knob than the global
        ``max_concurrent_chapters``. Previously both were sized to the same value
        so the per-source layer never constrained anything; this is the meaningful
        ceiling that keeps one slow source from saturating every global slot. A source
        may RAISE its own ceiling via the ``Source.max_concurrent_jobs`` class attr
        (e.g. mangadot=3, measured safe); the override is clamped to the global bound.

        Acquire order is **per-source first, global second** (#267). Were it reversed,
        an over-cap source's overflow tasks would grab a global permit and then block
        on the per-source cap — squatting global slots while idle and starving other
        sources (head-of-line blocking). Acquiring the per-source permit first means a
        capped source's extra tasks wait on ``source_sem`` alone, holding no global
        slot, leaving the global pool free for healthy sources.
        """
        job = self._projection.get(job_id)
        if job is None:  # pragma: no cover - defensive; submit always projects first
            # #14 / IN-02: a queued job was DELETEd before its task acquired the
            # semaphore — no orphaned state, but log it so the queued→nothing
            # transition isn't silent.
            _log.debug("job=%s dropped before semaphore acquire (deleted)", job_id)
            return
        source_sem = self._source_sems.setdefault(
            job.source_key,
            asyncio.Semaphore(self._per_source_bound(job.source_key)),
        )
        async with source_sem, self._global_sem:
            await self._engine.run(job)

    def _per_source_bound(self, source_key: str) -> int:
        """Per-source concurrent-job bound (D-30).

        The source's ``max_concurrent_jobs`` override if set, else
        ``settings.max_concurrent_per_source`` — clamped to the global
        ``max_concurrent_chapters`` so the per-source layer never exceeds it (WR-02).
        """
        cls = self._registry.get(source_key)
        override = (
            getattr(cls, "max_concurrent_jobs", None) if cls is not None else None
        )
        default = self._settings.max_concurrent_per_source
        bound = override if override is not None else default
        return max(1, min(bound, self._settings.max_concurrent_chapters))

    @staticmethod
    def _estimate_bytes_and_eta(job: Job) -> tuple[int, int, int | None]:
        """Live byte/ETA projection from CURRENT in-memory progress (WR-08, DL-05).

        Returns ``(total_bytes, remaining_bytes, eta_seconds)`` computed fresh per
        poll — no store/disk read. For a DOWNLOADING **or ARCHIVING** job with usable
        progress the total is the per-page-average projection and the ETA derives
        from the observed download rate; every other state (queued/resolving/
        completed/failed, or not-yet-usable progress) falls back to the STORED
        ``total_bytes``/``remaining_bytes`` with ``eta_seconds=None``.

        ARCHIVING is included so the counter stays MONOTONIC: by then all pages are
        fetched (``downloaded_pages == total_pages``) but the engine has not yet
        pinned ``total_bytes`` (that happens on COMPLETED), so a DOWNLOADING-only
        guard would briefly drop the live estimate back to the stored ``0/0`` and
        then jump to the exact total — a visible glitch. With ARCHIVING included,
        ``est_total == downloaded_bytes`` and ``remaining == 0`` there, matching the
        exact pinned total that COMPLETED then projects (issue #10 / CodeRabbit).

        Invariants (LOCKED Option 2): ``remaining_bytes`` is never negative
        (``max(0, …)``) and EVERY division is guarded against zero — the per-page
        average needs ``downloaded_pages > 0`` and ``total_pages > 0``; the ETA
        additionally needs a parseable ``download_started_at``, ``elapsed > 0`` and
        a positive observed rate. Any guard failing yields the stored fallback.
        """
        downloaded_bytes = job.downloaded_bytes
        downloaded_pages = job.downloaded_pages
        total_pages = job.total_pages
        if (
            job.status in (JobStatus.DOWNLOADING, JobStatus.ARCHIVING)
            and downloaded_pages
            and downloaded_pages > 0
            and total_pages
            and total_pages > 0
            and downloaded_bytes > 0
        ):
            est_total = round(downloaded_bytes / downloaded_pages * total_pages)
            remaining = max(0, est_total - downloaded_bytes)
            eta = _eta_seconds(job, remaining)
            return est_total, remaining, eta
        # Fallback: queued jobs project 0/0, completed jobs the exact pinned total
        # + remaining 0, everything terminal/non-downloading gets etaSeconds:null.
        return job.total_bytes, job.remaining_bytes, None

    def _to_dto(self, job: Job) -> DownloadJob:
        """Project the internal :class:`Job` onto the camelCase wire DTO (DL-04).

        Only contract ``DownloadJob`` fields are emitted — the manifest / page URLs /
        baseUrl never appear (PKG-01/R6). The byte/ETA fields are a LIVE estimate
        computed fresh from current progress (WR-08) — no store/disk read (DL-05).
        """
        total_bytes, remaining_bytes, eta_seconds = self._estimate_bytes_and_eta(job)
        return DownloadJob(
            job_id=job.job_id,
            title=job.title,
            source_key=job.source_key,
            status=job.status.value,
            total_bytes=total_bytes,
            remaining_bytes=remaining_bytes,
            total_pages=job.total_pages,
            downloaded_pages=job.downloaded_pages,
            eta_seconds=eta_seconds,
            output_path=job.output_path,
            message=job.message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
        )
