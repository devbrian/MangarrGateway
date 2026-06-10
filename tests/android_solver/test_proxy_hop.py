"""Offline tests for the auth-injecting CONNECT hop's control protocol.

The load-bearing proofs (in-place ``rserver`` mutation with list IDENTITY
preserved — Pitfall 2 / D-02 — and the loopback control channel) run WITHOUT a
real ``pproxy`` or any network, mirroring ``test_cdp.py``'s injected-fake style.
The single end-to-end ``serve()`` smoke is guarded by ``importorskip`` so the
gate venv (where the sidecar-only ``pproxy`` pin is absent) skips it.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib

import pytest
from android_solver.proxy_hop import (
    ProxyHopError,
    _handle_control,
    apply_command,
    control_endpoint,
    repoint,
    serve,
)

_HOP_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "android_solver" / "proxy_hop.py"
)


class _SentinelFactory:
    """Fake ``ServerFactory``: records URIs, returns a deterministic sentinel."""

    def __init__(self) -> None:
        self.uris: list[str] = []
        self.sentinel = object()

    def __call__(self, uri: str) -> object:
        self.uris.append(uri)
        return self.sentinel


def test_control_endpoint_is_loopback_and_distinct_from_proxy_port() -> None:
    """The repoint target is 127.0.0.1:hop_port+1 — NEVER the cross-container port.

    The live regression: the sidecar repointed over (hop_host, hop_port) — the
    pproxy PROXY listener — which silently closes a SET line (empty ack ⇒
    ProxyHopError), blocking every proxied solve. control_endpoint() is the single
    source of truth that keeps serve()'s control listener and the parent's repoint
    target in lock-step on the LOOPBACK control channel.
    """
    host, port = control_endpoint(18081)
    assert host == "127.0.0.1"  # loopback, sidecar-internal only (T-11-05)
    assert port == 18082  # hop_port + 1
    # Must differ from the (cross-container) proxy endpoint the device routes to.
    assert (host, port) != ("android-solver", 18081)
    assert port != 18081


def test_apply_set_mutates_rserver_in_place() -> None:
    factory = _SentinelFactory()
    rserver: list[object] = []
    original_id = id(rserver)

    ack = apply_command(
        "SET http://up.example.com:8080#user:secret",
        rserver,
        server_factory=factory,
    )

    assert ack == "OK"
    # IDENTITY preserved, CONTENTS changed (Pitfall 2 / D-02).
    assert id(rserver) == original_id
    assert rserver == [factory.sentinel]
    assert factory.uris == ["http://up.example.com:8080#user:secret"]


def test_apply_idle_clears_rserver_in_place() -> None:
    factory = _SentinelFactory()
    rserver: list[object] = [object(), object()]
    original_id = id(rserver)

    ack = apply_command("IDLE", rserver, server_factory=factory)

    assert ack == "OK"
    assert id(rserver) == original_id
    assert rserver == []


def test_apply_unknown_and_empty_commands_return_err() -> None:
    rserver: list[object] = []
    assert apply_command(
        "BOGUS", rserver, server_factory=_SentinelFactory()
    ).startswith("ERR")
    assert apply_command(
        "SET   ", rserver, server_factory=_SentinelFactory()
    ).startswith("ERR")
    assert rserver == []


def test_set_command_never_logs_credentials(caplog: pytest.LogCaptureFixture) -> None:
    factory = _SentinelFactory()
    with caplog.at_level(logging.DEBUG, logger="android_solver.proxy_hop"):
        apply_command(
            "SET http://up.example.com:8080#user:supersecret",
            [],
            server_factory=factory,
        )
    blob = " ".join(record.getMessage() for record in caplog.records)
    assert "supersecret" not in blob
    assert "user:supersecret" not in blob
    assert "up.example.com" not in blob


async def test_control_channel_roundtrip_preserves_list_identity() -> None:
    """repoint() over a 127.0.0.1 control listener mutates the SAME list in place."""
    factory = _SentinelFactory()
    rserver: list[object] = []
    original_id = id(rserver)

    server = await asyncio.start_server(
        lambda r, w: _handle_control(r, w, rserver, factory),
        "127.0.0.1",
        0,
    )
    host, port = server.sockets[0].getsockname()[:2]
    assert host == "127.0.0.1"  # control channel is loopback only (T-11-05)

    async with server:
        await asyncio.to_thread(
            repoint, "127.0.0.1", port, "http://up.example.com:8080#user:secret"
        )
        assert id(rserver) == original_id
        assert rserver == [factory.sentinel]

        await asyncio.to_thread(repoint, "127.0.0.1", port, None)
        assert id(rserver) == original_id
        assert rserver == []


async def test_repoint_raises_on_non_ok_ack() -> None:
    rserver: list[object] = []
    server = await asyncio.start_server(
        lambda r, w: _handle_control(r, w, rserver, _SentinelFactory()),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    async with server:
        with pytest.raises(ProxyHopError):
            # An empty upstream -> "SET " -> ERR ack; repoint() must raise.
            await asyncio.to_thread(repoint, "127.0.0.1", port, "")


def test_source_binds_proxy_0000_and_control_loopback() -> None:
    """Source assertion: proxy listener is 0.0.0.0, control default is loopback."""
    src = _HOP_SRC.read_text()
    assert (
        "http://0.0.0.0:{hop_port}" in src
    )  # proxy listener cross-container (Pitfall 1)
    assert '_DEFAULT_CONTROL_HOST = "127.0.0.1"' in src  # control loopback-only
    # In-place mutation grep gate (D-02 / Pitfall 2).
    assert "rserver[:]" in src
    assert "rserver.clear()" in src


@pytest.mark.parametrize("idle", [False, True])
async def test_serve_end_to_end_control_acks(idle: bool) -> None:
    """End-to-end: real ``pproxy`` hop, control channel acks OK. Skipped sans pproxy."""
    pytest.importorskip("pproxy")

    # Grab two distinct free ports (proxy + control).
    import socket as _socket

    def _free_port() -> int:
        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    hop_port, control_port = _free_port(), _free_port()
    factory = _SentinelFactory()

    task = asyncio.create_task(
        serve(
            hop_port,
            control_host="127.0.0.1",
            control_port=control_port,
            server_factory=factory,
        )
    )
    try:
        # Wait for the control listener to come up.
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", control_port
                )
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            break
        else:  # pragma: no cover - startup never completed
            pytest.fail("control listener did not come up")

        upstream = None if idle else "http://up.example.com:8080#user:secret"
        await asyncio.to_thread(repoint, "127.0.0.1", control_port, upstream)
        # SET records the URI through the SAME factory bound into serve().
        if idle:
            assert factory.uris == []
        else:
            assert factory.uris == [upstream]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
