"""``GET /caps`` — capabilities handshake (CAPS-01/02/03, PLAT-04, operationId getCaps).

Served read-through the 12h caps ``TTLCache`` seam built once in the app lifespan
(PLAT-04). Phase 2: the cached document advertises the live per-source ``SourceCap``
list read from the registry (``registry.caps()`` → each source's ``source_cap()``) —
the route stays source-agnostic (CAPS comes from the registry, never a MangaDex import).
"""

from __future__ import annotations

from typing import Annotated

from cachetools import TTLCache
from fastapi import APIRouter, Depends

from ... import __version__
from ...deps import get_caps_cache, get_registry
from ...framework.registry import SourceRegistry
from ...models.caps import Capabilities

router = APIRouter()

# Single key in the maxsize=1 caps cache (PLAT-04).
_CAPS_KEY = "caps"


@router.get("/caps", response_model=Capabilities, response_model_by_alias=True)
async def get_caps(
    cache: Annotated[TTLCache[str, object], Depends(get_caps_cache)],
    registry: Annotated[SourceRegistry, Depends(get_registry)],
) -> Capabilities:
    """Return the cached ``Capabilities`` document (read-through the 12h TTLCache).

    On a cache miss the document is built once with the gateway version and the live
    per-source caps from the registry (CAPS-02/03) and stored; subsequent calls within
    the 12h TTL return the same cached object.
    """
    cached = cache.get(_CAPS_KEY)
    if isinstance(cached, Capabilities):
        return cached
    caps = Capabilities(
        gateway_version=__version__,
        sources=registry.caps(),  # each source's source_cap() (CAPS-02/03)
    )
    cache[_CAPS_KEY] = caps
    return caps
