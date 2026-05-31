"""FastAPI application factory + lifespan.

The single-process R1 seams are built ONCE in the lifespan and stowed on
``app.state`` (PLAT-02): an injectable ``Transport`` (SRC-04), the shared
``SessionManager``, a ``NoopSolver`` (BOT-01 default), a ``RateLimiter`` seam, an
empty ``SourceRegistry`` (SRC-01), and the 12h caps ``TTLCache`` (PLAT-04). Global
API-key auth (AUTH-01/D-02) is applied on the app so every route is covered, and
the contract JSON error model (ERR-01) is registered.

The full ``load_settings`` default wiring lands in Task 3.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from cachetools import TTLCache
from fastapi import Depends, FastAPI

from .api import api_router
from .config import Settings
from .errors import register_error_handlers
from .framework.antibot import CloudflareSolver
from .framework.health import SourceHealth
from .framework.ratelimit import RateLimiter
from .framework.registry import SourceRegistry
from .framework.session import SessionManager
from .framework.transport import HttpxTransport
from .handles.store import HandleStore
from .jobs.manager import JobManager
from .jobs.store import open_store
from .security import require_api_key
from .sources import register_builtin_sources

# 12h caps TTL (PLAT-04).
_CAPS_TTL_SECONDS = 43_200

# Bound shutdown drain so a wedged in-flight job cannot hang teardown forever
# (CR-01). On timeout the stragglers are cancelled and shutdown proceeds.
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 30.0

# How often each D-37 recovery supervisor checks whether its cloudflare-gated
# source's breaker has tripped (cheap; the escalating +1h/+6h re-probe runs
# only once a trip is seen).
_RECOVERY_POLL_SECONDS = 60.0

_log = logging.getLogger("manga_gateway")

# Gateway-owned staging temp suffixes left by an interrupted archive (package.py).
# These — and ONLY these — are swept on startup; completed output files (.cbz/.cbt/
# folder dirs) are never touched (PLAT-03 / T-03-13).
_STAGING_GLOBS = ("*.cbz.tmp", "*.cbt.tmp", "*.folder.tmp")


def _sweep_staging(output_root: str) -> int:
    """Blocking: unlink orphan ``*.tmp`` staging artifacts under ``output_root``.

    A crash mid-archive can leave a partial ``*.cbz.tmp`` (or ``.cbt.tmp``/
    ``.folder.tmp``) staging temp that ``write_cbz``'s success path would otherwise
    have ``os.replace``d away (Runtime State Inventory). Walk the gateway-owned output
    root and remove only those staging temps; a completed output (``.cbz``/``.cbt``/a
    folder) is NEVER deleted (T-03-13). Offload via ``asyncio.to_thread`` (Pitfall 2).
    Returns the number of artifacts swept. A missing output root is a no-op.
    """
    root = Path(output_root)
    if not root.is_dir():
        return 0
    swept = 0
    for pattern in _STAGING_GLOBS:
        for temp in root.rglob(pattern):
            with suppress(OSError):
                if temp.is_dir():  # *.folder.tmp staging dir
                    for child in temp.glob("*"):
                        with suppress(OSError):
                            child.unlink()
                    temp.rmdir()
                else:
                    temp.unlink()
                swept += 1
    return swept


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _recovery_watchdog(
    health: SourceHealth,
    *,
    backoff_hours: tuple[int, int],
    sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    probe: Callable[[], Awaitable[bool]],
) -> None:
    """D-37 escalating-backoff recovery for a tripped per-source breaker.

    On a tripped breaker, re-probe the source on an ESCALATING schedule (+1h, then
    +6h by default) — NOT on the request path. A probe that succeeds resets the
    breaker (``record_success`` → re-enabled); after the last backoff step the
    source stays down until a manual restart (no busy retry, D-37).

    ``sleep``/``probe`` are injectable so tests assert the schedule with a fake clock
    and no real waiting. The probe returns ``True`` on recovery, ``False`` otherwise.
    """
    for hours in backoff_hours:
        if health.is_enabled:
            return  # already recovered elsewhere — nothing to do
        await sleep(hours * 3600.0)
        recovered = await probe()
        if recovered:
            health.record_success()  # breaker resets → source re-enabled
            _log.info("Recovery watchdog re-enabled a source after a re-probe")
            return
    # Exhausted the backoff schedule — stay down until manual restart (D-37).
    _log.warning("Recovery watchdog exhausted backoff; source stays down until restart")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the R1 singleton seams once; tear the transport down on shutdown."""
    settings: Settings = app.state.settings
    transport = HttpxTransport(settings)  # SRC-04 injectable seam
    app.state.transport = transport
    app.state.session = SessionManager(transport)  # R1 shared session
    # SRC-01: build the registry first so the rest of the lifespan can inspect
    # per-source metadata (antibot level, decrypt scheme) WITHOUT hardcoding any
    # source key by name. Adding the 50+ planned sources is a register call and
    # zero edits to this shell (CLAUDE.md "framework knows no source by name").
    registry = SourceRegistry()
    register_builtin_sources(registry)
    app.state.registry = registry
    # Derive the set of cloudflare-gated source classes from the registry rather
    # than hardcoding it — anything whose ``antibot`` declares cloudflare needs
    # both a SourceHealth breaker (D-38) and a slot in the solver's key set.
    cf_sources: dict[str, type] = {
        key: cls
        for key, cls in registry.items()
        if getattr(cls, "antibot", "none").startswith("cloudflare")
    }
    cloudflare_keys: frozenset[str] = frozenset(cf_sources)
    # Per-source health map (D-38): one breaker per cloudflare-gated source.
    # JobManager + the search route read this by reference (deps.get_source_health).
    source_health: dict[str, SourceHealth] = {
        key: SourceHealth(threshold=settings.cloudflare_breaker_threshold)
        for key in cloudflare_keys
    }
    app.state.source_health = source_health
    # Resolve the Cloudflare clearance URL from the first cloudflare-gated source's
    # ``cloudflare_challenge_url`` metadata — the framework solver itself never
    # names a host. If multiple cloudflare sources are registered, the first
    # with a non-None URL wins; sources without a URL fall back to the framework
    # solver default (an invalid.example placeholder useful only for tests).
    challenge_url: str | None = next(
        (
            getattr(cls, "cloudflare_challenge_url", None)
            for cls in cf_sources.values()
            if getattr(cls, "cloudflare_challenge_url", None)
        ),
        None,
    )
    solver_kwargs: dict[str, Any] = {
        "user_data_dir": settings.cloudflare_user_data_dir,
        "headless": settings.cloudflare_headless,
        "solve_concurrency": settings.cloudflare_solve_concurrency,
        # PR #58 follow-up: per-source-shape concurrency cap for the warm
        # browser. Defaults to 5 to match the Comix /search candidate
        # ceiling; raise via GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY when
        # adding a source whose fan-out exceeds that (Pitfall 6 caveat
        # applies — see config field comment).
        "fetch_concurrency": settings.cloudflare_fetch_concurrency,
        "cloudflare_keys": cloudflare_keys,
        # #35 / #40: select the stealth-browser engine (camoufox default
        # everywhere — dev + CI + prod — so Firefox-only failure modes like
        # issue #54 surface in local repro; patchright opt-in via
        # GATEWAY_CLOUDFLARE_ENGINE=patchright). Driven by Settings.cloudflare_engine.
        "engine": settings.cloudflare_engine,
        # #54 diagnostic: forward the per-page browser-event capture flag.
        # OFF by default — flip to ON via GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1
        # for nightly evidence-capture runs investigating the Playwright
        # Firefox handler crash (see debug session
        # ``comix-pageerror-throw-site-54.md``).
        "log_browser_events": settings.cloudflare_log_browser_events,
    }
    if challenge_url is not None:
        solver_kwargs["challenge_url"] = challenge_url
    # Swap NoopSolver for the ONE shared CloudflareSolver (R1/BOT-01). Construction is
    # cheap (no browser yet — the lazy engine-specific launch happens on the first
    # solve); the eager warm() is fired NON-BLOCKING so a launch/solve failure
    # degrades only cloudflare-gated sources and NEVER aborts startup (D-33/Pitfall 3).
    # Non-cloudflare sources (e.g. ``antibot="none"``) resolve no-clearance.
    solver = CloudflareSolver(**solver_kwargs)
    app.state.solver = solver

    async def _warm_solver() -> None:
        try:
            await solver.warm()  # eager best-effort solve + recycle watchdog (D-33)
        except Exception:  # noqa: BLE001 — cloudflare sources boot disabled, gateway lives
            for key in cloudflare_keys:
                source_health[key].force_disabled = True
            _log.warning(
                "CloudflareSolver warm failed; cloudflare-gated sources disabled (D-33)"
            )

    warm_task = asyncio.create_task(_warm_solver())  # non-blocking (Pitfall 3)
    app.state.ratelimiter = RateLimiter()  # rate-limit seam
    app.state.caps_cache = TTLCache(maxsize=1, ttl=_CAPS_TTL_SECONDS)  # PLAT-04
    app.state.handle_store = HandleStore()  # opaque downloadHandle store (HDL-01/02)
    # Download surface: aiosqlite job store + lifespan-owned JobManager (PLAT-03).
    store = await open_store(settings.db_path)
    job_manager = JobManager(
        store=store,
        registry=registry,
        session=app.state.session,
        ratelimiter=app.state.ratelimiter,
        handle_store=app.state.handle_store,
        settings=settings,
        solver=solver,
        source_health=source_health,
    )
    await job_manager.rehydrate()  # flip in-flight->failed + project rows (PLAT-03)
    # Restart staging sweep (PLAT-03 / T-03-13): clear orphan *.tmp archives left by a
    # crash mid-package; completed output files are never touched. Blocking FS walk is
    # offloaded off the event loop (Pitfall 2).
    swept = await asyncio.to_thread(_sweep_staging, settings.output_root)
    if swept:
        _log.info("Swept %d orphan staging artifact(s) on startup", swept)
    app.state.job_manager = job_manager

    # D-37 recovery supervisor: once a cloudflare-gated breaker trips, re-probe
    # on the escalating +1h/+6h schedule (off the request path), then stay down
    # until a manual restart. Strong-ref'd (manager.py:247-251 idiom) so each
    # fire-and-forget task is never GC'd. A ``force_disabled`` (eager-launch-
    # failed) source is left down. One supervisor task per cloudflare-gated
    # source so they recover independently.
    async def _probe_source(source_key: str) -> bool:
        try:
            clearance = await solver.get_clearance(source_key, force_resolve=True)
        except Exception:  # noqa: BLE001 — a failed re-probe just keeps it down
            return False
        return clearance is not None

    async def _supervise_source(source_key: str) -> None:
        health = source_health[source_key]
        while True:
            await asyncio.sleep(_RECOVERY_POLL_SECONDS)
            # eager-launch-failed (stay down) or healthy — nothing to do
            if health.force_disabled or health.is_enabled:
                continue
            await _recovery_watchdog(
                health,
                backoff_hours=settings.cloudflare_watchdog_backoff_hours,
                probe=lambda: _probe_source(source_key),
            )

    watchdog_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_supervise_source(key)) for key in cloudflare_keys
    ]
    try:
        yield
    finally:
        # Cancel the warm + watchdog bg tasks and close the solver BEFORE the
        # transport, so no orphan Chromium survives shutdown (Pitfall 4).
        bg_tasks = [warm_task, *watchdog_tasks]
        for bg in bg_tasks:
            bg.cancel()
        with suppress(Exception):
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        # Tear the bounded lifecycle + patchright down (Pitfall 4 — orphan Chromium).
        # Guarded so a solver-close failure (timeout/CDP error) does not skip the
        # store + transport teardown that follows.
        with suppress(Exception):
            await solver.aclose()
        # Await in-flight jobs BEFORE releasing the store/transport they depend on
        # (CR-01). Bound the drain so a wedged job cannot hang shutdown: on timeout,
        # cancel the stragglers, then proceed to close the store and transport.
        try:
            async with asyncio.timeout(_SHUTDOWN_DRAIN_TIMEOUT_SECONDS):
                await job_manager.drain()
        except TimeoutError:
            _log.warning(
                "Job drain exceeded %ss on shutdown; cancelling stragglers",
                _SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
            )
            await job_manager.cancel_all()
        await store.close()  # release the job-store connection
        await transport.aclose()  # release the one shared client


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        settings: Explicit settings (tests inject a fixed key per D-03). When
            ``None``, Task 3 wires this to ``load_settings()``.
    """
    if settings is None:
        from .config import load_settings  # noqa: PLC0415

        settings = load_settings()

    app = FastAPI(
        title="Mangarr Manga-Gateway API",
        lifespan=lifespan,
        dependencies=[Depends(require_api_key)],  # GLOBAL auth (AUTH-01/D-02)
        root_path=settings.url_base or "",  # PLAT-01 UrlBase
    )
    app.state.settings = settings
    app.include_router(api_router)
    register_error_handlers(app)
    return app
