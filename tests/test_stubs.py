"""Shape assertions for ``GET /caps`` + ``GET /status``.

These complement the schemathesis contract harness (``test_contract.py``) with
explicit, human-readable shape checks: camelCase field names on the wire, the
``downloadFormats`` enum subset, and the PLAT-04 caps-cache read-through (same
object served on a second request).

Phase 2 (Plan 02-02) flips the former Phase-1 ``sources: []`` invariant (D-05): the
caps document now advertises the live per-source ``SourceCap`` list from the registry
(CAPS-02/03). The detailed MangaDex SourceCap content is asserted in
``test_recent_caps.py``; here we only assert the registry-driven shape.
"""

from __future__ import annotations

import httpx
import pytest

_DOWNLOAD_FORMAT_ENUM = {"cbz", "cbt", "folder"}


@pytest.mark.asyncio
async def test_caps_shape_and_registry_sources(client: httpx.AsyncClient) -> None:
    """GET /caps returns camelCase fields, registry-driven sources, valid formats."""
    resp = await client.get("/caps")
    assert resp.status_code == 200
    body = resp.json()

    # camelCase on the wire (response_model_by_alias=True).
    assert isinstance(body["gatewayVersion"], str) and body["gatewayVersion"]
    assert isinstance(body["supportedSearchParams"], list)
    assert body["supportedSearchParams"]  # non-empty list of str
    assert all(isinstance(p, str) for p in body["supportedSearchParams"])

    # Phase 2: sources is registry-driven (CAPS-02/03) — at least the MangaDex source.
    assert isinstance(body["sources"], list)
    assert body["sources"]  # non-empty: the live registered sources
    assert all("key" in src for src in body["sources"])

    # limits.{defaultPageSize, maxPageSize} are ints.
    assert isinstance(body["limits"]["defaultPageSize"], int)
    assert isinstance(body["limits"]["maxPageSize"], int)

    # downloadFormats subset of the contract enum.
    assert set(body["downloadFormats"]) <= _DOWNLOAD_FORMAT_ENUM
    assert body["downloadFormats"]


@pytest.mark.asyncio
async def test_caps_served_from_cache(client: httpx.AsyncClient) -> None:
    """PLAT-04: /caps is served read-through the 12h caps TTLCache.

    The lifespan-built cache persists across requests within the same app, so the
    second request returns the same cached document the first one populated.
    """
    first = await client.get("/caps")
    second = await client.get("/caps")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()


@pytest.mark.asyncio
async def test_caps_cache_object_identity(app, client: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """PLAT-04: the cache holds the SAME static skeleton object across requests.

    D-38 refactor: only the STATIC skeleton (version/limits/formats) is cached; the
    per-source ``sources[]`` is recomputed live each call so a breaker trip is never
    masked by the 12h cache. The cached skeleton object is still reused by identity.
    """
    from manga_gateway.api.routes.caps import _CAPS_SKELETON_KEY
    from manga_gateway.models.caps import Capabilities

    await client.get("/caps")
    cached_first = app.state.caps_cache.get(_CAPS_SKELETON_KEY)
    assert isinstance(cached_first, Capabilities)

    await client.get("/caps")
    cached_second = app.state.caps_cache.get(_CAPS_SKELETON_KEY)
    # Identity: the read-through did not rebuild the cached skeleton.
    assert cached_first is cached_second


@pytest.mark.asyncio
async def test_status_shape(client: httpx.AsyncClient) -> None:
    """GET /status returns the contract-shaped StatusResponse with camelCase fields."""
    resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()

    # isLocalhost is true under the default 127.0.0.1 test bind (AUTH-02).
    assert body["isLocalhost"] is True
    # STAT-01: outputRootFolders reports the gateway-determined output root.
    assert body["outputRootFolders"] == ["/data/manga"]
    assert isinstance(body["version"], str) and body["version"]
    assert body["removesCompletedDownloads"] is False  # D-28

    caps = body["capabilities"]
    assert caps["pause"] is False  # PAUSE-01 is v2 — unsupported in v1
    assert set(caps["outputFormats"]) >= {"cbz", "cbt", "folder"}
    # STAT-01: maxConcurrentChapters reflects the effective D-30 config global (8).
    assert caps["maxConcurrentChapters"] == 8
