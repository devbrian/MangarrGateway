"""expected_egress driven against a MOCKED CONNECT transport (no network).

Asserts the authenticated-CONNECT self-probe normalizes a valid IP, hard-fails
(raises, never passes) on a non-200 status / non-IP body / connect error, and
injects ``Proxy-Authorization`` only when creds are supplied. Stdlib only.
"""

from __future__ import annotations

import base64
import logging
import pathlib

import pytest
from android_solver.egress import EgressProbeError, expected_egress

_EGRESS_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "android_solver" / "egress.py"
)


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeConnection:
    """Records the tunnel headers; replays a canned response (or raises)."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"203.0.113.7",
        raise_on_request: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.raise_on_request = raise_on_request
        self.tunnel_host: str | None = None
        self.tunnel_port: int | None = None
        self.tunnel_headers: dict[str, str] = {}
        self.closed = False

    def set_tunnel(
        self, host: str, port: int | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.tunnel_host = host
        self.tunnel_port = port
        self.tunnel_headers = dict(headers or {})

    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None:
        if self.raise_on_request is not None:
            raise self.raise_on_request

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.status, self.body)

    def close(self) -> None:
        self.closed = True


def _factory(conn: _FakeConnection):
    def make(host: str, port: int, timeout: float) -> _FakeConnection:
        assert host and port  # upstream host:port supplied
        return conn

    return make


def test_returns_normalized_ip_on_200() -> None:
    conn = _FakeConnection(status=200, body=b"  203.0.113.7\n")
    result = expected_egress(
        "up.example.com", 8080, "user", "secret", connection_factory=_factory(conn)
    )
    assert result == "203.0.113.7"
    assert conn.tunnel_host == "api.ipify.org"
    assert conn.tunnel_port == 443
    assert conn.closed is True


def test_normalizes_ipv6() -> None:
    conn = _FakeConnection(body=b"2001:0db8:0000:0000:0000:0000:0000:0001")
    result = expected_egress(
        "up.example.com", 8080, "user", "secret", connection_factory=_factory(conn)
    )
    assert result == "2001:db8::1"


def test_non_ip_body_raises() -> None:
    conn = _FakeConnection(status=200, body=b"<html>access denied</html>")
    with pytest.raises(EgressProbeError):
        expected_egress(
            "up.example.com", 8080, "user", "secret", connection_factory=_factory(conn)
        )
    assert conn.closed is True


def test_non_200_status_raises() -> None:
    conn = _FakeConnection(status=407, body=b"203.0.113.7")
    with pytest.raises(EgressProbeError):
        expected_egress(
            "up.example.com", 8080, "user", "secret", connection_factory=_factory(conn)
        )


def test_connect_error_raises_not_passes() -> None:
    conn = _FakeConnection(raise_on_request=OSError("connection reset"))
    with pytest.raises(EgressProbeError):
        expected_egress(
            "up.example.com", 8080, "user", "secret", connection_factory=_factory(conn)
        )
    assert conn.closed is True


def test_proxy_authorization_header_present_when_creds_supplied() -> None:
    conn = _FakeConnection()
    expected_egress(
        "up.example.com", 8080, "alice", "wonderland", connection_factory=_factory(conn)
    )
    expected = "Basic " + base64.b64encode(b"alice:wonderland").decode()
    assert conn.tunnel_headers.get("Proxy-Authorization") == expected


def test_proxy_authorization_header_absent_when_unauthenticated() -> None:
    conn = _FakeConnection()
    expected_egress(
        "up.example.com", 8080, None, None, connection_factory=_factory(conn)
    )
    assert "Proxy-Authorization" not in conn.tunnel_headers


def test_password_and_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    conn = _FakeConnection()
    with caplog.at_level(logging.DEBUG, logger="android_solver.egress"):
        expected_egress(
            "up.example.com",
            8080,
            "alice",
            "wonderland",
            connection_factory=_factory(conn),
        )
    blob = " ".join(record.getMessage() for record in caplog.records)
    token = base64.b64encode(b"alice:wonderland").decode()
    assert "wonderland" not in blob
    assert token not in blob


def test_source_is_stdlib_only_and_uses_set_tunnel() -> None:
    src = _EGRESS_SRC.read_text()
    assert "set_tunnel" in src  # explicit CONNECT header injection (Pitfall 5)
    assert "import httpx" not in src  # stdlib only (D-06)
    assert "import urllib" not in src  # not urllib.ProxyHandler (Pitfall 5)
    assert "ProxyHandler(" not in src  # never invoked
