"""Cheap dependency accessors reading the lifespan-built singletons.

These never construct anything per-request — they only read ``request.app.state``
where the lifespan stored the one-per-process seams (R1, PLAT-02).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from cachetools import TTLCache

    from .config import Settings
    from .framework.antibot import AntiBotSolver
    from .framework.ratelimit import RateLimiter
    from .framework.registry import SourceRegistry
    from .framework.session import SessionManager
    from .framework.transport import Transport


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_transport(request: Request) -> Transport:
    transport: Transport = request.app.state.transport
    return transport


def get_session(request: Request) -> SessionManager:
    session: SessionManager = request.app.state.session
    return session


def get_solver(request: Request) -> AntiBotSolver:
    solver: AntiBotSolver = request.app.state.solver
    return solver


def get_ratelimiter(request: Request) -> RateLimiter:
    ratelimiter: RateLimiter = request.app.state.ratelimiter
    return ratelimiter


def get_registry(request: Request) -> SourceRegistry:
    registry: SourceRegistry = request.app.state.registry
    return registry


def get_caps_cache(request: Request) -> TTLCache[str, object]:
    cache: TTLCache[str, object] = request.app.state.caps_cache
    return cache
