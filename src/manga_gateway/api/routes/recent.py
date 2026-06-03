"""``GET /recent`` — newest-first recent feed (RCNT-01/02, operationId getRecent).

Mirrors ``/search`` exactly: the route NEVER references MangaDex (SRC-01). It resolves
the selected sources from the registry, builds a framework ``SourceContext`` per source,
and fans the ``recent`` hook out with per-source isolation (one source failing → a
``warnings[]`` entry, still 200). The merged ``releases[]`` are sorted newest-first by
``publishDate`` (RCNT-01); ``since`` is applied as a defensive client-side cut so the
contract guarantee holds regardless of whether a source honored the upstream hint
(RCNT-02 / RESEARCH A3). ``limit`` is clamped to <=100 at the route boundary (T-02-06).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query

from ...deps import (
    get_handle_store,
    get_ratelimiter,
    get_registry,
    get_session,
    get_session_prep,
    get_solver,
    get_source_health,
)

# Runtime imports: FastAPI resolves the route's Annotated[T, Depends(...)] types at
# import time (``from __future__ import annotations`` makes them strings), so the
# dependency types cannot live under TYPE_CHECKING (Plan 01 deviation precedent).
from ...framework.antibot import AntiBotSolver
from ...framework.context import SourceContext
from ...framework.fanout import fan_out
from ...framework.health import SourceHealth
from ...framework.ratelimit import RateLimiter
from ...framework.registry import SourceRegistry
from ...framework.session import SessionManager
from ...framework.session_prep import SessionPrep
from ...handles.store import HandleStore
from ...models.search import Release, ReleaseListResponse, SourceWarning
from ..sorting import parse_publish_ts as _parse_ts
from ..sorting import sort_newest_first

if TYPE_CHECKING:
    from ...framework.base import Source

router = APIRouter()

# Contract ceiling for the recent feed (openapi.yaml: limit maximum 100, T-02-06).
_MAX_LIMIT = 100

# ``_parse_ts`` (ISO-8601 → aware datetime, malformed → epoch-min floor) and the
# newest-first merge now live in ``api/sorting.py`` so /recent and /search share the
# identical sort (issue #99). Imported above as ``_parse_ts`` to keep the ``since``
# cut below unchanged.


def _split_csv(raw: str | None) -> list[str] | None:
    """Parse a comma-separated query value into a trimmed, non-empty list."""
    if not raw:
        return None
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items or None


def _parse_limit(raw: str | None) -> int:
    """Defensively coerce the ``limit`` query value, clamped to <=100 (T-02-06/08).

    The contract documents only 200/401 for ``/recent`` — a malformed ``limit`` must
    NOT surface a 400. Bad/empty input falls back to the contract default (100), and
    any value is clamped to the ceiling.
    """
    if raw is None:
        return _MAX_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _MAX_LIMIT
    if value < 1:
        return _MAX_LIMIT
    return min(value, _MAX_LIMIT)


def _select_sources(
    registry: SourceRegistry, requested: list[str] | None
) -> list[Source]:
    """Resolve requested source keys (or all enabled) to source INSTANCES."""
    keys = requested if requested else registry.keys()
    sources: list[Source] = []
    for key in keys:
        cls = registry.get(key)
        if cls is not None:
            sources.append(cls())
    return sources


@router.get(
    "/recent",
    operation_id="getRecent",
    response_model=ReleaseListResponse,
    response_model_by_alias=True,
)
async def get_recent(
    session: Annotated[SessionManager, Depends(get_session)],
    ratelimiter: Annotated[RateLimiter, Depends(get_ratelimiter)],
    registry: Annotated[SourceRegistry, Depends(get_registry)],
    handle_store: Annotated[HandleStore, Depends(get_handle_store)],
    solver: Annotated[AntiBotSolver, Depends(get_solver)],
    health_map: Annotated[dict[str, SourceHealth], Depends(get_source_health)],
    session_prep: Annotated[SessionPrep, Depends(get_session_prep)],
    sources: Annotated[str | None, Query(description="CSV of source keys")] = None,
    languages: Annotated[str | None, Query(description="CSV of BCP-47 codes")] = None,
    limit: Annotated[str | None, Query(description="Max items; clamped <=100")] = None,
    since: Annotated[
        str | None, Query(description="Return items newer than this")
    ] = None,
) -> ReleaseListResponse:
    """Fan ``recent`` out across selected sources; newest-first (RCNT-01/02)."""
    source_keys = _split_csv(sources)
    language_list = _split_csv(languages)
    clamped_limit = _parse_limit(limit)
    selected = _select_sources(registry, source_keys)

    async def _run_one(src: Source) -> list[Release]:
        # RESEARCH Pitfall 3: this build was previously DROPPING solver/antibot/
        # decrypt_scheme/decrypt_config/source_health — a cloudflare* or csrf-bootstrap
        # /recent fan-out went out with no credentials → 403. Bring it to FULL kwarg
        # parity with search.py:_run_one and engine._build_context. Copy decrypt_config
        # per request so a scheme mutating its config cannot leak state across
        # concurrent requests via the source CLASS attribute (search.py discipline).
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
        )
        return await src.recent(
            languages=language_list,
            limit=clamped_limit,
            since=since,
            ctx=ctx,
        )

    releases, warning_tuples = await fan_out(selected, _run_one)

    # RCNT-02: defensive client-side `since` cut — the contract guarantee holds even if
    # a source ignored the upstream hint (RESEARCH A3). Datetime-parsed compare (WR-02):
    # a release with no provable publishDate (floor) is conservatively excluded.
    if since is not None:
        since_dt = _parse_ts(since)
        releases = [rel for rel in releases if _parse_ts(rel.publish_date) > since_dt]

    # RCNT-01: merge newest-first by publishDate across all sources (datetime-parsed).
    sort_newest_first(releases)
    # T-02-06: enforce the overall limit on the MERGED list — per-source paging caps
    # each source, but the merged feed across sources must not exceed the requested max.
    releases = releases[:clamped_limit]

    warnings = [
        SourceWarning(source_key=key, code=code, message=message)
        for key, code, message in warning_tuples
    ]
    return ReleaseListResponse(releases=releases, warnings=warnings)
