"""Outbound transport seam (SRC-04, proxy-ready).

All outbound HTTP goes through an injectable ``Transport`` so per-source/global
proxy pools + rotation (PROXY-01) drop in WITHOUT touching source subclasses.

Single-static-proxy support is now LIVE (#65): when ``cloudflare_proxy_server``
is configured, the ONE shared ``httpx.AsyncClient`` egresses through the same
proxy as the stealth browser — both derived from ``framework.proxy.build_proxy``
so they share one IP (cf_clearance is IP-bound). A future static-pool/rotation
implementation replaces only that helper, not this injection point.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from .proxy import build_proxy

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

    PROXY-01 injects the httpx proxy HERE only (#65, now live) — source
    subclasses never construct their own client, keeping R1's single shared
    session intact. The proxy is derived from the SAME ``build_proxy`` helper
    the lifespan feeds the browser launch closures, so both legs egress through
    one IP (cf_clearance is IP-bound).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # One shared client. Non-spoofed UA + per-host limits live HERE (SRC-04/SRC-05);
        # HTTP/2 when ``h2`` is available. The httpx proxy is the second element of
        # the shared build_proxy helper's pair (the browser dict is the first).
        _, httpx_proxy = build_proxy(settings)
        # Build kwargs conditionally: pass ``proxy=`` ONLY when configured so the
        # no-proxy client construction is byte-for-byte unchanged (the #65
        # regression contract). httpx 0.28.x uses ``proxy=`` (singular).
        client_kwargs: dict[str, Any] = {
            "headers": {"User-Agent": _USER_AGENT},
            "http2": _HTTP2,
            "limits": _LIMITS,
            "timeout": _TIMEOUT,  # bounded per-request deadline (CR-02)
        }
        if httpx_proxy is not None:
            client_kwargs["proxy"] = httpx_proxy
        self._client = httpx.AsyncClient(**client_kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
