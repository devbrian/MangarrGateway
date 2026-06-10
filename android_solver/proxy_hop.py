"""Persistent pproxy CONNECT hop that injects ``Proxy-Authorization`` upstream.

Android's ``global http_proxy`` is host:port-only — it cannot carry the Basic
proxy-auth our authenticated residential upstream requires. This module runs a
small, long-lived ``pproxy`` server INSIDE the android-solver sidecar that:

  * binds its PROXY listener on ``0.0.0.0:<hop_port>`` so the separate redroid
    container can reach it (Pitfall 1 — ``127.0.0.1`` would be redroid's own
    loopback, unreachable cross-container); and
  * accepts repoint commands on a SEPARATE CONTROL listener bound
    ``127.0.0.1`` (sidecar-internal ONLY — never cross-container).

Per solve, the parent repoints the upstream by sending one control command that
MUTATES the shared ``rserver`` list IN PLACE — never rebinding the name. pproxy's
``stream_handler`` captures the ``rserver`` list object by reference and re-reads
it (``schedule(rserver, …) or DIRECT``) on every connection, so an in-place
``rserver[:] = […]`` / ``rserver.clear()`` is seen by live handlers while a
``rserver = …`` rebind would leave them holding the stale object (Pitfall 2,
D-02). The upstream URI ``http://HOST:PORT#USER:PASS`` makes pproxy inject
``Proxy-Authorization: Basic b64(user:pass)`` on the upstream CONNECT (verified
in ``pproxy/proto.py``; D-01 / Req 4).

SECURITY:
  * T-11-05 — the proxy listener carries no auth and is docker-internal (no
    published host port; only redroid + the sidecar share it). The CONTROL
    channel binds loopback so the per-solve repoint (which carries the upstream
    creds) never crosses a container boundary. adb (5555) and the CDP devtools
    socket are NEVER routed through the hop — only app HTTP egress is.
  * T-11-02 — the upstream URI / ``user:pass`` is NEVER logged; only redacted,
    non-secret events are emitted.

R1: this module imports ONLY the approved sidecar dep (``pproxy``, lazily) and the
stdlib — never anything from ``src/manga_gateway``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from typing import Protocol

_log = logging.getLogger("android_solver.proxy_hop")

_DEFAULT_CONTROL_HOST = "127.0.0.1"
_DEFAULT_REPOINT_TIMEOUT = 5.0
_ACK_OK = "OK"
_CMD_SET = "SET"
_CMD_IDLE = "IDLE"


class ProxyHopError(RuntimeError):
    """Raised when a control-channel repoint command does not ack ``OK``."""


class ServerFactory(Protocol):
    """Builds a pproxy upstream-server object from a ``#user:pass`` URI."""

    def __call__(self, uri: str) -> object: ...


def _make_server(uri: str) -> object:
    """Build a ``pproxy.Server`` upstream from ``http://HOST:PORT#USER:PASS``.

    pproxy parses the ``#user:pass`` fragment into ``.users`` and injects
    ``Proxy-Authorization: Basic b64(user:pass)`` on the upstream CONNECT
    (``pproxy/proto.py``). Imported lazily so merely importing this module (e.g.
    for the gate's pytest collection / mypy, where the sidecar-only ``pproxy`` pin
    is absent from the venv) never fails — mirrors ``cdp.py``'s lazy ``websocket``.
    """
    import pproxy

    return pproxy.Server(uri)


def apply_command(
    line: str,
    rserver: list[object],
    *,
    server_factory: ServerFactory | None = None,
) -> str:
    """Apply one control command to ``rserver`` IN PLACE and return the ack.

    ``SET <uri>`` repoints the upstream (``rserver[:] = [server_factory(uri)]``);
    ``IDLE`` resets to DIRECT (``rserver.clear()``). The list object IDENTITY is
    always preserved — only its CONTENTS change — so pproxy's live handlers see
    the repoint (Pitfall 2 / D-02). The upstream URI is never logged (T-11-02).
    """
    factory = server_factory or _make_server
    command = line.strip()
    if command == _CMD_IDLE:
        rserver.clear()
        _log.info("hop repointed to DIRECT (idle)")
        return _ACK_OK
    if command.startswith(_CMD_SET + " "):
        uri = command[len(_CMD_SET) :].strip()
        if not uri:
            return "ERR empty upstream uri"
        # IN-PLACE mutation only — NEVER ``rserver = …`` (Pitfall 2).
        rserver[:] = [factory(uri)]
        # T-11-02: the credential-bearing URI is intentionally omitted from logs.
        _log.info("hop repointed to a new authenticated upstream")
        return _ACK_OK
    return "ERR unknown command"


async def _handle_control(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    rserver: list[object],
    server_factory: ServerFactory,
) -> None:
    """One control-connection: read a command line, ack ``OK``/``ERR …``."""
    try:
        raw = await reader.readline()
        line = raw.decode("utf-8", "replace")
        ack = apply_command(line, rserver, server_factory=server_factory)
        writer.write((ack + "\n").encode())
        await writer.drain()
    finally:
        writer.close()


async def serve(
    hop_port: int,
    *,
    control_host: str = _DEFAULT_CONTROL_HOST,
    control_port: int | None = None,
    server_factory: ServerFactory | None = None,
) -> None:
    """Run the persistent hop forever: 0.0.0.0 proxy + 127.0.0.1 control.

    The PROXY listener binds ``0.0.0.0:<hop_port>`` (cross-container, Pitfall 1);
    the CONTROL listener binds ``control_host`` (loopback, sidecar-internal only).
    ``rserver`` starts empty (⇒ DIRECT) and is mutated in place per repoint.
    """
    import pproxy

    if control_port is None:
        control_port = hop_port + 1
    factory = server_factory or _make_server

    rserver: list[object] = []
    listener = pproxy.Server(f"http://0.0.0.0:{hop_port}")
    await listener.start_server(dict(rserver=rserver))

    control = await asyncio.start_server(
        lambda r, w: _handle_control(r, w, rserver, factory),
        control_host,
        control_port,
    )
    _log.info(
        "proxy hop up: proxy 0.0.0.0:%d (cross-container), control %s:%d (loopback)",
        hop_port,
        control_host,
        control_port,
    )
    async with control:
        await asyncio.Event().wait()


def repoint(
    host: str,
    port: int,
    upstream: str | None,
    *,
    timeout: float = _DEFAULT_REPOINT_TIMEOUT,
) -> None:
    """Send one repoint over the 127.0.0.1 control channel and await the ack.

    ``upstream`` is the full ``http://HOST:PORT#USER:PASS`` URI (→ ``SET``), or
    ``None`` to reset the hop to DIRECT (→ ``IDLE``). This is the synchronous
    helper Plan 04's control thread calls under its ``Lock``. The upstream URI is
    never logged here (T-11-02). Raises :class:`ProxyHopError` on a non-``OK`` ack.
    """
    command = _CMD_IDLE if upstream is None else f"{_CMD_SET} {upstream}"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((command + "\n").encode())
        reader = sock.makefile("r")
        ack = reader.readline().strip()
    if ack != _ACK_OK:
        raise ProxyHopError(f"hop repoint failed (ack {ack!r})")


def main(argv: list[str] | None = None) -> None:
    """``python -m android_solver.proxy_hop --port <hop_port>`` entry point."""
    parser = argparse.ArgumentParser(
        description="android-solver auth-injecting CONNECT hop"
    )
    parser.add_argument(
        "--port", type=int, required=True, help="0.0.0.0 proxy listener port"
    )
    parser.add_argument(
        "--control-host",
        default=_DEFAULT_CONTROL_HOST,
        help="loopback control listener host (default 127.0.0.1)",
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=None,
        help="loopback control listener port (default: --port + 1)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        serve(
            args.port,
            control_host=args.control_host,
            control_port=args.control_port,
        )
    )


if __name__ == "__main__":
    main()
