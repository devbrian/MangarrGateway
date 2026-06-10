"""Offline unit tests for the gateway-side :class:`AndroidSolver` (Plan 10-03).

All deterministic — the android-solver sidecar ``POST /solve`` is respx-mocked, no
redroid, no adb, no real network. The tests pin the sidecar HTTP CONTRACT
(``{"challenge_url": ...}`` request → ``{cf_clearance, user_agent, host}`` response)
the solver consumes, the BOT-01 non-android-key → ``None`` exclusion, the D-35
force-resolve hold-and-re-solve, the SEC-01 ``X-Solver-Key`` auth header, and the
boot-disabled warm contract when the sidecar URL is unconfigured.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from manga_gateway.framework.android_solver import AndroidSolver
from manga_gateway.framework.antibot import AntiBotSolver, Clearance

_SIDECAR = "http://android-solver:8080"
_CHALLENGE_URLS = {
    "mangadot": "https://mangadot.net/",
    "kagane": "https://kagane.to/",
}
_WEBVIEW_UA = (
    "Mozilla/5.0 (Linux; Android 11; redroid11_x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36"
)
# Plan 11-05 / Req 7: the build_proxy() playwright dict shape threaded into /solve.
# Obviously-fake local-only values — never a real proxy credential.
_PROXY = {
    "server": "http://proxy.invalid:8080",
    "username": "fakeuser",
    "password": "NOTAREALSECRET-zzz9",
}


def _solver(**over: object) -> AndroidSolver:
    kwargs: dict[str, object] = {
        "base_url": _SIDECAR,
        "api_key": SecretStr("sidecar-secret"),
        "challenge_urls": dict(_CHALLENGE_URLS),
        "timeout_s": 5.0,
    }
    kwargs.update(over)
    return AndroidSolver(**kwargs)  # type: ignore[arg-type]


def _solve_response(host: str = "mangadot.net") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "cf_clearance": "android-minted-token",
            "user_agent": _WEBVIEW_UA,
            "host": host,
        },
    )


def test_satisfies_antibot_protocol() -> None:
    assert isinstance(_solver(), AntiBotSolver)


def test_force_resolve_is_declared_keyword() -> None:
    """D-41: ``force_resolve`` must be a real keyword param so context.py's
    ``inspect.signature`` detection passes it through on the D-35 retry."""
    params = inspect.signature(_solver().get_clearance).parameters
    assert "force_resolve" in params
    assert params["force_resolve"].kind is inspect.Parameter.KEYWORD_ONLY


@respx.mock
@pytest.mark.asyncio
async def test_get_clearance_returns_clearance() -> None:
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        clr = await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert isinstance(clr, Clearance)
    assert clr.cookies == {"cf_clearance": "android-minted-token"}
    assert clr.user_agent == _WEBVIEW_UA
    # The sidecar contract: POST /solve with the source's challenge_url.
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["X-Solver-Key"] == "sidecar-secret"


@respx.mock
@pytest.mark.asyncio
async def test_solve_body_carries_proxy_when_configured() -> None:
    """Req 7: a configured proxy rides the /solve body verbatim alongside
    challenge_url, so the sidecar's CF-solve egress matches the gateway's
    httpx-fetch egress for the same clearance."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver(proxy=dict(_PROXY))
    try:
        await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body["challenge_url"] == _CHALLENGE_URLS["mangadot"]
    assert body["proxy"] == _PROXY


@respx.mock
@pytest.mark.asyncio
async def test_solve_body_omits_proxy_when_unconfigured() -> None:
    """D-08: proxy=None ⇒ the body has ONLY challenge_url (today's body
    byte-for-byte), no ``proxy`` key."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver(proxy=None)
    try:
        await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body == {"challenge_url": _CHALLENGE_URLS["mangadot"]}
    assert "proxy" not in body


@respx.mock
@pytest.mark.asyncio
async def test_solve_does_not_log_proxy_or_token(caplog: pytest.LogCaptureFixture) -> None:
    """T-11-02 / T-10-04: neither the proxy password nor the cf_clearance token
    appears in any log record."""
    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver(proxy=dict(_PROXY))
    with caplog.at_level("DEBUG", logger="manga_gateway"):
        try:
            await solver.get_clearance("mangadot")
        finally:
            await solver.aclose()
    for record in caplog.records:
        msg = record.getMessage()
        assert _PROXY["password"] not in msg
        assert "android-minted-token" not in msg


@respx.mock
@pytest.mark.asyncio
async def test_non_android_key_returns_none_without_call() -> None:
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        assert await solver.get_clearance("mangadex") is None
    finally:
        await solver.aclose()
    assert not route.called  # BOT-01: never touches the sidecar


@respx.mock
@pytest.mark.asyncio
async def test_non_force_reuses_held_clearance() -> None:
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        first = await solver.get_clearance("mangadot")
        second = await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert first is second  # held value reused (D-35 hold)
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_force_resolve_reposts_and_replaces_held() -> None:
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        await solver.get_clearance("mangadot")
        await solver.get_clearance("mangadot", force_resolve=True)
    finally:
        await solver.aclose()
    # D-35: force_resolve ignores the held clearance and re-POSTs a FRESH solve.
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_force_resolve_failure_does_not_keep_stale_clearance() -> None:
    """WR-05: a FAILED force-resolve must invalidate the held clearance so the
    next non-force call re-solves instead of re-serving the known-bad token."""
    route = respx.post(f"{_SIDECAR}/solve").mock(
        side_effect=[
            _solve_response(),  # 1: initial solve → held
            httpx.Response(504),  # 2: force-resolve self-heal FAILS
            _solve_response(),  # 3: next non-force call must RE-SOLVE (not serve stale)
        ]
    )
    solver = _solver()
    try:
        first = await solver.get_clearance("mangadot")
        with pytest.raises(httpx.HTTPStatusError):
            await solver.get_clearance("mangadot", force_resolve=True)
        # If the stale clearance were still held, this would return it WITHOUT a
        # network call (call_count would stay 2). It must instead re-solve.
        third = await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert isinstance(first, Clearance)
    assert isinstance(third, Clearance)
    assert route.call_count == 3  # the failed force-resolve invalidated the hold


@respx.mock
@pytest.mark.asyncio
async def test_sidecar_non_200_raises() -> None:
    respx.post(f"{_SIDECAR}/solve").mock(return_value=httpx.Response(504))
    solver = _solver()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()


@pytest.mark.asyncio
async def test_unconfigured_url_warm_reports_all_android_keys_failed() -> None:
    """An AndroidSolver with no sidecar URL boots every android source disabled —
    gate/CI/local-without-redroid stays green (no sidecar to call)."""
    solver = _solver(base_url=None)
    try:
        failed = await solver.warm()
    finally:
        await solver.aclose()
    assert set(failed) == set(_CHALLENGE_URLS)


@respx.mock
@pytest.mark.asyncio
async def test_warm_eager_solves_each_android_key() -> None:
    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        failed = await solver.warm()
        # both keys solved → none failed, and both are now held
        assert failed == []
        held = await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert isinstance(held, Clearance)
