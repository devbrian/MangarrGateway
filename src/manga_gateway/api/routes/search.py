"""``POST /search`` — source-agnostic fan-out (SRCH-01..07, operationId ``search``).

The route NEVER references MangaDex (SRC-01): it resolves the selected sources from
the registry, builds a framework ``SourceContext`` per source, and calls the
``fan_out`` orchestrator. The SRCH-05 semantic guard (neither ``query`` nor a usable
id) returns ``bad_request`` BEFORE any fan-out (D-23). Per-source failures surface in
``warnings[]`` (200, not an HTTP error).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ...deps import (
    get_handle_store,
    get_ratelimiter,
    get_registry,
    get_session,
    get_solver,
    get_source_health,
)
from ...errors import _error

# Runtime imports: FastAPI must resolve the route's Annotated[T, Depends(...)] types
# at import time (``from __future__ import annotations`` makes them strings), so the
# dependency types cannot live under TYPE_CHECKING.
from ...framework.antibot import AntiBotSolver
from ...framework.context import SourceContext
from ...framework.fanout import fan_out
from ...framework.health import SourceHealth
from ...framework.ratelimit import RateLimiter
from ...framework.registry import SourceRegistry
from ...framework.session import SessionManager
from ...handles.store import HandleStore
from ...models.search import (
    Release,
    ReleaseListResponse,
    SearchRequest,
    SourceWarning,
)

if TYPE_CHECKING:
    from ...framework.base import Source

_log = logging.getLogger("manga_gateway.api.search")

router = APIRouter()

# Id keys that count as "usable search input" for the SRCH-05 guard (D-23).
USABLE_ID_KEYS = {"mangadexId", "anilistId", "malId"}


def _has_usable_search_input(req: SearchRequest) -> bool:
    if req.query and req.query.strip():
        return True
    if not req.ids:
        return False
    # A usable id key must carry a non-empty value — mere presence of an empty,
    # whitespace, or None id does NOT bypass the SRCH-05 guard (D-23).
    for key in USABLE_ID_KEYS:
        value = req.ids.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif value is not None:
            return True
    return False


def _select_sources(
    registry: SourceRegistry, requested: list[str] | None
) -> list[Source]:
    """Resolve requested source keys (or all enabled) to source INSTANCES (SRCH-02)."""
    keys = requested if requested else registry.keys()
    sources: list[Source] = []
    for key in keys:
        cls = registry.get(key)
        if cls is not None:
            sources.append(cls())
    return sources


@router.post(
    "/search",
    response_model=ReleaseListResponse,
    response_model_by_alias=True,
)
async def search(
    req: SearchRequest,
    session: Annotated[SessionManager, Depends(get_session)],
    ratelimiter: Annotated[RateLimiter, Depends(get_ratelimiter)],
    registry: Annotated[SourceRegistry, Depends(get_registry)],
    handle_store: Annotated[HandleStore, Depends(get_handle_store)],
    solver: Annotated[AntiBotSolver, Depends(get_solver)],
    health_map: Annotated[dict[str, SourceHealth], Depends(get_source_health)],
) -> ReleaseListResponse | JSONResponse:
    """Fan out the search across selected sources; isolate failures into warnings[]."""
    # SRCH-05 / D-23: neither query nor a usable id → 400 bad_request.
    if not _has_usable_search_input(req):
        return JSONResponse(
            status_code=400,
            content=_error("bad_request", "query or a usable id is required"),
        )

    sources = _select_sources(registry, req.sources)
    # Per-source soft warnings (D-14): a source that partially degrades emits
    # ``ctx.warn(code, msg)`` and we surface those alongside the fan-out's
    # hard-failure warnings. Captured in a closure so ``collect_warnings``
    # can read them after the source returns.
    soft_warnings: dict[str, list[tuple[str, str]]] = {}

    async def _run_one(src: Source) -> list[Release]:
        ctx = SourceContext(
            source_key=src.key,
            rate_limit_per_minute=src.rate_limit_per_minute,
            session=session,
            ratelimiter=ratelimiter,
            handle_store=handle_store,
            solver=solver,
            antibot=src.antibot,
            decrypt_scheme=src.decrypt_scheme,
            decrypt_config=getattr(src, "decrypt_config", None),
            source_health=health_map.get(src.key),
        )
        started = time.perf_counter()
        releases = await src.search(req, ctx)
        # ``fan_out`` calls ``collect_warnings`` on the SUCCESS path only — its
        # Timeout/SourceError/Exception branches append a hard-failure warning
        # and short-circuit. Recording soft warnings here matches that contract:
        # a source that warned then raised is already represented by the
        # hard-failure entry, so the soft tail would never be read anyway.
        soft_warnings[src.key] = list(ctx.warnings)
        elapsed = time.perf_counter() - started
        # #21: one INFO per source dispatched. Per-source warnings count helps
        # an operator spot a degraded source without re-parsing the response.
        _log.info(
            "search source=%s query=%r results=%d warnings=%d elapsed=%.2fs",
            src.key,
            (req.query or "")[:80],
            len(releases),
            len(ctx.warnings),
            elapsed,
        )
        return releases

    releases, warning_tuples = await fan_out(
        sources,
        _run_one,
        collect_warnings=lambda src: soft_warnings.get(src.key, []),
    )
    # T-02-04: bound the total releases/handles a single search can mint. A title
    # search fans across multiple candidate manga, so per-source/per-manga paging
    # alone does not cap the merged total — truncate to the requested limit.
    releases = releases[: req.limit]
    warnings = [
        SourceWarning(source_key=key, code=code, message=message)
        for key, code, message in warning_tuples
    ]
    return ReleaseListResponse(releases=releases, warnings=warnings)
