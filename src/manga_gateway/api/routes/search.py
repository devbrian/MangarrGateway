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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ...deps import (
    get_enum_cache,
    get_failure_cooldown,
    get_handle_store,
    get_ratelimiter,
    get_registry,
    get_session,
    get_session_prep,
    get_solver,
    get_source_health,
)
from ...errors import _error

# Runtime imports: FastAPI must resolve the route's Annotated[T, Depends(...)] types
# at import time (``from __future__ import annotations`` makes them strings), so the
# dependency types cannot live under TYPE_CHECKING.
from ...framework.antibot import AntiBotSolver
from ...framework.context import SourceContext
from ...framework.cooldown import SourceFailureCooldown
from ...framework.enum_cache import EnumerationCache
from ...framework.fanout import fan_out
from ...framework.health import SourceHealth
from ...framework.ratelimit import RateLimiter
from ...framework.registry import SourceRegistry
from ...framework.session import SessionManager
from ...framework.session_prep import SessionPrep
from ...handles.store import HandleStore
from ...metrics.collector import get_collector
from ...metrics.context import stash_request_blob, stash_request_result
from ...models.search import (
    Release,
    ReleaseListResponse,
    SearchRequest,
    SourceWarning,
)
from ..sorting import sort_newest_first

if TYPE_CHECKING:
    from ...framework.base import Source

_log = logging.getLogger("manga_gateway.api.search")

router = APIRouter()


def _emit_source_result(result_count: int, candidates_enumerated: int | None) -> None:
    """No-op-safe per-source summary emit (260605-e9a deliverables 3+5).

    Mirrors ``context.py:_emit_http``: a ``None`` collector short-circuits and any
    collector-side error is swallowed so a metric failure can never break a source
    run. ``source_key`` self-attributes via ``current_source`` (bound by
    ``fanout._guarded``); ``request_id`` via ``current_request``.
    """
    collector = get_collector()
    if collector is None:
        return
    try:
        collector.emit_source_result(
            result_count=result_count,
            candidates_enumerated=candidates_enumerated,
        )
    except Exception:  # noqa: BLE001 — a metric failure must never break a source
        pass


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
    request: Request,
    session: Annotated[SessionManager, Depends(get_session)],
    ratelimiter: Annotated[RateLimiter, Depends(get_ratelimiter)],
    registry: Annotated[SourceRegistry, Depends(get_registry)],
    handle_store: Annotated[HandleStore, Depends(get_handle_store)],
    solver: Annotated[AntiBotSolver, Depends(get_solver)],
    health_map: Annotated[dict[str, SourceHealth], Depends(get_source_health)],
    session_prep: Annotated[SessionPrep, Depends(get_session_prep)],
    enum_cache: Annotated[EnumerationCache, Depends(get_enum_cache)],
    failure_cooldown: Annotated[SourceFailureCooldown, Depends(get_failure_cooldown)],
) -> ReleaseListResponse | JSONResponse:
    """Fan out the search across selected sources; isolate failures into warnings[]."""
    # 260605-e9a deliverable 1: capture the request blob (method/path/query from
    # request.url for the exact 1:1 reconstruction; body = the parsed model
    # serialized to the wire shape). Stashed into current_request; the middleware
    # finally-block emits it on the umbrella request event. NO body-buffering — the
    # parsed model is already in hand. Secrets are redacted at stash time.
    #
    # Stashed BEFORE the SRCH-05 early return (CodeRabbit #129 outside-diff) so a
    # 400 bad_request is still reconstructable — a malformed search is exactly the
    # kind of request you want to debug from telemetry.
    stash_request_blob(
        method="POST",
        path=request.url.path,
        query_string=request.url.query,
        body=req.model_dump(by_alias=True),
    )

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
        # Copy ``decrypt_config`` per request so a scheme that mutates its
        # config cannot leak state across concurrent requests via the source
        # CLASS attribute.
        src_decrypt_config = getattr(src, "decrypt_config", None)
        ctx = SourceContext(
            source_key=src.key,
            rate_limit_per_minute=src.rate_limit_per_minute,
            session=session,
            ratelimiter=ratelimiter,
            handle_store=handle_store,
            solver=solver,
            antibot=src.antibot,
            decrypt_scheme=src.decrypt_scheme,
            decrypt_config=dict(src_decrypt_config) if src_decrypt_config else None,
            source_health=health_map.get(src.key),
            session_prep=session_prep,
            enum_cache=enum_cache,
            # 260606-lyb Change 1: the SEARCH path retries once (2 attempts) so a
            # down source fails fast on a repeat search; downloads/jobs keep the
            # default 4-attempt budget (they never pass retry_attempts).
            retry_attempts=2,
        )
        started = time.perf_counter()
        releases = await src.search(req, ctx)
        # ``fan_out`` calls ``collect_warnings`` on the SUCCESS path only — its
        # Timeout/SourceError/Exception branches append a hard-failure warning
        # and short-circuit. Recording soft warnings here matches that contract:
        # a source that warned then raised is already represented by the
        # hard-failure entry, so the soft tail would never be read anyway.
        soft_warnings[src.key] = list(ctx.warnings)
        # 260605-e9a deliverables 3+5: per-source PRE-merge result_count + how many
        # candidates were deep-enumerated, self-attributed via current_source (bound
        # by fanout._guarded). Uses the SAME len(releases) the INFO log reports.
        _emit_source_result(len(releases), ctx.candidates_enumerated)
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
        cooldown=failure_cooldown,
    )
    # #99: merge newest-first by publishDate across ALL sources BEFORE truncating —
    # mirroring /recent (RCNT-01). fan_out concatenates results in source order, so
    # without this sort ``[: limit]`` drops whichever sources land past the cap,
    # letting a high-volume source (e.g. mangaball/mangadot returning 50-250 hits)
    # silently starve sources after it of releases that exist upstream.
    sort_newest_first(releases)
    # T-02-04: bound the total releases/handles a single search can mint. A title
    # search fans across multiple candidate manga, so per-source/per-manga paging
    # alone does not cap the merged total — truncate to the requested limit.
    releases = releases[: req.limit]
    warnings = [
        SourceWarning(source_key=key, code=code, message=message)
        for key, code, message in warning_tuples
    ]
    # 260605-e9a deliverables 2+4: the final merged result_count + a compact
    # warnings summary ride the umbrella request event (one value per request).
    # Stashed BEFORE returning so the middleware finally-block emit (which runs
    # after the handler returns) reads a populated contextvar.
    stash_request_result(
        result_count=len(releases),
        warnings_summary=[
            {"source_key": key, "code": code} for key, code, _msg in warning_tuples
        ],
    )
    return ReleaseListResponse(releases=releases, warnings=warnings)
