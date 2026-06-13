"""Regression: the shared transport enforces a TOTAL per-request wall-clock
deadline so a slow-trickling / tarpitting upstream cannot defeat httpx's
PER-CHUNK ``read`` timeout and pin a pooled connection forever.

Root cause of debug ``all-sources-pooltimeout`` (2026-06-13): comix.to's
Cloudflare edge dribbled bytes to flagged requests; httpx resets ``read`` on
every byte, so those requests never completed and never released their slot in
the ONE shared 100-connection pool. ~95 stuck connections drained the pool and
EVERY source then failed with ``PoolTimeout`` until a restart. The fix wraps the
single outbound choke point in ``asyncio.timeout(_TOTAL_REQUEST_DEADLINE)``;
on expiry the request is cancelled (releasing the connection) and surfaced as a
retryable ``httpx.ReadTimeout``.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from manga_gateway.config import Settings
from manga_gateway.framework import transport as transport_mod
from manga_gateway.framework.transport import HttpxTransport

TEST_API_KEY = "k" * 32

# Trickle one byte well inside the per-chunk read timeout but the request still
# never completes — exactly the tarpit that defeated the old config.
_TRICKLE_INTERVAL = 0.05
_DECLARED_LEN = 1_000_000


async def _tarpit_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:
        pass
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % _DECLARED_LEN)
    try:
        await writer.drain()
        while True:
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(_TRICKLE_INTERVAL)
    except Exception:
        pass
    finally:
        writer.close()


def _pool_conn_count(t: HttpxTransport) -> int:
    return len(t._client._transport._pool.connections)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_total_deadline_kills_tarpit_and_releases_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shrink the total deadline so the test is fast; it stays far below the
    # 30s per-chunk read timeout, so a fire here proves it's the TOTAL deadline,
    # not ``read``.
    monkeypatch.setattr(transport_mod, "_TOTAL_REQUEST_DEADLINE", 0.4)

    server = await asyncio.start_server(_tarpit_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/"

    transport = HttpxTransport(Settings(api_key=TEST_API_KEY))  # type: ignore[arg-type]
    try:
        async with server:
            await server.start_serving()

            start = time.monotonic()
            with pytest.raises(httpx.ReadTimeout) as excinfo:
                await transport.request("GET", url)
            elapsed = time.monotonic() - start

            # Fired on the TOTAL deadline (~0.4s), nowhere near the 30s read timeout.
            assert elapsed < 5.0
            assert "deadline" in str(excinfo.value)
            # Retryable: a TransportError so _is_retryable / tenacity treat it as a
            # transient timeout, not a permanent leak.
            assert isinstance(excinfo.value, httpx.TransportError)

            # The cancelled request released its connection — the pool is NOT
            # permanently consumed (the whole point of the fix).
            await asyncio.sleep(0.1)
            assert _pool_conn_count(transport) == 0
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_deadline_does_not_disturb_a_fast_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_mod, "_TOTAL_REQUEST_DEADLINE", 5.0)

    async def _ok_handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except Exception:
            pass
        body = b'{"ok":true}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_ok_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/"

    transport = HttpxTransport(Settings(api_key=TEST_API_KEY))  # type: ignore[arg-type]
    try:
        async with server:
            await server.start_serving()
            resp = await transport.request("GET", url)
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
    finally:
        await transport.aclose()
