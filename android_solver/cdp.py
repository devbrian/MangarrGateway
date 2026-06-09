"""CDP extraction of the Cloudflare clearance token + WebView User-Agent.

After the Turnstile checkbox is hardware-tapped (device.py), the cleared cookie
lives in the WebView's cookie jar. This module reads it over the Chrome DevTools
Protocol page websocket that ``AdbDevice.forward_devtools`` exposes:

  * ``extract_clearance`` opens the CDP page websocket with
    ``suppress_origin=True`` (Chrome 111+ REJECTS a localhost Origin header on the
    devtools socket — proven in debug ``mangadot-cf-linux-fingerprint``, EXP27),
    issues ``Network.getAllCookies``, and returns the cleared token VALUE scoped
    to the challenge host (cross-site cookies excluded).
  * ``webview_user_agent`` reads the ``User-Agent`` from CDP ``GET /json/version``
    (the WebView UA must travel with the token to the gateway's httpx leg).

SECURITY (threat T-10-01, mirrors antibot.py T-04-12): the clearance token VALUE
is NEVER logged or printed — only a redacted "captured clearance" event is
emitted. The grep gate in 10-01-PLAN enforces this discipline.

R1: this module imports ONLY the approved third-party dep (``websocket-client``,
lazily) and the stdlib — never anything from ``src/manga_gateway``.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Protocol

_log = logging.getLogger("android_solver.cdp")

# CDP / Network cookie name for the Cloudflare clearance token.
_CLEARANCE_COOKIE = "cf_clearance"

_DEFAULT_WS_TIMEOUT = 15.0
_DEFAULT_HTTP_TIMEOUT = 10.0


class WebSocketLike(Protocol):
    """Minimal CDP page-websocket surface (so tests inject a MOCK socket)."""

    def send(self, payload: str) -> None: ...
    def recv(self) -> str | bytes: ...
    def close(self) -> None: ...


class WebSocketFactory(Protocol):
    def __call__(self, url: str, *, timeout: float) -> WebSocketLike: ...


class HttpGetter(Protocol):
    def __call__(self, url: str, *, timeout: float) -> bytes: ...


def _default_ws_factory(url: str, *, timeout: float) -> WebSocketLike:
    """Open a real CDP page websocket via websocket-client (lazy import)."""
    from websocket import create_connection  # type: ignore[import-untyped]

    # Chrome 111+ rejects a localhost Origin on the devtools socket → suppress it.
    return create_connection(url, timeout=timeout, suppress_origin=True)


def _default_http_get(url: str, *, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — devtools http URL is sidecar-built
        return resp.read()


def _belongs_to_host(cookie: dict[str, Any], host: str) -> bool:
    """True if ``cookie`` is bound to ``host`` (or a parent domain thereof).

    Mirrors ``antibot.py::_belongs_to_host`` (copied, NOT imported — R1). CDP
    cookie dicts carry ``domain`` with or without a leading dot; both denote
    subdomain-inclusive binding. Cross-site cookies are excluded.
    """
    if not host:
        return False
    raw = cookie.get("domain") or ""
    if not raw:
        return False
    domain = raw.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


def _recv_result(ws: WebSocketLike, command_id: int) -> dict[str, Any]:
    """Read CDP frames until the response matching ``command_id`` arrives."""
    while True:
        raw = ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        message = json.loads(raw)
        if message.get("id") == command_id:
            return message.get("result", {})


def cdp_call(
    ws: WebSocketLike,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    command_id: int,
) -> dict[str, Any]:
    """Send one CDP command over ``ws`` and return its ``result`` payload.

    Shared by ``extract_clearance`` and ``turnstile.locate_checkbox`` so the
    request/response framing lives in one place.
    """
    request: dict[str, Any] = {"id": command_id, "method": method}
    if params:
        request["params"] = params
    ws.send(json.dumps(request))
    return _recv_result(ws, command_id)


def extract_clearance(
    devtools_ws_url: str,
    host: str,
    *,
    ws_factory: WebSocketFactory | None = None,
    timeout: float = _DEFAULT_WS_TIMEOUT,
    command_id: int = 1,
) -> str | None:
    """Return the cleared cookie value scoped to ``host``, or ``None`` if absent.

    Opens the CDP page websocket (``suppress_origin=True``), issues
    ``Network.getAllCookies``, and selects the host-scoped token. The token
    value is returned to the caller but never logged here.
    """
    factory = ws_factory or _default_ws_factory
    ws = factory(devtools_ws_url, timeout=timeout)
    try:
        result = cdp_call(ws, "Network.getAllCookies", command_id=command_id)
    finally:
        ws.close()

    cookies = result.get("cookies", [])
    value = _select_host_clearance(cookies, host)
    if value is not None:
        # Redacted event ONLY — the token value never reaches the logs (T-10-01).
        _log.info("captured clearance for host %s", host)
    else:
        _log.warning("no host-scoped clearance cookie found for host %s", host)
    return value


def _select_host_clearance(cookies: list[dict[str, Any]], host: str) -> str | None:
    for cookie in cookies:
        if cookie.get("name") == _CLEARANCE_COOKIE and _belongs_to_host(cookie, host):
            return cookie.get("value")
    return None


def webview_user_agent(
    devtools_http_url: str,
    *,
    http_get: HttpGetter | None = None,
    timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> str | None:
    """Read the WebView ``User-Agent`` from CDP ``GET /json/version``."""
    getter = http_get or _default_http_get
    payload = getter(devtools_http_url, timeout=timeout)
    data = json.loads(payload)
    return data.get("User-Agent")
