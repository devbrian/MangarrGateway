"""Regression: a request CANCELLED mid-response by an OUTER scope must release its
pooled connection — never leak the slot (debug pooltimeout-recurrence, angle C).

Live root cause: the search fan-out's per-source ``asyncio.timeout(30s)`` fires
BEFORE the transport's own 60s inner deadline (and the engine's per-page TaskGroup
cancels in-flight siblings), so the in-flight request is cancelled by an OUTER scope
mid-response. The pre-fix buffered ``client.request()`` gave httpcore 1.0.9 no chance
to return the connection on that cancellation unwind: the OS socket stayed ESTABLISHED
(idle, no keepalive timer) and its LOGICAL pool slot leaked. Enough leaked slots
exhausted ``max_connections`` so every acquisition blocked ``pool=10s`` and raised
``PoolTimeout``. Observed live: 503 idle no-timer ESTABLISHED sockets, 445 to 4 CF
hosts, against a 100 keepalive cap.

The fix routes every request through ``send(stream=True)`` + ``aread()`` with a
guaranteed ``finally: await response.aclose()``, so the connection is released on
EVERY exit path — success, error, AND cancellation.
"""

from __future__ import annotations

import asyncio

import pytest

from manga_gateway.config import Settings
from manga_gateway.framework.transport import HttpxTransport

TEST_API_KEY = "k" * 32
_DECLARED_LEN = 10_000_000  # large body so the read phase is long enough to cancel in
_TRICKLE_INTERVAL = 0.02


async def _tarpit_body_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Send headers immediately, then dribble the body forever — so a request is
    firmly in the RESPONSE-READ phase (connection acquired, response started) when
    an outer cancel arrives. This is the window the leak lives in."""
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:
        return
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % _DECLARED_LEN)
    try:
        await writer.drain()
        while True:
            writer.write(b"x" * 64)
            await writer.drain()
            await asyncio.sleep(_TRICKLE_INTERVAL)
    except Exception:
        pass
    finally:
        with __import__("contextlib").suppress(Exception):
            writer.close()


def _pool_conn_count(t: HttpxTransport) -> int:
    return len(t._client._transport._pool.connections)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_outer_cancel_during_response_read_releases_connection() -> None:
    server = await asyncio.start_server(_tarpit_body_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/"

    transport = HttpxTransport(Settings(api_key=TEST_API_KEY))  # type: ignore[arg-type]
    try:
        async with server:
            await server.start_serving()

            # Cancel from an OUTER scope while the body is still streaming in — the
            # fan-out-timeout / engine-TaskGroup cancellation shape that leaked live.
            with pytest.raises((asyncio.CancelledError, TimeoutError)):
                async with asyncio.timeout(0.3):
                    await transport.request("GET", url)

            # Give the unwind a beat, then assert the pool released the connection.
            await asyncio.sleep(0.2)
            leaked = _pool_conn_count(transport)
            assert leaked == 0, (
                f"connection leak: {leaked} pooled connection(s) still pinned after an "
                "outer cancellation mid-response (the live PoolTimeout cause)"
            )
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_inner_deadline_during_response_read_releases_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport's OWN deadline firing mid-read also releases the connection and
    surfaces a retryable ReadTimeout (the df08b38 guarantee, kept under streaming)."""
    import httpx

    from manga_gateway.framework import transport as transport_mod

    monkeypatch.setattr(transport_mod, "_TOTAL_REQUEST_DEADLINE", 0.3)

    server = await asyncio.start_server(_tarpit_body_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/"

    transport = HttpxTransport(Settings(api_key=TEST_API_KEY))  # type: ignore[arg-type]
    try:
        async with server:
            await server.start_serving()
            with pytest.raises(httpx.ReadTimeout):
                await transport.request("GET", url)
            await asyncio.sleep(0.2)
            assert _pool_conn_count(transport) == 0
    finally:
        await transport.aclose()
