"""``GET /caps`` — capabilities handshake (CAPS-01/02/03, PLAT-04, operationId getCaps).

The STATIC skeleton (gateway version, limits, supported search params, download
formats) is served read-through the 12h caps ``TTLCache`` built once in the app
lifespan (PLAT-04). The per-source ``sources[]`` is computed LIVE on every call from
the registry + the lifespan-owned per-source ``SourceHealth`` map — so a tripped
breaker flips ``enabled:false`` in the SAME poll and the 12h cache can NEVER mask a
freshly-degraded source (D-38, the PATTERNS TTLCache-masking caveat). The route stays
source-agnostic (caps come from the registry, never a MangaDex import).
"""

from __future__ import annotations

from typing import Annotated

from cachetools import TTLCache
from fastapi import APIRouter, Depends

from ... import __version__
from ...deps import get_caps_cache, get_registry, get_source_health
from ...framework.health import SourceHealth
from ...framework.registry import SourceRegistry
from ...models.caps import Capabilities

router = APIRouter()

# Cache key for the STATIC skeleton only (the dynamic sources[] is never cached).
_CAPS_SKELETON_KEY = "caps_skeleton"


@router.get("/caps", response_model=Capabilities, response_model_by_alias=True)
async def get_caps(
    cache: Annotated[TTLCache[str, object], Depends(get_caps_cache)],
    registry: Annotated[SourceRegistry, Depends(get_registry)],
    health_map: Annotated[dict[str, SourceHealth], Depends(get_source_health)],
) -> Capabilities:
    """Return the ``Capabilities`` document: cached skeleton + LIVE per-source caps.

    The static skeleton (version, limits, formats, supported params) is cached for 12h
    on a miss. ``sources[]`` is rebuilt every call from ``registry.caps(health_map)`` so
    a breaker trip is observable immediately (D-38) without the cache masking it; the
    contract shape is unchanged (still one ``Capabilities`` doc).
    """
    cached = cache.get(_CAPS_SKELETON_KEY)
    if isinstance(cached, Capabilities):
        skeleton = cached
    else:
        skeleton = Capabilities(gateway_version=__version__)  # static defaults only
        cache[_CAPS_SKELETON_KEY] = skeleton
    # Rebuild the dynamic enabled-per-source list live; keep the cached static fields.
    return skeleton.model_copy(update={"sources": registry.caps(health_map)})
