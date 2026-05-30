"""Background job state-machine engine (JOB-01/02/03/04, D-26/D-29, R6/PKG-01).

The :class:`JobEngine` drives a single :class:`~manga_gateway.jobs.model.Job` through
``resolving → downloading → archiving → completed`` on the all-pages-succeed path, or
ends it ``failed`` on ANY unrecoverable page loss (D-29 strict — no partial CBZ). It is
the first post-response background coroutine in the repo; one job's failure never
poisons another (the ``framework/fanout.py`` per-unit-isolation analog — here a per-job
failure is caught and recorded as ``status=failed``).

SOURCE-AGNOSTIC (SRC-01): the engine resolves the source class from the registry by
``source_key`` and calls its ``fetch_manifest`` / ``fetch_image`` hooks the same way
``api/routes/search.py`` calls ``search`` — it NEVER names a concrete source. The page
manifest (page URLs / any fresh at-home token) is resolved INTERNALLY and never leaves
this module (PKG-01/R6); only ``jobId`` + progress cross the wire.

Write-through ordering (RESEARCH Pattern 2): every status transition is persisted to
SQLite via ``store.update`` BEFORE the in-memory projection is observed — the projection
holds the SAME mutable ``Job`` object, so each transition mutates the fields then writes
SQLite, leaving the durable store as the source of truth if the process crashes.

Blocking work (Pillow ``verify``, ``zipfile`` write, ``os.replace``) is offloaded via
``asyncio.to_thread`` so it never stalls the event loop or ``GET /downloads`` polling
(Pitfall 2). Image bytes are fetched under a per-job ``asyncio.Semaphore`` (D-31).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..framework.context import SourceContext
from ..framework.errors import SourceError
from .model import Job, JobStatus
from .package import (
    compute_output_path,
    is_valid_image,
    write_cbt,
    write_cbz,
    write_folder,
)

if TYPE_CHECKING:
    from ..config import Settings
    from ..framework.antibot import AntiBotSolver
    from ..framework.health import SourceHealth
    from ..framework.ratelimit import RateLimiter
    from ..framework.registry import SourceRegistry
    from ..framework.session import SessionManager
    from ..handles.store import HandleStore
    from .store import JobStore

# Explicit stale-baseUrl re-resolve budget (Pitfall 4). tenacity STOPS on a 403, so a
# fresh-baseUrl recovery is engine logic, not a retry. After this many re-resolves the
# job fails.
_MAX_MANIFEST_RERESOLVES = 2

# Writer per advertised output format (D-26 atomic publish lives in each).
_WRITERS = {"cbz": write_cbz, "cbt": write_cbt, "folder": write_folder}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _page_filename(url: str) -> str:
    """The original page filename (last path segment) — preserves the extension."""
    return url.rsplit("/", 1)[-1] or "page"


class JobEngine:
    """Drives one job's fetch/package lifecycle (constructed once by the JobManager)."""

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
        self._registry = registry
        self._session = session
        self._ratelimiter = ratelimiter
        self._handle_store = handle_store
        self._settings = settings
        # Phase-4 anti-bot seams (defaulted so app.py without the Plan-04 wiring
        # still constructs; a cloudflare* download injects clearance + decrypts).
        self._solver = solver
        self._source_health = source_health or {}

    async def run(self, job: Job) -> None:
        """Drive ``job`` to ``completed`` or ``failed`` (per-job isolation, D-29).

        Any exception ends the job ``failed`` with a generic reason; the strict
        all-or-nothing fetch loop never publishes a partial CBZ.
        """
        try:
            await self._run_inner(job)
        except SourceError as exc:
            await self._fail(job, str(exc) or exc.code)
        except Exception:  # noqa: BLE001 — isolation: a job failure must not poison others
            await self._fail(job, "job failed")

    # ─────────────────────────── state machine ───────────────────────────

    async def _run_inner(self, job: Job) -> None:
        cls = self._registry.get(job.source_key)
        if cls is None:
            await self._fail(job, f"unknown source {job.source_key}")
            return
        source = cls()
        ctx = self._build_context(source)

        # resolving → fetch the page manifest INTERNALLY (PKG-01/R6).
        await self._transition(job, JobStatus.RESOLVING)
        if job.chapter_id is None:
            await self._fail(job, "missing chapter id")
            return
        manifest = await source.fetch_manifest(job.chapter_id, ctx)
        if not manifest:
            await self._fail(job, "manifest resolved to zero pages")
            return

        # downloading → strict all-or-nothing page fetch (D-29), with stale-baseUrl
        # re-resolve recovery (Pitfall 4).
        job.total_pages = len(manifest)
        job.downloaded_pages = 0
        await self._transition(job, JobStatus.DOWNLOADING)
        pages = await self._fetch_all_pages(job, source, ctx, manifest)

        # archiving → package to the requested format, atomic publish (D-26).
        await self._transition(job, JobStatus.ARCHIVING)
        final_path = compute_output_path(
            self._settings.output_root,
            job.manga_id,
            job.title,
            output_format=job.output_format,
        )
        writer = _WRITERS.get(job.output_format, write_cbz)
        await asyncio.to_thread(writer, pages, final_path)

        # completed → expose the host-reachable output path (JOB-03/D-26).
        job.output_path = str(final_path)
        job.completed_at = _now_iso()
        await self._transition(job, JobStatus.COMPLETED)

    async def _fetch_all_pages(
        self,
        job: Job,
        source: object,
        ctx: SourceContext,
        manifest: list[str],
    ) -> list[tuple[str, bytes]]:
        """Fetch every page concurrently (bounded), strict-fail on first loss (D-29).

        On a mid-fetch image-host 403 (a ``SourceError`` — tenacity STOPS on 403), the
        whole manifest is re-resolved ONCE to get a fresh baseUrl and the remaining
        pages are retried; exceeding ``_MAX_MANIFEST_RERESOLVES`` ends the job
        ``failed``. Any other unrecoverable page failure ends the job immediately.
        """
        reresolves = 0
        while True:
            try:
                return await self._fetch_pages_once(job, source, ctx, manifest)
            except _StaleManifest:
                if reresolves >= _MAX_MANIFEST_RERESOLVES:
                    raise SourceError(
                        "source_unavailable",
                        "stale manifest re-resolve budget exceeded",
                    ) from None
                reresolves += 1
                job.downloaded_pages = 0
                # source duck-types the Source fetch hooks (resolved via registry).
                manifest = await source.fetch_manifest(job.chapter_id, ctx)  # type: ignore[attr-defined]
                if not manifest:
                    raise SourceError(
                        "source_unavailable", "manifest re-resolved to zero pages"
                    ) from None

    async def _fetch_pages_once(
        self,
        job: Job,
        source: object,
        ctx: SourceContext,
        manifest: list[str],
    ) -> list[tuple[str, bytes]]:
        sem = asyncio.Semaphore(self._settings.image_fetch_concurrency)
        results: list[tuple[str, bytes] | None] = [None] * len(manifest)

        async def _one(index: int, url: str) -> None:
            async with sem:
                content = await source.fetch_image(url, ctx)  # type: ignore[attr-defined]
            ok = await asyncio.to_thread(is_valid_image, content)
            if not ok:
                raise SourceError("source_unavailable", f"page {index + 1} invalid")
            results[index] = (_page_filename(url), content)
            # Progress lives in the projection only — no per-page SQLite write
            # (RESEARCH Pattern 2 / DL-05).
            job.downloaded_pages = (job.downloaded_pages or 0) + 1

        try:
            async with asyncio.TaskGroup() as tg:
                for i, url in enumerate(manifest):
                    tg.create_task(_one(i, url))
        except* SourceError as eg:
            # A 403 from the image host means a stale baseUrl → re-resolve (Pitfall 4);
            # any other SourceError is a genuine unrecoverable page loss (D-29 strict).
            if any("403" in str(e) for e in eg.exceptions):
                raise _StaleManifest from None
            raise eg.exceptions[0] from None

        return [p for p in results if p is not None]

    # ─────────────────────────── helpers ───────────────────────────

    def _build_context(self, source: object) -> SourceContext:
        """Build a SourceContext exactly like the search route (Pattern 4).

        Threads the same anti-bot seams as ``search.py:_run_one`` so a cloudflare*
        download injects clearance (D-40), reconciles a challenge 403 (D-35), and
        decrypts page bytes (D-39); ``source_health.get(key)`` feeds the breaker (D-36).
        """
        key: str = source.key  # type: ignore[attr-defined]
        # D-45: thread the warm-page solver into decrypt_config so the comix-v1
        # browser-evaluated decrypt scheme can reach it (MangaDex / scheme=None
        # ignores it). Source-declared keys take precedence over the framework-
        # injected ``solver`` field (sources do not name their own ``solver`` key).
        decrypt_config = dict(getattr(source, "decrypt_config", None) or {})
        decrypt_config["solver"] = self._solver
        return SourceContext(
            source_key=key,
            rate_limit_per_minute=source.rate_limit_per_minute,  # type: ignore[attr-defined]
            session=self._session,
            ratelimiter=self._ratelimiter,
            handle_store=self._handle_store,
            solver=self._solver,
            antibot=getattr(source, "antibot", "none"),
            decrypt_scheme=getattr(source, "decrypt_scheme", None),
            decrypt_config=decrypt_config,
            source_health=self._source_health.get(key),
        )

    async def _transition(self, job: Job, status: JobStatus) -> None:
        """Persist a status transition: SQLite write-through FIRST (Pattern 2)."""
        job.status = status
        await self._store.update(job)

    async def _fail(self, job: Job, message: str) -> None:
        job.status = JobStatus.FAILED
        job.message = message
        await self._store.update(job)


class _StaleManifest(Exception):
    """Internal signal: an image-host 403 means the baseUrl expired (Pitfall 4)."""
