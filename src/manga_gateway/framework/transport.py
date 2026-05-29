"""Outbound transport seam (SRC-04, proxy-ready).

All outbound HTTP goes through an injectable ``Transport`` so per-source/global
proxy pools + rotation (PROXY-01, v2) drop in WITHOUT touching source
subclasses. Nothing fetches in Phase 1 — this only establishes the seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from ..config import Settings


@runtime_checkable
class Transport(Protocol):
    """Injectable outbound HTTP transport."""

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform an outbound request."""
        ...

    async def aclose(self) -> None:
        """Release the underlying client/connections."""
        ...


class HttpxTransport:
    """Default transport wrapping ONE shared ``httpx.AsyncClient``.

    PROXY-01 (v2) injects httpx proxies/mounts HERE only — source subclasses
    never construct their own client, keeping R1's single shared session intact.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # One shared client. HTTP/2 + per-host limits land with the first
        # source (Phase 2); proxies/mounts inject here in v2 (SRC-04).
        self._client = httpx.AsyncClient()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
