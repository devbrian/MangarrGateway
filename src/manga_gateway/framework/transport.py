"""Outbound transport seam (SRC-04, proxy-ready).

All outbound HTTP goes through an injectable ``Transport`` so per-source/global
proxy pools + rotation (PROXY-01, v2) drop in WITHOUT touching source
subclasses. Nothing fetches in Phase 1 — this only establishes the seam.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from ..config import Settings

# Honest, non-spoofed UA (MangaDex requires a descriptive UA — RESEARCH MangaDex
# Contract; SRC-05). One place the outbound identity lives.
_USER_AGENT = "MangaGateway/1.0"

# Per-host connection limits — the ONE place they live (SRC-04). Sources never
# construct their own client, so this bounds outbound concurrency process-wide.
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)

# Explicit per-request deadlines so no outbound call can hang unbounded (CR-02).
# Lives HERE with the other client-level config; the per-job asyncio.timeout wrapper
# is a separate concern handled in the job engine, not added here.
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# HTTP/2 needs the optional ``h2`` package; enable only when present so the gateway
# runs with the locked dependency set (falls back to HTTP/1.1 transparently).
_HTTP2 = importlib.util.find_spec("h2") is not None


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
        # One shared client. Non-spoofed UA + per-host limits live HERE (SRC-04/SRC-05);
        # HTTP/2 when ``h2`` is available; proxies/mounts inject here in v2 (PROXY-01).
        self._client = httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            http2=_HTTP2,
            limits=_LIMITS,
            timeout=_TIMEOUT,  # bounded per-request deadline (CR-02)
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
