"""``GET /recent`` — newest-first recent feed (RCNT-01/02, operationId getRecent).

Mirrors ``/search`` exactly: the route NEVER references MangaDex (SRC-01). It resolves
the selected sources from the registry, builds a framework ``SourceContext`` per source,
and fans the ``recent`` hook out with per-source isolation (one source failing → a
``warnings[]`` entry, still 200). The merged ``releases[]`` are sorted newest-first by
``publishDate`` (RCNT-01); ``since`` is applied as a defensive client-side cut so the
contract guarantee holds regardless of whether a source honored the upstream hint
(RCNT-02 / RESEARCH A3). ``limit`` is clamped to <=2000 at the route boundary (T-02-06).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query, Request

from ...deps import (
    get_failure_cooldown,
    get_handle_store,
    get_ratelimiter,
    get_registry,
    get_session,
    get_session_prep,
    get_solver,
    get_source_health,
    get_source_pinned_proxies,
)

# Runtime imports: FastAPI resolves the route's Annotated[T, Depends(...)] types at
# import time (``from __future__ import annotations`` makes them strings), so the
# dependency types cannot live under TYPE_CHECKING (Plan 01 deviation precedent).
from ...framework.antibot import AntiBotSolver
from ...framework.context import SourceContext
from ...framework.cooldown import SourceFailureCooldown
from ...framework.fanout import fan_out
from ...framework.health import SourceHealth
from ...framework.ratelimit import RateLimiter
from ...framework.registry import SourceRegistry
from ...framework.session import SessionManager
from ...framework.session_prep import SessionPrep
from ...framework.source_pin import SourcePinnedProxies
from ...handles.store import HandleStore
from ...metrics.collector import get_collector
from ...metrics.context import stash_request_blob, stash_request_result
from ...models.search import Release, ReleaseListResponse, SourceWarning
from ..sorting import parse_publish_ts as _parse_ts
from ..sorting import sort_newest_first

if TYPE_CHECKING:
    from ...framework.base import Source

router = APIRouter()


def _emit_source_result(result_count: int, candidates_enumerated: int | None) -> None:
    """No-op-safe per-source summary emit (260605-e9a; mirrors search.py)."""
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


# Contract ceiling for the recent feed (openapi.yaml: limit maximum 2000, T-02-06).
_MAX_LIMIT = 2000

# ``_parse_ts`` (ISO-8601 → aware datetime, malformed → epoch-min floor) and the
# newest-first merge now live in ``api/sorting.py`` so /recent and /search share the
# identical sort (issue #99). Imported above as ``_parse_ts`` to keep the ``since``
# cut below unchanged.


def _split_multi(raw: list[str] | None) -> list[str] | None:
    """Flatten a multi-valued query param into a trimmed, non-empty list.

    Accepts BOTH encodings (Postel's law): repeated params
    (``?sources=a&sources=b`` → ``["a", "b"]``), a single CSV value
    (``?sources=a,b`` → ``["a,b"]``), and any mix (``["a,b", "c"]``). Mangarr sends
    the repeated-param form; the contract historically documented CSV — accepting
    both means neither silently drops sources. FastAPI binds a repeated query param
    into a ``list[str]``; here we additionally split commas inside each element.
    """
    if not raw:
        return None
    items = [part.strip() for value in raw for part in value.split(",") if part.strip()]
    return items or None


def _parse_limit(raw: str | None) -> int:
    """Defensively coerce the ``limit`` query value, clamped to <=2000 (T-02-06/08).

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
    request: Request,
    session: Annotated[SessionManager, Depends(get_session)],
    ratelimiter: Annotated[RateLimiter, Depends(get_ratelimiter)],
    registry: Annotated[SourceRegistry, Depends(get_registry)],
    handle_store: Annotated[HandleStore, Depends(get_handle_store)],
    solver: Annotated[AntiBotSolver, Depends(get_solver)],
    health_map: Annotated[dict[str, SourceHealth], Depends(get_source_health)],
    session_prep: Annotated[SessionPrep, Depends(get_session_prep)],
    failure_cooldown: Annotated[SourceFailureCooldown, Depends(get_failure_cooldown)],
    source_pins: Annotated[SourcePinnedProxies, Depends(get_source_pinned_proxies)],
    sources: Annotated[
        list[str] | None,
        Query(description="Source keys: repeated param (?sources=a&sources=b) or CSV"),
    ] = None,
    languages: Annotated[
        list[str] | None,
        Query(description="BCP-47 codes: repeated param or CSV"),
    ] = None,
    limit: Annotated[str | None, Query(description="Max items; clamped <=2000")] = None,
    since: Annotated[
        str | None, Query(description="Return items newer than this")
    ] = None,
) -> ReleaseListResponse:
    """Fan ``recent`` out across selected sources; newest-first (RCNT-01/02)."""
    # 260605-e9a deliverable 1: capture the request blob (GET → body=None). Path +
    # query come from request.url for the exact 1:1 reconstruction; stashed into
    # current_request, emitted on the umbrella request event by the middleware.
    stash_request_blob(
        method="GET",
        path=request.url.path,
        query_string=request.url.query,
        body=None,
    )
    source_keys = _split_multi(sources)
    language_list = _split_multi(languages)
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
            download_rate_limit_per_minute=getattr(
                src, "download_rate_limit_per_minute", None
            ),
            session=session,
            ratelimiter=ratelimiter,
            handle_store=handle_store,
            solver=solver,
            antibot=src.antibot,
            cloudflare_challenge_optional=getattr(
                src, "cloudflare_challenge_optional", False
            ),
            decrypt_scheme=src.decrypt_scheme,
            decrypt_config=dict(src_decrypt_config) if src_decrypt_config else None,
            source_health=health_map.get(src.key),
            session_prep=session_prep,
            # 260606-lyb Change 1: the SEARCH/recent path retries once (2 attempts)
            # so a down source fails fast; downloads/jobs keep the default 4.
            retry_attempts=2,
            # Phase 16 (PROXY-03/PROXY-04): full kwarg parity with search.py — the R1
            # pinned-proxy singleton + this source's opt-in flag + its bound
            # ``is_origin_block`` predicate, all read via getattr (no source named by
            # key; a non-opted source threads False/None ⇒ byte-for-byte today).
            source_pins=source_pins,
            solve_search_via_proxy_pool=getattr(
                src, "solve_search_via_proxy_pool", False
            ),
            is_origin_block_fn=getattr(src, "is_origin_block", None),
        )
        releases = await src.recent(
            languages=language_list,
            limit=clamped_limit,
            since=since,
            ctx=ctx,
        )
        # 260605-e9a deliverables 3+5: per-source PRE-merge result_count +
        # candidates_enumerated, self-attributed via current_source (bound by
        # fanout._guarded). No-op-safe so a metric error never breaks the source.
        _emit_source_result(len(releases), ctx.candidates_enumerated)
        return releases

    releases, warning_tuples = await fan_out(
        selected, _run_one, cooldown=failure_cooldown
    )

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
    # 260605-e9a deliverables 2+4: final merged result_count + compact warnings
    # summary ride the umbrella request event. Stashed BEFORE returning so the
    # middleware finally-block emit reads a populated contextvar.
    stash_request_result(
        result_count=len(releases),
        warnings_summary=[
            {"source_key": key, "code": code} for key, code, _msg in warning_tuples
        ],
    )
    return ReleaseListResponse(releases=releases, warnings=warnings)
