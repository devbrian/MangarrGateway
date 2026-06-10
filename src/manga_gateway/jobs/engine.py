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

Diagnosability (issue #70): when a job fails the INTERNAL log records both the
underlying exception/traceback (so an operator can tell a flaky image fetch from a
resolve/decrypt timeout from a genuinely-bad release) and the release identity
(title + chapter id + a truncated handle). The wire-visible ``job.message`` stays
generic — internals never leak over the API (SSRF/info-leak discipline).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ..framework.context import SourceContext
from ..framework.errors import SourceError
from ..metrics.collector import get_collector
from .model import Job, JobStatus
from .package import (
    compute_output_path,
    is_valid_image,
    write_cbt,
    write_cbz,
    write_folder,
)

_log = logging.getLogger("manga_gateway.jobs.engine")


def _emit_job(source_key: str, *, op: str, outcome: str, error: str | None) -> None:
    """No-op-safe, failure-isolated ``emit_job`` (OBS-01/05, T-08-15).

    The engine runs OUTSIDE a fan-out child, so ``current_source`` is unbound here;
    ``source_scope`` binds the job's source_key for the duration of the emit so the
    job event self-attributes. Strictly additive — a ``None`` collector is a no-op
    and a collector error never breaks a job transition.
    """
    collector = get_collector()
    if collector is None:
        return
    try:
        from ..metrics.context import source_scope

        with source_scope(source_key):
            collector.emit_job(op=op, outcome=outcome, error=error)
    except Exception:  # noqa: BLE001 — a metric failure must never break a job
        pass


def _emit_package(
    source_key: str,
    *,
    op: str,
    outcome: str,
    duration_ms: float,
    error: str | None,
) -> None:
    """No-op-safe, failure-isolated ``emit_package`` (timed at the to_thread site)."""
    collector = get_collector()
    if collector is None:
        return
    try:
        from ..metrics.context import source_scope

        with source_scope(source_key):
            collector.emit_package(
                op=op, outcome=outcome, duration_ms=duration_ms, error=error
            )
    except Exception:  # noqa: BLE001 — a metric failure must never break a job
        pass


if TYPE_CHECKING:
    from ..config import Settings
    from ..framework.antibot import AntiBotSolver
    from ..framework.health import SourceHealth
    from ..framework.ratelimit import RateLimiter
    from ..framework.registry import SourceRegistry
    from ..framework.session import SessionManager
    from ..framework.session_prep import SessionPrep
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
    """The original page filename (last path segment), with extension when present.

    The URL query/fragment is stripped FIRST (issue #204): a kagane page URL is
    ``.../{page_id}.{ext}?token={jwt}&is_datasaver=false`` whose JWT carries dots
    (``header.payload.signature``). Splitting the RAW url on ``/`` keeps the query,
    so the packager's ``Path(name).suffix`` extension derivation grabs the dotted
    JWT-signature tail (``.<sig>&is_datasaver=false``) as the "extension" — yielding
    garbage CBZ entry names like ``01.<sig>&is_datasaver=false``. Parsing the URL
    and taking only its path leaves a clean ``{page_id}.{ext}`` for every source.

    For a normal page URL like ``.../h/p1.png`` returns ``p1.png`` (extension
    preserved). For a degenerate manifest URL ending in ``/`` (no path segment)
    returns the constant ``"page"`` — extensionless; the engine writes entries by
    index so the fallback name is harmless (IN-03).
    """
    return urlsplit(url).path.rsplit("/", 1)[-1] or "page"


# Issue #70 (diagnosability): a failed/transitioning job must be tie-able to WHAT it
# was downloading. The handle is gateway-issued and may be secret-like (it is the
# token Mangarr re-submits to download), so it is NEVER logged in full — only a
# short prefix for correlation. Title + chapter_id is the human-readable identity.
_HANDLE_LOG_PREFIX = 8


def _leaf_exc_name(exc: BaseException) -> str:
    """The most meaningful exception TYPE name for the durable failure message.

    A non-``SourceError`` escaping the per-page ``asyncio.TaskGroup`` (the realistic
    generic-failure shape — see ``_fetch_pages_once``: ``except* SourceError`` lets
    any OTHER exception re-raise wrapped) arrives at ``run`` inside a
    ``BaseExceptionGroup``. Recursively unwrap to the first leaf so the persisted
    ``"job failed: <Type>"`` names the ACTUAL cause (e.g. ``RuntimeError``,
    ``TimeoutError``) rather than the opaque ``"ExceptionGroup"`` wrapper. Type
    names ONLY — never the exception message (which can carry a URL/handle); the
    full message + traceback stay in the internal ``_log.exception`` record.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return type(exc).__name__


def _release_identity(job: Job) -> str:
    """A compact, non-secret release identity for INTERNAL logs (issue #70).

    Renders ``title=… chapter=… handle=…`` from fields the ``Job`` already carries
    in memory. The release handle is TRUNCATED to a short prefix (never logged in
    full) because it is a gateway-issued token Mangarr re-submits — treating it
    like a secret keeps the leak surface minimal (CLAUDE.md SSRF/info-leak
    discipline). This string is for logs only and never crosses the wire.
    """
    title = job.title or "?"
    chapter = job.chapter_id or "?"
    handle = job.release_handle or ""
    handle_short = (
        f"{handle[:_HANDLE_LOG_PREFIX]}…"
        if len(handle) > _HANDLE_LOG_PREFIX
        else (handle or "?")
    )
    return f"title={title!r} chapter={chapter!r} handle={handle_short!r}"


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
        session_prep: SessionPrep | None = None,
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
        # Phase-7 session-prep seam (D-01): the shared CsrfBootstrap provider threaded
        # from the lifespan so a csrf-bootstrap download (MangaBall) carries the CSRF
        # token + session cookie on its /api/v1 POSTs. None for the pre-Plan-03 app
        # (every prepare() returns None → MangaDex/Comix unchanged).
        self._session_prep = session_prep

    async def run(self, job: Job) -> None:
        """Drive ``job`` to ``completed`` or ``failed`` (per-job isolation, D-29).

        Any exception ends the job ``failed`` with a generic reason; the strict
        all-or-nothing fetch loop never publishes a partial CBZ. A non-``SourceError``
        escape (issue #70 — e.g. an httpx transport error/5xx that exhausted tenacity
        and was never mapped to a ``SourceError``) is recorded WITH its full traceback
        to the internal log; the wire message stays generic so internals are never
        leaked over the API.
        """
        try:
            await self._run_inner(job)
        except SourceError as exc:
            await self._fail(job, str(exc) or exc.code)
        except Exception as exc:  # noqa: BLE001 — isolation: a job failure must not poison others
            # #70: capture the cause + traceback to the INTERNAL log only. Without
            # this the cause is swallowed entirely and a failed job has neither a
            # cause nor (without _fail's identity) an identity in any artifact.
            _log.exception(
                "job=%s source=%s %s unexpected error — failing job",
                job.job_id,
                job.source_key,
                _release_identity(job),
            )
            # download-jobs-failed-23 (issue #70 follow-up): the traceback above is
            # INTERNAL and EPHEMERAL — a container redeploy wipes the logs, leaving a
            # genuinely-failed job's durable row carrying only the opaque "job failed"
            # with no recoverable cause. Persist the exception TYPE on the wire-visible
            # message so the failure CLASS survives a restart. The type name is a code
            # identifier (never a URL/handle/secret/page byte), so this respects the
            # SSRF/info-leak discipline — the full str(exc) + traceback stay in the
            # internal log above and never cross the wire. _leaf_exc_name unwraps the
            # per-page TaskGroup's ExceptionGroup to name the ACTUAL cause.
            await self._fail(job, f"job failed: {_leaf_exc_name(exc)}")

    # ─────────────────────────── state machine ───────────────────────────

    async def _run_inner(self, job: Job) -> None:
        cls = self._registry.get(job.source_key)
        if cls is None:
            await self._fail(job, f"unknown source {job.source_key}")
            return
        source = cls()
        # #83/IN-03: forward the record's declared page count (carried on the job) so
        # ``fetch_manifest`` can integrity-check the extracted manifest length.
        ctx = self._build_context(source, expected_pages=job.page_count)

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
        # WR-08: zero the byte accumulator + stamp a download-start marker so the
        # _to_dto live byte/ETA estimate has a clean baseline (projection-only —
        # no SQLite column, mirroring downloaded_pages above).
        job.downloaded_bytes = 0
        job.download_started_at = _now_iso()
        await self._transition(job, JobStatus.DOWNLOADING)
        pages = await self._fetch_all_pages(job, source, ctx, manifest)

        # archiving → package to the requested format, atomic publish (D-26).
        await self._transition(job, JobStatus.ARCHIVING)
        final_path = compute_output_path(
            self._settings.output_root,
            job.manga_id,
            job.title,
            output_format=job.output_format,
            # Issue #9: when Mangarr submits without a mangaId, fall back to the
            # source-stable chapter id so two different series with the same
            # title can't collide inside the shared manga-unknown/ bucket.
            fallback_discriminator=job.chapter_id,
            # Issue #16: when Mangarr submits without a mangaId, bucket the output
            # under the resolved series title (manga-{title}/) instead of the flat
            # manga-unknown/; falls back to manga-unknown/ when the title is unusable.
            manga_title=job.manga_title,
        )
        writer = _WRITERS.get(job.output_format, write_cbz)
        # Time the package step at the ASYNC call-site (RESEARCH DRIFT — the sync
        # write_* worker runs in a thread where the contextvars aren't readable, so
        # emit here, never inside package.py). Strictly additive + failure-isolated.
        _pkg_start = time.perf_counter()
        try:
            await asyncio.to_thread(writer, pages, final_path)
        except Exception as exc:
            _emit_package(
                job.source_key,
                op=job.output_format,
                outcome="error",
                duration_ms=(time.perf_counter() - _pkg_start) * 1000.0,
                error=repr(exc),
            )
            raise
        _emit_package(
            job.source_key,
            op=job.output_format,
            outcome="ok",
            duration_ms=(time.perf_counter() - _pkg_start) * 1000.0,
            error=None,
        )

        # completed → expose the host-reachable output path (JOB-03/D-26).
        job.output_path = str(final_path)
        job.completed_at = _now_iso()
        # WR-08: pin the EXACT real total = sum of fetched page bytes, and zero
        # the remainder. These ARE persisted columns (the _transition write-through
        # stores them), so completed history carries the exact archive size and the
        # _to_dto fallback projects them verbatim (with etaSeconds:null).
        job.total_bytes = job.downloaded_bytes
        job.remaining_bytes = 0
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
                # WR-08: reset the byte accumulator too so the re-resolved pass
                # does not double-count bytes from the partially-fetched stale pass
                # (same reasoning that resets downloaded_pages above).
                job.downloaded_bytes = 0
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
                # #70 (fix 3): wrap the page fetch so a genuine transport failure
                # (httpx timeout/connect error, or a 5xx that exhausted tenacity's
                # reraise=True retries) surfaces as a STRUCTURED, page-scoped
                # ``SourceError("source_unavailable", "page N …")`` instead of a
                # bare httpx exception that escapes the TaskGroup's ``except*
                # SourceError`` and degrades to the generic "job failed". This is
                # the realistic ~98s-then-generic-fail escape path from issue #70:
                # ``ctx.get_bytes_plain`` retries transport errors/5xx and then
                # re-raises the raw httpx error. The cause is still captured by the
                # ``except Exception`` traceback log; this just gives the failure a
                # specific, diagnosable shape.
                try:
                    content = await source.fetch_image(url, ctx)  # type: ignore[attr-defined]
                except SourceError:
                    raise
                except httpx.HTTPError as exc:
                    raise SourceError(
                        "source_unavailable",
                        f"page {index + 1} fetch failed: {type(exc).__name__}: {exc}",
                    ) from exc
            ok = await asyncio.to_thread(is_valid_image, content)
            if not ok:
                raise SourceError("source_unavailable", f"page {index + 1} invalid")
            results[index] = (_page_filename(url), content)
            # Progress lives in the projection only — no per-page SQLite write
            # (RESEARCH Pattern 2 / DL-05).
            job.downloaded_pages = (job.downloaded_pages or 0) + 1
            # WR-08: accumulate the REAL fetched byte count alongside the page
            # count (same projection-only treatment — no per-page SQLite write).
            job.downloaded_bytes = (job.downloaded_bytes or 0) + len(content)

        try:
            async with asyncio.TaskGroup() as tg:
                for i, url in enumerate(manifest):
                    tg.create_task(_one(i, url))
        except* SourceError as eg:
            # A 403 from the image host means a stale baseUrl → re-resolve (Pitfall 4);
            # any other SourceError is a genuine unrecoverable page loss (D-29 strict).
            # Branch on the structured ``status`` attribute (WR-07) — not substring
            # presence of "403" in the rendered message, which misclassifies any
            # error whose message happens to contain those digits.
            if any(
                isinstance(e, SourceError) and e.status == 403 for e in eg.exceptions
            ):
                raise _StaleManifest from None
            raise eg.exceptions[0] from None

        return [p for p in results if p is not None]

    # ─────────────────────────── helpers ───────────────────────────

    def _build_context(
        self, source: object, *, expected_pages: int | None = None
    ) -> SourceContext:
        """Build a SourceContext exactly like the search route (Pattern 4).

        Threads the same anti-bot seams as ``search.py:_run_one`` so a cloudflare*
        download injects clearance (D-40), reconciles a challenge 403 (D-35), and
        decrypts page bytes (D-39); ``source_health.get(key)`` feeds the breaker (D-36).
        ``expected_pages`` (the resolved record's declared page count) is forwarded to
        ``fetch_manifest`` as a manifest-integrity hint (#83/IN-03) — ``None`` on the
        search route, which never resolves a manifest.
        """
        key: str = source.key  # type: ignore[attr-defined]
        # Copy ``decrypt_config`` per request so a scheme that mutates its
        # config cannot leak state across jobs via the source CLASS attribute.
        src_decrypt_config = getattr(source, "decrypt_config", None)
        return SourceContext(
            source_key=key,
            rate_limit_per_minute=source.rate_limit_per_minute,  # type: ignore[attr-defined]
            session=self._session,
            ratelimiter=self._ratelimiter,
            handle_store=self._handle_store,
            solver=self._solver,
            antibot=getattr(source, "antibot", "none"),
            decrypt_scheme=getattr(source, "decrypt_scheme", None),
            decrypt_config=dict(src_decrypt_config) if src_decrypt_config else None,
            source_health=self._source_health.get(key),
            session_prep=self._session_prep,
            expected_pages=expected_pages,
        )

    async def _transition(self, job: Job, status: JobStatus) -> None:
        """Persist a status transition: SQLite write-through FIRST (Pattern 2)."""
        previous = job.status
        job.status = status
        await self._store.update(job)
        # #21: one log per state transition is the operator's primary signal
        # that the gateway is actually working during a long download.
        # #70: carry the release identity so a transition can be tied to WHAT it
        # was downloading (the Job already holds title/chapter_id/release_handle).
        _log.info(
            "job=%s source=%s %s %s→%s pages=%s",
            job.job_id,
            job.source_key,
            _release_identity(job),
            previous.value,
            status.value,
            job.total_pages if job.total_pages is not None else "?",
        )
        # Additive job-state metric (OBS-05): every RESOLVING→…→COMPLETED change
        # flows through here. op = the new state; outcome ok (the failure twin is
        # in _fail). Strictly additive — no effect on the write-through above.
        _emit_job(job.source_key, op=status.value, outcome="ok", error=None)

    async def _fail(self, job: Job, message: str) -> None:
        previous = job.status
        job.status = JobStatus.FAILED
        job.message = message
        await self._store.update(job)
        # #70: a failed job MUST carry its release identity in the log so the
        # failure can be tied to WHAT it was downloading; the wire message stays
        # generic (set on job.message) but the internal log is diagnosable.
        _log.warning(
            "job=%s source=%s %s %s→failed reason=%r",
            job.job_id,
            job.source_key,
            _release_identity(job),
            previous.value,
            message,
        )
        # Additive job-failure metric (OBS-05): the failure twin of _transition.
        _emit_job(job.source_key, op="failed", outcome="error", error=message)


class _StaleManifest(Exception):
    """Internal signal: an image-host 403 means the baseUrl expired (Pitfall 4)."""
