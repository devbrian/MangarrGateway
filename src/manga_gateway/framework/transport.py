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

import asyncio
import importlib.util
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from .proxy import build_proxy

if TYPE_CHECKING:
    from ..config import Settings

# Honest, non-spoofed UA (MangaDex requires a descriptive UA — RESEARCH MangaDex
# Contract; SRC-05). One place the outbound identity lives.
_USER_AGENT = "MangaGateway/1.0"

# Per-client connection limits — the ONE place they live (SRC-04). Sources never
# construct their own client, so this bounds outbound concurrency per pool.
#
# Raised 100→500 / keepalive 20→100 and SPLIT INTO TWO POOLS (debug
# pool-starves-search-cooldown, 2026-06-17). Previously a SINGLE 100-connection
# pool served BOTH the search/recent fan-out AND the download image fetches. A
# large download backlog (per-source max_concurrent_jobs × image_fetch_concurrency)
# plus a concurrent search fan-out (8 sources, each deep-enumerating candidates at
# _CHAPTERS_FANOUT_CONCURRENCY) jointly drove the one pool to its 100 ceiling;
# whichever side lost the acquisition race raised PoolTimeout, and a search source
# that repeatedly PoolTimeouts trips its SourceFailureCooldown backoff → ALL sources
# report source_unavailable. The fix gives the download surface its OWN client/pool
# (app.py builds two HttpxTransport instances; SessionManager.download_transport
# routes the engine's download contexts there) so download saturation can never
# starve search. The 500/100 bump is extra headroom on top of the structural split
# (file-descriptor cost is trivial — container nofile is 1,048,576). Clearance is
# injected as a PER-REQUEST Cookie header (context.py _clearance_kwargs), NOT on a
# client cookie jar, so the two clients need no cookie/clearance mirroring.
_LIMITS = httpx.Limits(max_connections=500, max_keepalive_connections=100)

# Explicit per-request deadlines so no outbound call can hang unbounded (CR-02).
# Lives HERE with the other client-level config; the per-job asyncio.timeout wrapper
# is a separate concern handled in the job engine, not added here.
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# TOTAL wall-clock ceiling per outbound request (debug all-sources-pooltimeout,
# 2026-06-13). httpx's ``read`` timeout above is PER-CHUNK — it resets on every byte
# received — so a slow-trickling / tarpitting upstream (e.g. a Cloudflare-flagged
# host dribbling a byte every few seconds) NEVER trips ``read`` and holds its pooled
# connection indefinitely. Within any single pool, enough such stuck requests drain
# every slot and every request sharing that pool then fails with ``PoolTimeout`` — a
# permanent outage that only a restart clears. This hard total deadline bounds each
# request's wall-time regardless of per-chunk progress; on expiry the request is
# cancelled, which cleanly releases the connection back to the pool (httpx closes the
# response on ``BaseException``), so a tarpit becomes a bounded, retryable timeout
# instead of a permanent leak. 60s is generous headroom over connect(10)+read(30) for
# any legitimate API call or CDN image fetch while still killing a tarpit.
_TOTAL_REQUEST_DEADLINE = 60.0

# HTTP/2 needs the optional ``h2`` package; enable only when present so the gateway
# runs with the locked dependency set (falls back to HTTP/1.1 transparently).
_HTTP2 = importlib.util.find_spec("h2") is not None

# Sentinel distinguishing "caller passed no proxy_override" (derive from settings, the
# byte-for-byte-unchanged default) from "caller explicitly passed None" — both differ
# from passing a concrete proxy. A bare ``None`` default could not express the
# image-proxy-pool path (260620-4im), which constructs one transport PER residential
# proxy via an explicit ``proxy_override``.
_NO_PROXY_OVERRIDE = object()


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

    PER-PROXY-POOL OVERRIDE (260620-4im): ``proxy_override`` lets the framework
    image-proxy pool build ONE transport per residential proxy — httpx binds the
    proxy at client construction, so a distinct client per proxy is required. When
    ``proxy_override`` is the ``_NO_PROXY_OVERRIDE`` sentinel (the default, every
    existing caller), the proxy is derived from ``build_proxy(settings)`` exactly as
    today; otherwise the supplied override is used verbatim (a ``None`` override still
    omits ``proxy=``).
    """

    def __init__(
        self,
        settings: Settings,
        proxy_override: httpx.Proxy | str | None = _NO_PROXY_OVERRIDE,  # type: ignore[assignment]
    ) -> None:
        self._settings = settings
        # One shared client. Non-spoofed UA + per-host limits live HERE (SRC-04/SRC-05);
        # HTTP/2 when ``h2`` is available. The httpx proxy is the second element of
        # the shared build_proxy helper's pair (the browser dict is the first) UNLESS
        # an explicit per-proxy override was supplied (the image-proxy pool path).
        if proxy_override is _NO_PROXY_OVERRIDE:
            _, httpx_proxy = build_proxy(settings)
        else:
            httpx_proxy = proxy_override
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
        # Wrap the call in a TOTAL wall-clock deadline (_TOTAL_REQUEST_DEADLINE) so a
        # tarpitting upstream cannot defeat httpx's PER-CHUNK ``read`` timeout and pin
        # a pooled connection forever (debug all-sources-pooltimeout). The deadline is
        # surfaced as ``httpx.ReadTimeout`` (a ``TransportError``) so the context's
        # existing ``_is_retryable`` / tenacity policy treats it like any other
        # timeout — retried with backoff, then terminal → source cooldown.
        #
        # CONNECTION-LEAK FIX (debug pooltimeout-recurrence, 2026-06-18): use the
        # explicit STREAMING send (``send(stream=True)`` + ``aread()`` + a guaranteed
        # ``aclose()`` in ``finally``) instead of the buffered ``client.request()``.
        # The buffered helper provides NO guarantee that the pooled connection is
        # released when the in-flight coroutine is CANCELLED by an OUTER scope — and
        # that is exactly the live failure mode: the search fan-out's per-source
        # ``asyncio.timeout(30s)`` fires BEFORE this inner 60s deadline (and the job
        # engine's per-page TaskGroup cancels in-flight siblings), re-raising
        # ``CancelledError`` straight through ``client.request`` mid-response. httpcore
        # 1.0.9 then loses track of the connection: the OS socket stays ESTABLISHED
        # (idle, no keepalive timer) and — critically — its LOGICAL pool slot is never
        # returned. Enough leaked slots exhaust ``max_connections`` and every new
        # acquisition blocks for ``pool=10s`` → ``PoolTimeout``. Observed live: 503
        # idle no-timer ESTABLISHED sockets (445 to 4 CF hosts) against a 100 keepalive
        # cap. The 60s deadline (df08b38) didn't help — it only converts ITS OWN
        # TimeoutError, not an external CancelledError; the ceiling bump (9c01865) only
        # delayed exhaustion. Streaming with ``finally: await response.aclose()``
        # releases the connection on EVERY exit path (success, error, AND cancellation),
        # which closes the leak at the single choke point.
        request = self._client.build_request(method, url, **kwargs)
        try:
            async with asyncio.timeout(_TOTAL_REQUEST_DEADLINE):
                response = await self._client.send(request, stream=True)
                try:
                    await response.aread()
                finally:
                    # Release the connection back to the pool on EVERY exit — success,
                    # error, OR cancellation. This is the leak fix: without it a
                    # cancellation between ``send`` and the implicit close leaves the
                    # connection checked out forever (the live PoolTimeout cause).
                    await response.aclose()
                return response
        except TimeoutError as exc:
            raise httpx.ReadTimeout(
                f"total request deadline ({_TOTAL_REQUEST_DEADLINE}s) exceeded",
                request=request,
            ) from exc

    async def aclose(self) -> None:
        await self._client.aclose()
