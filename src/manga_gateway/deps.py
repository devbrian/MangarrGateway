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
    from .framework.health import SourceHealth
    from .framework.ratelimit import RateLimiter
    from .framework.registry import SourceRegistry
    from .framework.session import SessionManager
    from .framework.session_prep import SessionPrep
    from .framework.transport import Transport
    from .handles.store import HandleStore
    from .jobs.manager import JobManager
    from .metrics.snapshot import MetricSnapshotStore
    from .metrics.store import InMemoryStore


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


def get_source_health(request: Request) -> dict[str, SourceHealth]:
    """The lifespan-owned per-source health map (D-36/D-38).

    Mirrors ``get_solver`` but tolerates the pre-Plan-04 app:
    ``app.state.source_health`` is wired in Plan 04, so until then this returns an
    empty map and every
    ``health_map.get(key)`` is ``None`` (sources stay always-enabled — MangaDex
    regression, no breaker yet).
    """
    health: dict[str, SourceHealth] = getattr(request.app.state, "source_health", {})
    return health


def get_session_prep(request: Request) -> SessionPrep:
    """The lifespan-owned shared session-prep provider (D-01, Phase 7).

    Mirrors ``get_solver`` but tolerates the pre-Plan-02 app: until the lifespan
    wires ``app.state.session_prep`` (the shared ``CsrfBootstrap``) this falls back
    to a ``NoSessionPrep`` so every ``prepare(key)`` returns ``None`` — MangaDex/
    Comix contribute no CSRF header and stay byte-for-byte unchanged.
    """
    from .framework.session_prep import NoSessionPrep  # noqa: PLC0415

    provider: SessionPrep = (
        getattr(request.app.state, "session_prep", None) or NoSessionPrep()
    )
    return provider


def get_ratelimiter(request: Request) -> RateLimiter:
    ratelimiter: RateLimiter = request.app.state.ratelimiter
    return ratelimiter


def get_registry(request: Request) -> SourceRegistry:
    registry: SourceRegistry = request.app.state.registry
    return registry


def get_caps_cache(request: Request) -> TTLCache[str, object]:
    cache: TTLCache[str, object] = request.app.state.caps_cache
    return cache


def get_handle_store(request: Request) -> HandleStore:
    store: HandleStore = request.app.state.handle_store
    return store


def get_job_manager(request: Request) -> JobManager:
    job_manager: JobManager = request.app.state.job_manager
    return job_manager


def get_metric_store(request: Request) -> InMemoryStore:
    """The lifespan-owned (rehydrated) live ``InMemoryStore`` (OBS-05/06).

    Mirrors ``get_job_manager``: a one-liner reading the singleton the lifespan
    stashed on ``app.state.metric_store``. The admin metrics routes read this to
    serve flat-cost JSON (poll-friendly, like ``GET /downloads``).
    """
    store: InMemoryStore = request.app.state.metric_store
    return store


def get_metric_ring_store(request: Request) -> MetricSnapshotStore | None:
    """The lifespan-owned disk ring store (recent/failures/slow), or ``None``.

    The ring events moved to the on-disk ``ring_events`` table (260604-wm2): the
    admin recent/failures/slow/requests-{id} routes read THIS store's indexed
    queries instead of the in-memory deques. ``None`` is the degraded mode (the
    snapshot DB could not be opened — ``test_create_app_boots_when_metrics_db_
    unwritable``); the routes then return ``[]`` / 404, never 500. Tolerates the
    pre-wiring app via ``getattr`` (mirrors ``get_source_health``).
    """
    snapshot: MetricSnapshotStore | None = getattr(
        request.app.state, "metric_snapshot", None
    )
    return snapshot
