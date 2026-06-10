"""Stdlib-only authenticated self-probe: learn the expected egress IP (D-05/D-06).

Before the sidecar trusts a solve, it must know the egress IP the WebView SHOULD
present — learned through the SAME authenticated residential upstream the app
traffic uses, so a CDP egress read can be compared against it (D-03 happens in
``service.py``; this module owns the expected value).

``expected_egress`` opens a CONNECT tunnel to the neutral echo host
``api.ipify.org`` (D-04) through the upstream and reads back the egress IP. It uses
``http.client.HTTPSConnection.set_tunnel(host, port, headers={...})`` so the
``Proxy-Authorization`` header is injected explicitly on the CONNECT — NOT
``urllib.request.ProxyHandler``, which does not reliably add that header on the
HTTPS CONNECT tunnel across CPython versions (Pitfall 5). Stdlib only: NO httpx
(D-06). The IP is normalized via ``ipaddress.ip_address`` (NOT a regex) so junk /
HTML error pages raise instead of slipping through.

FAIL-LOUD discipline (D-05 / T-11-01): a non-200 status, a connect/timeout error,
or a non-IP body is a self-probe FAILURE — it RAISES :class:`EgressProbeError`,
never returns a sentinel/pass. A transient echo failure is a solve failure, not a
green light.

SECURITY (T-11-02): the upstream ``pw`` and the base64 ``Proxy-Authorization``
token are NEVER logged.

R1: this module imports ONLY the stdlib — never anything from ``src/manga_gateway``.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import logging
import ssl
from collections.abc import Mapping
from typing import Protocol, cast

_log = logging.getLogger("android_solver.egress")

_ECHO_HOST = "api.ipify.org"
_ECHO_PORT = 443
_DEFAULT_TIMEOUT = 15.0


class EgressProbeError(RuntimeError):
    """Raised when the authenticated self-probe cannot confirm the egress IP."""


class _ResponseLike(Protocol):
    status: int

    def read(self) -> bytes: ...


class _ConnectionLike(Protocol):
    """Minimal CONNECT-tunnel surface (so tests inject a fake transport)."""

    def set_tunnel(
        self,
        host: str,
        port: int | None = ...,
        headers: Mapping[str, str] | None = ...,
    ) -> None: ...

    def request(self, method: str, url: str, *, headers: Mapping[str, str]) -> None: ...

    def getresponse(self) -> _ResponseLike: ...

    def close(self) -> None: ...


class ConnectionFactory(Protocol):
    def __call__(self, host: str, port: int, timeout: float) -> _ConnectionLike: ...


def _default_connection(host: str, port: int, timeout: float) -> _ConnectionLike:
    """Open a real HTTPS CONNECT-capable connection to the upstream."""
    return cast(
        _ConnectionLike,
        http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ssl.create_default_context()
        ),
    )


def _proxy_auth_header(user: str, pw: str) -> str:
    """Return ``Basic <b64(user:pass)>`` for the upstream CONNECT (never logged)."""
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return "Basic " + token


def _parse_ip(body: str) -> str:
    """Normalize an echo body to a canonical IPv4/IPv6 string (raises on junk)."""
    return str(ipaddress.ip_address(body.strip()))


def expected_egress(
    up_host: str,
    up_port: int,
    user: str | None,
    pw: str | None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    connection_factory: ConnectionFactory | None = None,
) -> str:
    """Return the egress IP seen through the authenticated upstream, or RAISE.

    Opens a CONNECT tunnel to ``api.ipify.org`` (D-04) through
    ``up_host:up_port``, injecting ``Proxy-Authorization`` when ``user``/``pw`` are
    present (omitted when unauthenticated), GETs ``/``, and returns the normalized
    egress IP. Any non-200 status, connect/timeout error, or non-IP body RAISES
    :class:`EgressProbeError` — never a pass (D-05 / T-11-01).
    """
    factory = connection_factory or _default_connection

    headers: dict[str, str] = {}
    if user and pw:
        headers["Proxy-Authorization"] = _proxy_auth_header(user, pw)

    conn = factory(up_host, up_port, timeout)
    try:
        conn.set_tunnel(_ECHO_HOST, _ECHO_PORT, headers=headers)
        conn.request("GET", "/", headers={"Host": _ECHO_HOST})
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
    except (OSError, http.client.HTTPException) as exc:
        # Transient transport failure ⇒ solve failure, NOT a pass (D-05).
        raise EgressProbeError(f"self-probe connect failed: {exc}") from exc
    finally:
        conn.close()

    if status != 200:
        raise EgressProbeError(f"self-probe egress check returned HTTP {status}")

    body = raw.decode("utf-8", "replace")
    try:
        ip = _parse_ip(body)
    except ValueError as exc:
        # A non-IP body (e.g. an HTML challenge / error page) is a FAILURE.
        raise EgressProbeError("self-probe returned a non-IP body") from exc

    _log.info("self-probe learned expected egress via authenticated upstream")
    return ip
