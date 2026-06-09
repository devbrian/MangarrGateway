"""extract_clearance / webview_user_agent driven against a MOCKED CDP socket.

No real WebView, adb, or websocket is touched. Asserts host-scoped cookie
selection (cross-site cookies excluded) and UA parsing.
"""

from __future__ import annotations

import json

from android_solver.cdp import extract_clearance, webview_user_agent


class FakeCdpWs:
    """Mock CDP page websocket: replays a per-method result for each command id."""

    def __init__(self, results: dict[str, dict]) -> None:
        self._results = results
        self._pending: list[dict] = []
        self.closed = False

    def send(self, payload: str) -> None:
        msg = json.loads(payload)
        self._pending.append(
            {"id": msg["id"], "result": self._results.get(msg["method"], {})}
        )

    def recv(self) -> str:
        return json.dumps(self._pending.pop(0))

    def close(self) -> None:
        self.closed = True


def _factory(ws: FakeCdpWs):
    def make(url: str, *, timeout: float) -> FakeCdpWs:
        assert url  # sidecar-built devtools ws url
        return ws

    return make


def test_extract_clearance_returns_only_challenge_host_token() -> None:
    cookies = [
        {"name": "cf_clearance", "value": "OTHER_HOST_TOKEN", "domain": ".example.com"},
        {"name": "session", "value": "noise", "domain": ".mangadot.net"},
        {"name": "cf_clearance", "value": "MANGADOT_TOKEN", "domain": ".mangadot.net"},
    ]
    ws = FakeCdpWs({"Network.getAllCookies": {"cookies": cookies}})

    value = extract_clearance(
        "ws://localhost:9222/devtools/page/ABC",
        "mangadot.net",
        ws_factory=_factory(ws),
    )

    assert value == "MANGADOT_TOKEN"
    assert ws.closed is True


def test_extract_clearance_excludes_cross_site_cookie() -> None:
    cookies = [
        {"name": "cf_clearance", "value": "OTHER_HOST_TOKEN", "domain": ".example.com"},
    ]
    ws = FakeCdpWs({"Network.getAllCookies": {"cookies": cookies}})

    value = extract_clearance(
        "ws://localhost:9222/devtools/page/ABC",
        "mangadot.net",
        ws_factory=_factory(ws),
    )

    assert value is None


def test_extract_clearance_matches_subdomain_binding() -> None:
    cookies = [
        {"name": "cf_clearance", "value": "KAGANE_TOKEN", "domain": "kagane.to"},
    ]
    ws = FakeCdpWs({"Network.getAllCookies": {"cookies": cookies}})

    value = extract_clearance(
        "ws://localhost:9222/devtools/page/XYZ",
        "www.kagane.to",
        ws_factory=_factory(ws),
    )

    assert value == "KAGANE_TOKEN"


def test_webview_user_agent_parses_json_version() -> None:
    ua = "Mozilla/5.0 (Linux; Android 11; redroid11_x86_64) Chrome/148 Mobile wv"
    payload = json.dumps({"Browser": "WebView", "User-Agent": ua}).encode()

    def fake_get(url: str, *, timeout: float) -> bytes:
        assert url.endswith("/json/version")
        return payload

    result = webview_user_agent("http://localhost:9222/json/version", http_get=fake_get)

    assert result == ua
    assert result is not None and result.endswith(" wv")
