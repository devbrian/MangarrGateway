"""Unit tests for the framework session-prep seam + the POST/form + CSRF-403 path.

Two surfaces are exercised here (Plan 07-01):

* ``framework/session_prep.py`` — the httpx twin of the browser clearance seam
  (``SessionCredentials`` dataclass, ``SessionPrep`` Protocol, ``NoSessionPrep``
  default, ``CsrfBootstrap`` impl). It GETs an HTML page, parses
  ``meta[name="csrf-token"]`` and harvests ``PHPSESSID``, caches them, and
  re-GETs on ``force_refresh=True``. The token/cookie value is NEVER logged.
* ``framework/context.py`` — the new ``post_json`` (form-urlencoded POST through
  the ONE shared transport), the ``is_csrf_failure`` predicate, the per-request
  clearance+CSRF merge (one ``Cookie`` header, one ``X-CSRF-Token``), and the
  CSRF-403 refresh-once-and-retry branch that sits BEFORE the permanent-4xx STOP.

No network, no browser: a fake ``Transport`` records what the context emits and
returns queued responses; respx mocks the bootstrap HTML GET.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from manga_gateway.framework.context import SourceContext, is_csrf_failure
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.framework.session_prep import (
    CsrfBootstrap,
    NoSessionPrep,
    SessionCredentials,
    SessionPrep,
)
from manga_gateway.handles.store import HandleStore

_TOKEN = "a" * 64  # a 64-hex meta csrf-token
_TOKEN2 = "b" * 64
_HTML_URL = "https://mangaball.net/"
_API_URL = "https://mangaball.net/api/v1/title/search-advanced/"


def _html(token: str) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="csrf-token" content="{token}">'
        "</head><body>ok</body></html>"
    )


# ─────────────────────────── fake transport ───────────────────────────


class _RecordingTransport:
    """Fake Transport recording requests + serving a queued response sequence.

    ``html_token`` (when set) makes any GET return the bootstrap HTML carrying
    that token + a ``Set-Cookie: PHPSESSID`` header. POSTs pop from ``responses``.
    """

    def __init__(
        self,
        responses: list[httpx.Response] | None = None,
        *,
        html_token: str | None = None,
        session_cookie: str = "sess-1",
    ) -> None:
        self._responses = responses or []
        self._html_token = html_token
        self._session_cookie = session_cookie
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.html_gets = 0

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, url, kwargs))
        req = httpx.Request(method, url)
        if method == "GET" and self._html_token is not None:
            self.html_gets += 1
            return httpx.Response(
                200,
                text=_html(self._html_token),
                headers={"Set-Cookie": f"PHPSESSID={self._session_cookie}; Path=/"},
                request=req,
            )
        resp = self._responses.pop(0)
        # Ensure raise_for_status has a request bound (queued responses built
        # without an explicit request= still work through the real machinery).
        if resp._request is None:  # noqa: SLF001 — test fixture binding
            resp.request = req
        return resp

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _ctx(
    transport: _RecordingTransport,
    *,
    session_prep: SessionPrep | None = None,
) -> SourceContext:
    return SourceContext(
        source_key="mangaball",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        session_prep=session_prep,
    )


# ═══════════════════════════ Task 1: session_prep.py ═══════════════════════════


@pytest.mark.asyncio
async def test_csrf_bootstrap_captures_token_and_phpsessid() -> None:
    transport = _RecordingTransport(html_token=_TOKEN, session_cookie="abc123")
    prep = CsrfBootstrap(
        keys={"mangaball"},
        session=SessionManager(transport),  # type: ignore[arg-type]
        bootstrap_urls={"mangaball": _HTML_URL},
    )
    creds = await prep.prepare("mangaball")
    assert isinstance(creds, SessionCredentials)
    assert creds.csrf_token == _TOKEN
    assert creds.cookies.get("PHPSESSID") == "abc123"


@pytest.mark.asyncio
async def test_csrf_bootstrap_caches_until_force_refresh() -> None:
    transport = _RecordingTransport(html_token=_TOKEN, session_cookie="abc123")
    prep = CsrfBootstrap(
        keys={"mangaball"},
        session=SessionManager(transport),  # type: ignore[arg-type]
        bootstrap_urls={"mangaball": _HTML_URL},
    )
    first = await prep.prepare("mangaball")
    assert transport.html_gets == 1
    cached = await prep.prepare("mangaball")
    assert transport.html_gets == 1  # served from cache, no second GET
    assert cached is first or cached == first

    # force_refresh re-GETs the HTML page (D-03/D-05).
    transport._html_token = _TOKEN2
    refreshed = await prep.prepare("mangaball", force_refresh=True)
    assert transport.html_gets == 2
    assert refreshed.csrf_token == _TOKEN2


@pytest.mark.asyncio
async def test_csrf_bootstrap_returns_none_for_unconfigured_key() -> None:
    transport = _RecordingTransport(html_token=_TOKEN)
    prep = CsrfBootstrap(
        keys={"mangaball"},
        session=SessionManager(transport),  # type: ignore[arg-type]
        bootstrap_urls={"mangaball": _HTML_URL},
    )
    assert await prep.prepare("mangadex") is None
    assert transport.html_gets == 0  # no HTML GET for an unconfigured source


@pytest.mark.asyncio
async def test_no_session_prep_returns_none_for_every_key() -> None:
    prep = NoSessionPrep()
    assert await prep.prepare("mangaball") is None
    assert await prep.prepare("anything") is None


@pytest.mark.asyncio
async def test_csrf_bootstrap_never_logs_token_or_cookie(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _RecordingTransport(html_token=_TOKEN, session_cookie="secret-sess")
    prep = CsrfBootstrap(
        keys={"mangaball"},
        session=SessionManager(transport),  # type: ignore[arg-type]
        bootstrap_urls={"mangaball": _HTML_URL},
    )
    with caplog.at_level(logging.DEBUG, logger="manga_gateway"):
        await prep.prepare("mangaball")
        await prep.prepare("mangaball", force_refresh=True)
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert _TOKEN not in blob
    assert "secret-sess" not in blob


def test_session_prep_protocol_is_runtime_checkable() -> None:
    assert isinstance(NoSessionPrep(), SessionPrep)


# ═══════════════════════════ Task 2: context.py POST + CSRF ═══════════════════


def test_is_csrf_failure_predicate() -> None:
    req = httpx.Request("POST", _API_URL)
    marker = httpx.Response(
        403, json={"error": "CSRF token validation failed"}, request=req
    )
    assert is_csrf_failure(marker) is True
    # A plain 403 (no marker) is NOT a CSRF failure → terminal STOP.
    assert is_csrf_failure(httpx.Response(403, text="forbidden", request=req)) is False
    # A 200 carrying the words is not a 403 → not a CSRF failure.
    ok_200 = httpx.Response(
        200, json={"error": "CSRF token validation failed"}, request=req
    )
    assert is_csrf_failure(ok_200) is False


def test_is_csrf_failure_broadened_marker_tolerant() -> None:
    """WR-04: the marker match is case-insensitive and tolerant of casing /
    escaping / localized wording (lowercased ``csrf`` + ``token``/``validation``)."""
    req = httpx.Request("POST", _API_URL)
    # Differently-cased / re-worded.
    assert is_csrf_failure(
        httpx.Response(403, text="The CSRF Token is invalid", request=req)
    )
    # JSON-escaped with extra whitespace.
    assert is_csrf_failure(
        httpx.Response(403, json={"detail": "csrf  validation error"}, request=req)
    )
    # A 403 mentioning csrf but with NEITHER token nor validation does not match
    # (avoids over-matching unrelated bodies).
    assert not is_csrf_failure(
        httpx.Response(403, text="see our csrf docs", request=req)
    )


class _CredsPrep:
    """Minimal SessionPrep returning fixed credentials (no HTML GET)."""

    def __init__(self, token: str = _TOKEN, cookie: str = "sess-1") -> None:
        self._token = token
        self._cookie = cookie
        self.prepare_calls: list[bool] = []

    async def prepare(
        self, source_key: str, *, force_refresh: bool = False
    ) -> SessionCredentials | None:
        self.prepare_calls.append(force_refresh)
        if force_refresh:
            self._token = self._token[:-1] + "F"  # rotate so a retry differs
        return SessionCredentials(
            cookies={"PHPSESSID": self._cookie}, csrf_token=self._token
        )


@pytest.mark.asyncio
async def test_post_json_sends_form_urlencoded_with_csrf_and_cookie() -> None:
    transport = _RecordingTransport(
        [httpx.Response(200, json={"code": 200, "data": []})]
    )
    ctx = _ctx(transport, session_prep=_CredsPrep(cookie="abc"))
    result = await ctx.post_json(_API_URL, data={"search_input": "one piece"})
    assert result == {"code": 200, "data": []}

    method, url, kwargs = transport.requests[-1]
    assert method == "POST"
    assert url == _API_URL
    # httpx form-encodes data=dict → application/x-www-form-urlencoded.
    assert kwargs["data"] == {"search_input": "one piece"}
    headers = kwargs["headers"]
    assert headers["X-CSRF-Token"] == _TOKEN
    assert "PHPSESSID=abc" in headers["Cookie"]


@pytest.mark.asyncio
async def test_post_json_refreshes_once_on_csrf_403_then_retries() -> None:
    req = httpx.Request("POST", _API_URL)
    transport = _RecordingTransport(
        [
            httpx.Response(
                403, json={"error": "CSRF token validation failed"}, request=req
            ),
            httpx.Response(
                200, json={"code": 200, "data": [{"ok": True}]}, request=req
            ),
        ]
    )
    prep = _CredsPrep()
    ctx = _ctx(transport, session_prep=prep)
    result = await ctx.post_json(_API_URL, data={"search_input": "x"})
    assert result == {"code": 200, "data": [{"ok": True}]}
    # Exactly two POSTs: the 403 then the retry after refresh.
    posts = [r for r in transport.requests if r[0] == "POST"]
    assert len(posts) == 2
    # Exactly one forced refresh.
    assert prep.prepare_calls.count(True) == 1


@pytest.mark.asyncio
async def test_plain_403_does_not_refresh_or_retry() -> None:
    from manga_gateway.framework.errors import SourceError

    req = httpx.Request("POST", _API_URL)
    transport = _RecordingTransport([httpx.Response(403, text="nope", request=req)])
    prep = _CredsPrep()
    ctx = _ctx(transport, session_prep=prep)
    with pytest.raises(SourceError) as ei:
        await ctx.post_json(_API_URL, data={"q": "x"})
    assert ei.value.status == 403
    posts = [r for r in transport.requests if r[0] == "POST"]
    assert len(posts) == 1  # no retry on a non-CSRF 403
    assert prep.prepare_calls.count(True) == 0  # no forced refresh


@pytest.mark.asyncio
async def test_session_prep_none_contributes_no_headers() -> None:
    # MangaDex path byte-for-byte unchanged: no Cookie / X-CSRF-Token injected.
    transport = _RecordingTransport(
        [httpx.Response(200, json={"code": 200, "data": []})]
    )
    ctx = _ctx(transport, session_prep=None)
    await ctx.post_json(_API_URL, data={"q": "x"})
    _, _, kwargs = transport.requests[-1]
    headers = kwargs.get("headers", {})
    assert "X-CSRF-Token" not in headers
    assert "Cookie" not in headers


@pytest.mark.asyncio
async def test_get_json_still_works_unchanged() -> None:
    transport = _RecordingTransport([httpx.Response(200, json={"data": "ok"})])
    ctx = _ctx(transport, session_prep=None)
    result = await ctx.get_json("https://x/api")
    assert result == {"data": "ok"}
    method, _, _ = transport.requests[-1]
    assert method == "GET"


@pytest.mark.asyncio
async def test_post_form_body_round_trips_as_urlencoded() -> None:
    # Prove A2: httpx encodes data=dict as application/x-www-form-urlencoded.
    captured: dict[str, Any] = {}

    class _EncodingTransport:
        async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            built = httpx.Request(method, url, **kwargs)
            captured["content_type"] = built.headers.get("content-type")
            captured["body"] = built.content
            return httpx.Response(200, json={"code": 200}, request=built)

        async def aclose(self) -> None:  # pragma: no cover
            pass

    ctx = SourceContext(
        source_key="mangaball",
        rate_limit_per_minute=6000,
        session=SessionManager(_EncodingTransport()),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        session_prep=None,
    )
    await ctx.post_json(_API_URL, data={"search_input": "one piece", "page": "1"})
    assert "application/x-www-form-urlencoded" in (captured["content_type"] or "")
    parsed = parse_qs(captured["body"].decode())
    assert parsed["search_input"] == ["one piece"]
    assert parsed["page"] == ["1"]
