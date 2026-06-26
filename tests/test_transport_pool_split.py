"""Regression: the download surface uses a SEPARATE connection pool from the
search/recent fan-out, so a large download backlog can never exhaust the pool the
search surface needs.

Root cause of debug ``pool-starves-search-cooldown`` (2026-06-17): ONE shared
``httpx.AsyncClient`` (``max_connections=100``) served BOTH the search fan-out AND
the download image fetches. A download backlog plus a concurrent search fan-out
jointly drove the single pool to its ceiling; a search source that then raised
``httpx.PoolTimeout`` tripped the 300s ``SourceFailureCooldown`` → every source
reported ``source_unavailable``. The fix splits the transport into two pools
(``SessionManager.transport`` vs ``SessionManager.download_transport``); the
download path (``engine._build_context`` → ``use_download_transport=True``) routes
there. Both pools share ONE identity because clearance rides per-request headers,
not a client cookie jar.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from manga_gateway.config import Settings
from manga_gateway.framework import transport as transport_mod
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.framework.transport import HttpxTransport
from manga_gateway.handles.store import HandleStore

TEST_API_KEY = "k" * 32


# ───────────────────────────── fakes & helpers ──────────────────────────────


class _RecordingTransport:
    """Fake Transport that records how many requests it served (and returns 200)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            200, json={"served_by": self.name}, request=httpx.Request(method, url)
        )

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _ctx(session: SessionManager, *, use_download_transport: bool) -> SourceContext:
    return SourceContext(
        source_key="x",
        rate_limit_per_minute=6000,
        session=session,
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        use_download_transport=use_download_transport,
    )


async def _start_server(slow_delay: float) -> tuple[asyncio.AbstractServer, str]:
    """Local HTTP/1.1 server: a request whose path contains ``slow`` HOLDS its
    connection for ``slow_delay`` seconds before replying; anything else replies
    immediately. ``Connection: close`` so each request maps to one connection."""

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return
        path = request_line.split(b" ")[1] if b" " in request_line else b"/"
        if b"slow" in path:
            await asyncio.sleep(slow_delay)
        body = b"ok"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(body), body)
        )
        try:
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return server, f"http://{host}:{port}"


def _tiny_pool(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    """Shrink the pool to ``n`` and the pool-acquire wait to 0.5s so saturation is
    deterministic and the starvation control fails fast (not the 10s prod wait)."""
    monkeypatch.setattr(
        transport_mod,
        "_LIMITS",
        httpx.Limits(max_connections=n, max_keepalive_connections=n),
    )
    monkeypatch.setattr(
        transport_mod,
        "_TIMEOUT",
        httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=0.5),
    )


# ───────────────────────── SessionManager wiring ────────────────────────────


def test_download_transport_defaults_to_search_transport() -> None:
    # Single-arg construction = single shared pool (pre-split behaviour) so every
    # existing caller/test is unchanged.
    t = _RecordingTransport("shared")
    sm = SessionManager(t)
    assert sm.transport is t
    assert sm.download_transport is t


def test_download_transport_is_distinct_when_provided() -> None:
    search, download = _RecordingTransport("search"), _RecordingTransport("download")
    sm = SessionManager(search, download)
    assert sm.transport is search
    assert sm.download_transport is download


# ───────────────────────── SourceContext routing ────────────────────────────


@pytest.mark.asyncio
async def test_download_context_routes_to_download_pool() -> None:
    search, download = _RecordingTransport("search"), _RecordingTransport("download")
    session = SessionManager(search, download)

    await _ctx(session, use_download_transport=True).get_json("https://x/api")
    assert download.calls == 1
    assert search.calls == 0  # download traffic NEVER touches the search pool


@pytest.mark.asyncio
async def test_search_context_routes_to_search_pool() -> None:
    search, download = _RecordingTransport("search"), _RecordingTransport("download")
    session = SessionManager(search, download)

    await _ctx(session, use_download_transport=False).get_json("https://x/api")
    assert search.calls == 1
    assert download.calls == 0


# ──────────────────── behavioural isolation (the fix) ───────────────────────


@pytest.mark.asyncio
async def test_saturated_download_pool_does_not_starve_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pool of 1 per client; a slow download holds the download pool's only slot.
    _tiny_pool(monkeypatch, 1)
    server, base = await _start_server(slow_delay=2.0)
    settings = Settings(api_key=TEST_API_KEY)
    search_tx = HttpxTransport(settings)
    download_tx = HttpxTransport(settings)
    session = SessionManager(search_tx, download_tx)
    try:
        # Occupy the DOWNLOAD pool's single connection with an in-flight request.
        hold = asyncio.create_task(
            session.download_transport.request("GET", f"{base}/slow")
        )
        await asyncio.sleep(0.2)  # let it acquire the slot
        # A concurrent SEARCH request must still succeed quickly — separate pool.
        resp = await asyncio.wait_for(
            session.transport.request("GET", f"{base}/fast"), timeout=1.5
        )
        assert resp.status_code == 200
        assert (await hold).status_code == 200
    finally:
        await search_tx.aclose()
        await download_tx.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_single_shared_pool_starves_search_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CONTROL: the pre-split topology (one pool for both) DID starve search —
    # proves the test would catch a regression back to a shared pool.
    _tiny_pool(monkeypatch, 1)
    server, base = await _start_server(slow_delay=2.0)
    settings = Settings(api_key=TEST_API_KEY)
    shared = HttpxTransport(settings)
    session = SessionManager(shared)  # download falls back to the SAME pool
    try:
        hold = asyncio.create_task(
            session.download_transport.request("GET", f"{base}/slow")
        )
        await asyncio.sleep(0.2)
        with pytest.raises(httpx.PoolTimeout):
            await session.transport.request("GET", f"{base}/fast")
        await hold
    finally:
        await shared.aclose()
        server.close()
        await server.wait_closed()


# ────────── Phase 16 STEP B: opted-in search egress routes through the pin ──────────
#
# The transport pick in ``_send_emitting`` already routes through
# ``pool.transport_for(active_proxy)`` when ``_ACTIVE_PROXY`` is set; STEP B SETS it for
# an opted-in source's ``get_json``. A non-opted source leaves it unset and routes the
# existing search/download transport byte-for-byte (the regression contract).

from pydantic import SecretStr  # noqa: E402

from manga_gateway.framework.proxy_pool import PooledProxy, ProxyPool  # noqa: E402
from manga_gateway.framework.source_pin import SourcePinnedProxies  # noqa: E402

_PIN_HOST = "proxy.invalid"
_PIN_USER = "fakeuser"
_PIN_PASS = "NOTAREALSECRET-zzz9"  # distinctive sentinel — never a real credential


class _PinTransport:
    """A per-proxy transport that records it served the request."""

    def __init__(self, settings: object, *, proxy_override: object) -> None:
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            200, json={"served_by": "pin"}, request=httpx.Request(method, url)
        )

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _pinned_pool() -> tuple[ProxyPool, dict[str, _PinTransport]]:
    """A real ``ProxyPool`` over one fake proxy with a recording per-proxy transport."""
    made: dict[str, _PinTransport] = {}

    def _factory(settings: object, *, proxy_override: object) -> _PinTransport:
        t = _PinTransport(settings, proxy_override=proxy_override)
        made["pin"] = t
        return t

    pool = ProxyPool(
        [PooledProxy(_PIN_HOST, 8000, _PIN_USER, SecretStr(_PIN_PASS))],
        settings=Settings(api_key=TEST_API_KEY),  # type: ignore[call-arg]
        cooldown_seconds=300.0,
        transport_factory=_factory,  # type: ignore[arg-type]
    )
    return pool, made


def _opted_ctx(
    session: SessionManager, pins: SourcePinnedProxies, *, opted: bool
) -> SourceContext:
    return SourceContext(
        source_key="mangadot",
        rate_limit_per_minute=6000,
        session=session,
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        source_pins=pins,
        solve_search_via_proxy_pool=opted,
    )


@pytest.mark.asyncio
async def test_opted_in_search_routes_through_pinned_transport() -> None:
    search, download = _RecordingTransport("search"), _RecordingTransport("download")
    session = SessionManager(search, download)
    pool, made = _pinned_pool()
    pins = SourcePinnedProxies(pool)
    try:
        await _opted_ctx(session, pins, opted=True).get_json("https://x/api")
        # The pinned per-proxy transport served it — NOT the search/download pool.
        assert made["pin"].calls == 1
        assert search.calls == 0
        assert download.calls == 0
        # The pin is stable (acquired once, kept) for the source.
        assert pins.current("mangadot") is not None
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_non_opted_search_routes_through_existing_transport_byte_for_byte() -> (
    None
):
    search, download = _RecordingTransport("search"), _RecordingTransport("download")
    session = SessionManager(search, download)
    pool, made = _pinned_pool()
    pins = SourcePinnedProxies(pool)
    try:
        # A non-opted source threads the singleton but the flag is False ⇒ no pin set ⇒
        # the existing search transport serves it, exactly as today.
        await _opted_ctx(session, pins, opted=False).get_json("https://x/api")
        assert search.calls == 1
        assert "pin" not in made  # the pinned transport was never even built
        assert pins.current("mangadot") is None  # nothing acquired/pinned
    finally:
        await pool.aclose()
