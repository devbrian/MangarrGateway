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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
from .security import require_api_key
from .sources import register_builtin_sources

# 12h caps TTL (PLAT-04).
_CAPS_TTL_SECONDS = 43_200


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
    try:
        yield
    finally:
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
