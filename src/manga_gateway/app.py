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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from cachetools import TTLCache
from fastapi import Depends, FastAPI

from .api import api_router
from .config import Settings
from .errors import register_error_handlers
from .framework.antibot import NoopSolver
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the R1 singleton seams once; tear the transport down on shutdown."""
    settings: Settings = app.state.settings
    transport = HttpxTransport(settings)  # SRC-04 injectable seam
    app.state.transport = transport
    app.state.session = SessionManager(transport)  # R1 shared session
    app.state.solver = NoopSolver()  # BOT-01 default
    app.state.ratelimiter = RateLimiter()  # rate-limit seam
    registry = SourceRegistry()  # SRC-01
    register_builtin_sources(registry)  # register MangaDex into THIS instance
    app.state.registry = registry
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
    )
    await job_manager.rehydrate()  # flip in-flight->failed + project rows (PLAT-03)
    # Restart staging sweep (PLAT-03 / T-03-13): clear orphan *.tmp archives left by a
    # crash mid-package; completed output files are never touched. Blocking FS walk is
    # offloaded off the event loop (Pitfall 2).
    swept = await asyncio.to_thread(_sweep_staging, settings.output_root)
    if swept:
        _log.info("Swept %d orphan staging artifact(s) on startup", swept)
    app.state.job_manager = job_manager
    try:
        yield
    finally:
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
