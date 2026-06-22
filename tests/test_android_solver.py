"""Offline unit tests for the gateway-side :class:`AndroidSolver` (Plan 10-03).

All deterministic — the android-solver sidecar ``POST /solve`` is respx-mocked, no
redroid, no adb, no real network. The tests pin the sidecar HTTP CONTRACT
(``{"challenge_url": ...}`` request → ``{cf_clearance, user_agent, host}`` response)
the solver consumes, the BOT-01 non-android-key → ``None`` exclusion, the D-35
force-resolve hold-and-re-solve, the SEC-01 ``X-Solver-Key`` auth header, and the
boot-disabled warm contract when the sidecar URL is unconfigured.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import httpx
import pytest
import respx
from pydantic import SecretStr

from manga_gateway.framework.android_solver import _WEBVIEW_PRIME_JS, AndroidSolver
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


# Default epoch expiry the sidecar mints (the precise-refresh contract). Far-future so
# real-``time.time()`` math never treats a default-minted clearance as already expired.
_DEFAULT_EXPIRES = 2000000000.0
# Sentinel: pass ``expires=_ABSENT`` to omit ``cf_clearance_expires`` from the response
# body (an OLDER sidecar). Distinct from ``None`` (present-but-session/unknown).
_ABSENT: object = object()


def _solve_response(
    host: str = "mangadot.net",
    *,
    token: str = "android-minted-token",
    expires: object = _DEFAULT_EXPIRES,
) -> httpx.Response:
    body: dict[str, object] = {
        "cf_clearance": token,
        "user_agent": _WEBVIEW_UA,
        "host": host,
    }
    if expires is not _ABSENT:
        body["cf_clearance_expires"] = expires
    return httpx.Response(200, json=body)


def _eval_clearance_response(
    *,
    value: object = True,
    token: str = "comix-eval-minted-token",
    user_agent: str = _WEBVIEW_UA,
    expires: object = _DEFAULT_EXPIRES,
    egress_ip: str = "",
    clearance: object = _ABSENT,
) -> httpx.Response:
    """A sidecar ``/eval`` ``extract_clearance`` response (Bug 5 follow-on #3).

    ``{"value": ..., "clearance": {cf_clearance, user_agent, cf_clearance_expires,
    egress_ip}}`` — the shape the gateway parses to mint+hold a page-holder's clearance
    via the EVAL path (never ``/solve``). Pass ``clearance=None`` to model a clean clear
    that deposited NO host-scoped cookie (the sidecar returns ``"clearance": null``)."""
    block: object
    if clearance is _ABSENT:
        block = {
            "cf_clearance": token,
            "user_agent": user_agent,
            "cf_clearance_expires": expires,
            "egress_ip": egress_ip,
        }
    else:
        block = clearance
    return httpx.Response(200, json={"value": value, "clearance": block})


class _FakeCollector:
    """Records ``emit_solve`` calls so a test can assert the android solve is now a
    labeled metric event (the gap-mystery fix). Other ``emit_*`` are no-ops."""

    def __init__(self) -> None:
        self.solves: list[dict[str, object]] = []

    def emit_solve(self, **kwargs: object) -> None:
        self.solves.append(kwargs)

    def __getattr__(self, _name: str):  # type: ignore[no-untyped-def]
        return lambda *a, **k: None


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
async def test_solve_does_not_log_proxy_or_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
async def test_warm_skips_on_demand_keys() -> None:
    """On-demand keys are NEVER eager-warmed (debug pooltimeout-recurrence): warm()
    skips them, so the sidecar is not hit for an absent challenge and the key is NOT
    reported failed (→ never force-disabled). The non-on-demand key still warms."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver(on_demand_keys=frozenset({"mangadot"}))
    try:
        failed = await solver.warm()
    finally:
        await solver.aclose()
    # mangadot skipped → not failed; kagane warmed once.
    assert "mangadot" not in failed
    assert route.call_count == 1  # only kagane hit the sidecar
    body = json.loads(route.calls.last.request.content)
    assert body["challenge_url"] == _CHALLENGE_URLS["kagane"]


def test_solve_if_missing_is_declared_keyword() -> None:
    """On-demand peek (debug pooltimeout-recurrence): ``solve_if_missing`` must be a
    real keyword param so context.py / SolverRouter ``inspect.signature`` detection
    forwards it (D-41)."""
    params = inspect.signature(_solver().get_clearance).parameters
    assert "solve_if_missing" in params
    assert params["solve_if_missing"].kind is inspect.Parameter.KEYWORD_ONLY


@respx.mock
@pytest.mark.asyncio
async def test_solve_if_missing_false_peeks_without_solving() -> None:
    """On-demand peek with NOTHING held → ``None`` and NO sidecar solve. This is what
    stops an intermittent-challenge source from blocking on a clearance the site is
    not currently demanding (debug pooltimeout-recurrence)."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        result = await solver.get_clearance("mangadot", solve_if_missing=False)
    finally:
        await solver.aclose()
    assert result is None
    assert not route.called  # peeked the empty cache — never touched the sidecar


@respx.mock
@pytest.mark.asyncio
async def test_solve_if_missing_false_serves_held_clearance() -> None:
    """On-demand peek WITH a held clearance → serve it (no re-solve). The first real
    solve (or a D-35 force-resolve) populates ``_held``; subsequent peeks reuse it."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        seeded = await solver.get_clearance("mangadot")  # one real solve seeds _held
        peeked = await solver.get_clearance("mangadot", solve_if_missing=False)
    finally:
        await solver.aclose()
    assert peeked is seeded  # served the held value
    assert route.call_count == 1  # the peek added NO sidecar call


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


# ─────────────────────────── eval_in_webview (EVAL-02) ──────────────────────────
# The off-Protocol in-WebView eval client (Plan 14-02): POST /eval, returns
# ``payload["value"]``; same X-Solver-Key + raise_for_status contract as /solve.

_EVAL_URL = "https://comix.to/"


@respx.mock
@pytest.mark.asyncio
async def test_eval_in_webview_posts_key_and_body_and_returns_value() -> None:
    """The sidecar /eval contract: POST {challenge_url, js, wait_for?} with the
    X-Solver-Key header → return the response's ``value`` (Plan 14-01)."""
    payload = {"value": [{"hid": "abc", "title": "Ch 1"}]}
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(200, json=payload)
    )
    solver = _solver()
    try:
        result = await solver.eval_in_webview(
            _EVAL_URL, "return await c.list({})", wait_for="() => !!window.__c"
        )
    finally:
        await solver.aclose()
    assert result == payload["value"]
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["X-Solver-Key"] == "sidecar-secret"
    body = json.loads(sent.content)
    assert body == {
        "challenge_url": _EVAL_URL,
        "js": "return await c.list({})",
        "wait_for": "() => !!window.__c",
    }


@respx.mock
@pytest.mark.asyncio
async def test_eval_in_webview_omits_wait_for_when_none() -> None:
    """``wait_for`` rides the body ONLY when given (mirrors _solve's proxy gate)."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(200, json={"value": 7})
    )
    solver = _solver()
    try:
        result = await solver.eval_in_webview(_EVAL_URL, "return 7")
    finally:
        await solver.aclose()
    assert result == 7
    body = json.loads(route.calls.last.request.content)
    assert body == {"challenge_url": _EVAL_URL, "js": "return 7"}
    assert "wait_for" not in body


@respx.mock
@pytest.mark.asyncio
async def test_eval_in_webview_body_carries_proxy_when_configured() -> None:
    """Req 7 parity with test_solve_body_carries_proxy_when_configured: a configured
    proxy rides the /eval body verbatim so the eval navigation egresses the SAME
    residential IP the proxied clearance was minted on."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(200, json={"value": 1})
    )
    solver = _solver(proxy=dict(_PROXY))
    try:
        await solver.eval_in_webview(_EVAL_URL, "return 1")
    finally:
        await solver.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body["challenge_url"] == _EVAL_URL
    assert body["js"] == "return 1"
    assert body["proxy"] == _PROXY


@respx.mock
@pytest.mark.asyncio
async def test_eval_in_webview_body_omits_proxy_when_unconfigured() -> None:
    """D-08: proxy=None ⇒ the /eval body has NO ``proxy`` key (today's body
    byte-for-byte)."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(200, json={"value": 1})
    )
    solver = _solver(proxy=None)
    try:
        await solver.eval_in_webview(_EVAL_URL, "return 1")
    finally:
        await solver.aclose()
    body = json.loads(route.calls.last.request.content)
    assert "proxy" not in body


@respx.mock
@pytest.mark.asyncio
async def test_eval_in_webview_non_200_raises() -> None:
    """Same failure contract as _solve: a non-200 /eval raises (raise_for_status)."""
    respx.post(f"{_SIDECAR}/eval").mock(return_value=httpx.Response(504))
    solver = _solver()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.eval_in_webview(_EVAL_URL, "return 1")
    finally:
        await solver.aclose()


@pytest.mark.asyncio
async def test_eval_in_webview_unconfigured_raises() -> None:
    """D-33 / T-14-07: an unconfigured sidecar URL makes eval raise RuntimeError
    (fails loud — never a silent empty result that looks like an empty chapter
    list); the gate / CI / a local box without redroid stays green."""
    solver = _solver(base_url=None)
    try:
        with pytest.raises(RuntimeError):
            await solver.eval_in_webview(_EVAL_URL, "return 1")
    finally:
        await solver.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_eval_in_webview_does_not_log_js_or_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-14-04: neither the gateway-authored ``js`` nor the eval result appears in
    any log record (mirrors test_solve_does_not_log_proxy_or_token)."""
    secret_js = "return await __SECRET_TOKEN_MINT__()"
    secret_result = "SUPER-SECRET-EVAL-RESULT-zzz9"
    respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(200, json={"value": secret_result})
    )
    solver = _solver()
    with caplog.at_level("DEBUG", logger="manga_gateway"):
        try:
            out = await solver.eval_in_webview(_EVAL_URL, secret_js)
        finally:
            await solver.aclose()
    assert out == secret_result
    for record in caplog.records:
        msg = record.getMessage()
        assert secret_js not in msg
        assert secret_result not in msg


# ── shared-device 503 "solver busy" backpressure is retried (bounded) ─────────────
# The single redroid is shared; the sidecar rejects a concurrent op with 503 (its
# non-blocking lock). The gateway's _device_op serializes only ITS calls, so a 503 from
# another caller's in-flight op is retried (bounded), not surfaced as upstream-503.


@respx.mock
@pytest.mark.asyncio
async def test_eval_retries_sidecar_503_busy_then_succeeds() -> None:
    """Two ``503 solver busy`` then a 200 → the eval RETRIES and returns the value (not
    an upstream-503 failure). 503 is the shared-device backpressure signal, not fail."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        side_effect=[
            httpx.Response(503, json={"error": "solver busy"}),
            httpx.Response(503, json={"error": "solver busy"}),
            httpx.Response(200, json={"value": 42}),
        ]
    )
    solver = _solver()
    solver._busy_retry_backoff_s = 0.0  # no real sleep in the test
    try:
        result = await solver.eval_in_webview(_EVAL_URL, "return 42")
    finally:
        await solver.aclose()
    assert result == 42
    assert route.call_count == 3


@respx.mock
@pytest.mark.asyncio
async def test_eval_does_not_retry_non_503_failure() -> None:
    """A 504 (a REAL eval failure, not busy backpressure) raises immediately — the
    retry is scoped strictly to 503 so a genuine failure is never masked/delayed."""
    route = respx.post(f"{_SIDECAR}/eval").mock(return_value=httpx.Response(504))
    solver = _solver()
    solver._busy_retry_backoff_s = 0.0
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.eval_in_webview(_EVAL_URL, "return 1")
    finally:
        await solver.aclose()
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_eval_busy_retry_exhausts_then_raises() -> None:
    """A device that stays busy past the retry budget surfaces the LAST 503 (fails loud
    — a genuinely wedged shared device is never masked forever)."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(503, json={"error": "solver busy"})
    )
    solver = _solver()
    solver._busy_retry_attempts = 3
    solver._busy_retry_backoff_s = 0.0
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.eval_in_webview(_EVAL_URL, "return 1")
    finally:
        await solver.aclose()
    assert route.call_count == 3


# ── Mode-E follow-up: a TRANSIENT comix-origin 5xx eval-throw is retried ONCE ─────────
# comix's in-page env-module axios (c.list / chapters) occasionally hits comix's own
# flaky origin → Cloudflare 521/520/522. The sidecar surfaces that as a 504 with body
# {"error": "eval threw", "detail": "...Request failed with status code 5xx..."}. A
# single bounded retry self-recovers a one-off origin blip WITHIN the search (so a
# single-shot caller like external_links[comix] does not return 0). A clean empty (200
# []), a 4xx throw, a non-origin throw, or a timeout is NEVER retried.


def _eval_threw(status_code: int) -> httpx.Response:
    """A sidecar ``504 eval threw`` carrying the axios origin-status summary."""
    return httpx.Response(
        504,
        json={
            "error": "eval threw",
            "detail": (
                f"in-page eval threw: AxiosError: Request failed with "
                f"status code {status_code}"
            ),
        },
    )


@respx.mock
@pytest.mark.asyncio
async def test_eval_retries_origin_5xx_throw_then_recovers() -> None:
    """A comix-origin 521 eval-throw then a 200 → the eval RETRIES once and returns the
    recovered items (the transient origin blip self-heals within the single search)."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        side_effect=[
            _eval_threw(521),
            httpx.Response(200, json={"value": [{"hid": "abc", "title": "Recovered"}]}),
        ]
    )
    solver = _solver()
    solver._eval_origin_5xx_retry_backoff_s = 0.0  # no real sleep in the test
    try:
        result = await solver.eval_in_webview(_EVAL_URL, "return await c.list({})")
    finally:
        await solver.aclose()
    assert result == [{"hid": "abc", "title": "Recovered"}]
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_eval_does_not_retry_clean_empty_result() -> None:
    """A CLEAN empty (a 200 ``[]`` — a genuine no-match, NOT a throw) returns 0 with NO
    retry (the negative-cache handles a genuine empty; a wasted eval is avoided)."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    solver = _solver()
    solver._eval_origin_5xx_retry_backoff_s = 0.0
    try:
        result = await solver.eval_in_webview(_EVAL_URL, "return await c.list({})")
    finally:
        await solver.aclose()
    assert result == []
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_eval_does_not_retry_4xx_throw() -> None:
    """A 4xx in-page throw (e.g. a 404 — NOT a transient origin blip) raises immediately
    with NO retry: the retry is scoped strictly to the 5xx origin band."""
    route = respx.post(f"{_SIDECAR}/eval").mock(return_value=_eval_threw(404))
    solver = _solver()
    solver._eval_origin_5xx_retry_backoff_s = 0.0
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.eval_in_webview(_EVAL_URL, "return await c.list({})")
    finally:
        await solver.aclose()
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_eval_does_not_retry_non_origin_throw() -> None:
    """A throw with NO HTTP status code in the summary (a CF/hydration/extractor error,
    not an origin 5xx) raises immediately — only an identifiable 5xx blip retries."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(
            504,
            json={
                "error": "eval threw",
                "detail": "in-page eval threw: TypeError: env module not importable",
            },
        )
    )
    solver = _solver()
    solver._eval_origin_5xx_retry_backoff_s = 0.0
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.eval_in_webview(_EVAL_URL, "return await c.list({})")
    finally:
        await solver.aclose()
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_eval_persistent_origin_5xx_exhausts_retry_then_raises() -> None:
    """A PERSISTENT origin 5xx exhausts the single bounded retry then raises (no
    infinite loop) — comix surfaces a per-source warning; the fan-out is never stuck."""
    route = respx.post(f"{_SIDECAR}/eval").mock(return_value=_eval_threw(521))
    solver = _solver()
    solver._eval_origin_5xx_retry_backoff_s = 0.0
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.eval_in_webview(_EVAL_URL, "return await c.list({})")
    finally:
        await solver.aclose()
    # 1 initial + 1 bounded retry = 2 device evals, then it fails loud.
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_eval_timeout_504_is_not_retried_as_origin_5xx() -> None:
    """A sidecar ``504 eval timed out`` (a HANG, distinct from an origin throw) is NOT
    treated as a retryable origin 5xx — it raises on the first failure."""
    route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=httpx.Response(504, json={"error": "eval timed out"})
    )
    solver = _solver()
    solver._eval_origin_5xx_retry_backoff_s = 0.0
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.eval_in_webview(_EVAL_URL, "return await c.list({})")
    finally:
        await solver.aclose()
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_solve_retries_sidecar_503_busy_then_succeeds() -> None:
    """The ``/solve`` path retries ``503 solver busy`` too — a 503 then a 200 mints the
    clearance (a cf_clearance source is robust to shared-device contention)."""
    route = respx.post(f"{_SIDECAR}/solve").mock(
        side_effect=[
            httpx.Response(503, json={"error": "solver busy"}),
            _solve_response(),
        ]
    )
    solver = _solver()
    solver._busy_retry_backoff_s = 0.0
    try:
        clearance = await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert clearance is not None
    assert clearance.cookies == {"cf_clearance": "android-minted-token"}
    assert route.call_count == 2


# ── Bug 5 follow-on #3: page-holder get_clearance serves held-or-None, never /solve ──
# comix is antibot=cloudflare+encrypted, so the framework calls get_clearance('comix')
# on its httpx legs. comix can NEVER be /solve'd — /solve runs `pm clear`, cold-wiping
# the warm WebView comix's managed challenge + env-*.js depend on → 5xx. And (live-
# confirmed) comix has NO replayable host-scoped cf_clearance: its data path is the warm
# cleared eval page + the in-page env-module API; the sidecar logs "no host-scoped
# clearance cookie found for host comix.to" on every warm eval. So get_clearance(comix)
# serves held-or-None and NEVER mints on the request hot path; the warm prime
# opportunistically holds a cookie IF a real Turnstile clear ever deposits one.


@respx.mock
@pytest.mark.asyncio
async def test_get_clearance_page_holder_serves_held_or_none_never_solves() -> None:
    """``get_clearance`` for a page-holder key (``warm_last_keys`` — comix) with nothing
    held returns ``None`` WITHOUT any device hit (no ``/solve`` — which would cold-wipe
    the warm page — and no per-request eval). A seeded held clearance is served back."""
    solve_route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    eval_route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=_eval_clearance_response()
    )
    solver = _solver(
        challenge_urls={
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
        },
        warm_last_keys={"comix"},
    )
    try:
        # Nothing held → None, and NO device hit at all (comix needs no httpx clearance;
        # its eval data path clears itself on the warm page).
        none_clearance = await solver.get_clearance("comix")
        assert none_clearance is None
        assert not solve_route.called
        assert not eval_route.called
        # A held capture (e.g. from the warm prime) is served back, still no device hit.
        seeded = Clearance(cookies={"cf_clearance": "held-tok"}, user_agent=_WEBVIEW_UA)
        solver._held["comix"] = seeded
        assert await solver.get_clearance("comix") is seeded
        assert not solve_route.called
        assert not eval_route.called
    finally:
        await solver.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_get_clearance_clearance_only_key_still_solves() -> None:
    """Non-regression: a clearance-only android key (mangadot — NOT a page-holder)
    still mints via ``/solve``, never the eval path. The page-holder handling is gated
    strictly on ``warm_last_keys``."""
    solve_route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    eval_route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=_eval_clearance_response()
    )
    solver = _solver(
        challenge_urls={
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
        },
        warm_last_keys={"comix"},
    )
    try:
        clearance = await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert clearance is not None
    assert clearance.cookies == {"cf_clearance": "android-minted-token"}
    assert solve_route.called
    assert not eval_route.called


@respx.mock
@pytest.mark.asyncio
async def test_get_clearance_page_holder_force_resolve_captures_via_eval() -> None:
    """The D-35 403 self-heal (``force_resolve=True``) on a page-holder discards the
    stale capture and runs ONE opportunistic eval re-clear: when comix re-challenges and
    the Turnstile clear deposits a host-scoped ``cf_clearance``, it is held + returned —
    NEVER via ``/solve``."""
    solve_route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    eval_route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=_eval_clearance_response(token="reclear-token")
    )
    solver = _solver(
        challenge_urls={"comix": "https://comix.to/"},
        warm_last_keys={"comix"},
    )
    try:
        captured = await solver.get_clearance("comix", force_resolve=True)
    finally:
        await solver.aclose()
    assert captured is not None
    assert captured.cookies == {"cf_clearance": "reclear-token"}
    assert solver._held["comix"] is captured
    assert eval_route.call_count == 1
    assert not solve_route.called
    body = json.loads(eval_route.calls.last.request.content)
    assert body["js"] == _WEBVIEW_PRIME_JS
    assert body["extract_clearance"] is True
    assert "wait_for" not in body


@respx.mock
@pytest.mark.asyncio
async def test_get_clearance_page_holder_force_resolve_no_cookie_returns_none() -> None:
    """A force-resolve re-clear that extracts NO host-scoped cookie (``clearance: null``
    — the COMMON comix case) returns ``None`` and holds nothing: NOT a failure (the eval
    re-cleared the warm page; the open image CDN serves bytes with no cookie)."""
    solve_route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    eval_route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=_eval_clearance_response(clearance=None)
    )
    solver = _solver(
        challenge_urls={"comix": "https://comix.to/"},
        warm_last_keys={"comix"},
    )
    solver._held["comix"] = Clearance(
        cookies={"cf_clearance": "stale"}, user_agent=_WEBVIEW_UA
    )
    try:
        result = await solver.get_clearance("comix", force_resolve=True)
    finally:
        await solver.aclose()
    assert result is None  # NOT a raise — a missing cookie is the norm for comix
    assert "comix" not in solver._held  # the stale capture was discarded, none re-held
    assert eval_route.called
    assert not solve_route.called


# ── #296: per-source-key single-flight coalescing around _solve ───────────────


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_force_resolve_coalesces_to_one_sidecar_call() -> None:
    """#296: N concurrent ``force_resolve`` for ONE key fire EXACTLY one sidecar
    /solve and every caller receives the SAME ``Clearance`` object — the same-key
    herd is one device hit, not N (the sidecar serializes them, so N un-coalesced
    solves would cost ~N×~11s for one mint)."""
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def _gated(request: httpx.Request) -> httpx.Response:
        # Hold the single shared solve open until every caller has attached to it,
        # so the coalescing is exercised deterministically (the de-dup is structural
        # — the in-flight task is registered before the first await — but the gate
        # hardens the assertion against any scheduling surprise).
        entered.set()
        await gate.wait()
        return _solve_response()

    route = respx.post(f"{_SIDECAR}/solve").mock(side_effect=_gated)
    solver = _solver()
    try:
        tasks = [
            asyncio.create_task(solver.get_clearance("mangadot", force_resolve=True))
            for _ in range(5)
        ]
        await entered.wait()  # the one shared solve has started; all callers attached
        gate.set()
        results = await asyncio.gather(*tasks)
    finally:
        await solver.aclose()
    assert route.call_count == 1  # one device hit for the whole herd
    first = results[0]
    assert isinstance(first, Clearance)
    assert all(r is first for r in results)  # one shared Clearance object fans out


@respx.mock
@pytest.mark.asyncio
async def test_coalesced_failed_solve_propagates_and_leaves_no_hold() -> None:
    """WR-05 under coalescing: a 504 on the shared solve propagates to EVERY
    ``force_resolve`` awaiter (each raises ``httpx.HTTPStatusError``), the sidecar is
    hit ONCE, and no held entry survives for the key (the next non-force call
    re-solves rather than re-serving a known-bad token)."""
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def _gated(request: httpx.Request) -> httpx.Response:
        entered.set()
        await gate.wait()
        return httpx.Response(504)

    route = respx.post(f"{_SIDECAR}/solve").mock(side_effect=_gated)
    solver = _solver()
    try:
        tasks = [
            asyncio.create_task(solver.get_clearance("mangadot", force_resolve=True))
            for _ in range(5)
        ]
        await entered.wait()
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await solver.aclose()
    assert route.call_count == 1  # one shared (failed) solve for the herd
    assert all(isinstance(r, httpx.HTTPStatusError) for r in results)
    assert "mangadot" not in solver._held  # WR-05: nothing held after a failed solve
    assert "mangadot" not in solver._expires_at


@respx.mock
@pytest.mark.asyncio
async def test_refresh_and_reactive_do_not_share_deferrable_task() -> None:
    """A proactive refresh tick (deferrable, ``defer_if_foreground=True``) and a
    concurrent reactive ``force_resolve`` (non-deferrable) for the SAME key run as
    SEPARATE sidecar /solves — they must NOT share one single-flight task. A deferrable
    refresh task can raise ``_RefreshDeferred`` (a control-flow signal only warm/refresh
    understand); were a non-deferrable foreground caller to await that shared task it
    would receive the ``_RefreshDeferred`` and violate the get_clearance contract that
    foreground calls never defer (CodeRabbit). Deferrable solves therefore bypass the
    coalesce slot; the device lock still serializes the two /solves (no concurrent herd
    on the one device)."""
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def _gated(request: httpx.Request) -> httpx.Response:
        entered.set()
        await gate.wait()
        return _solve_response(token="fresh-token", expires=time.time() + 99999)

    route = respx.post(f"{_SIDECAR}/solve").mock(side_effect=_gated)
    solver = _solver()
    solver._refresh_lead_s = 120.0
    solver._held["mangadot"] = Clearance(
        cookies={"cf_clearance": "stale-token"}, user_agent=_WEBVIEW_UA
    )
    solver._expires_at["mangadot"] = time.time() + 30.0  # inside lead → expiring
    try:
        # Refresh first so its deferrable /solve acquires the device lock; the reactive
        # force_resolve then runs its OWN /solve, serialized behind the device lock (it
        # does NOT attach to the deferrable refresh task).
        refresh = asyncio.create_task(solver._refresh_tick())
        reactive = asyncio.create_task(
            solver.get_clearance("mangadot", force_resolve=True)
        )
        await entered.wait()
        gate.set()
        await asyncio.gather(refresh, reactive)
    finally:
        await solver.aclose()
    # Separate /solves: the deferrable refresh task is NOT shared with the
    # non-deferrable reactive caller (so _RefreshDeferred can never escape to it); the
    # device lock serializes them, so it is two sequential mints, not a concurrent herd.
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_different_keys_do_not_block_each_other() -> None:
    """#296 isolation: concurrent ``force_resolve`` for two DIFFERENT keys solve
    independently — one /solve per key (the single-flight registry is keyed by
    source, with no global lock, so one key's herd never blocks another)."""
    route = respx.post(f"{_SIDECAR}/solve").mock(
        side_effect=lambda request: _solve_response()
    )
    solver = _solver()
    try:
        a, b = await asyncio.gather(
            solver.get_clearance("mangadot", force_resolve=True),
            solver.get_clearance("kagane", force_resolve=True),
        )
    finally:
        await solver.aclose()
    assert route.call_count == 2  # one per key — different keys are independent
    assert isinstance(a, Clearance)
    assert isinstance(b, Clearance)


# ── #1: the android solve is now a labeled metric event (the gap-mystery fix) ──


@respx.mock
@pytest.mark.asyncio
async def test_solve_emits_solve_metric_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful sidecar solve emits a ``kind=solve`` event so its ~11s latency is
    visible in the per-request breakdown (previously the android solve emitted nothing
    → the gap read as a mystery)."""
    fake = _FakeCollector()
    monkeypatch.setattr("manga_gateway.metrics.collector.get_collector", lambda: fake)
    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    try:
        await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert len(fake.solves) == 1
    ev = fake.solves[0]
    assert ev["source_key"] == "mangadot"
    assert ev["outcome"] == "ok"
    assert ev["error"] is None
    assert isinstance(ev["duration_ms"], float) and ev["duration_ms"] >= 0.0


@respx.mock
@pytest.mark.asyncio
async def test_solve_emits_error_metric_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed solve still emits an ``outcome=error`` event (and re-raises) — the
    failure is observable, the token value never reaches the metric."""
    fake = _FakeCollector()
    monkeypatch.setattr("manga_gateway.metrics.collector.get_collector", lambda: fake)
    respx.post(f"{_SIDECAR}/solve").mock(return_value=httpx.Response(504))
    solver = _solver()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert len(fake.solves) == 1
    assert fake.solves[0]["outcome"] == "error"
    assert fake.solves[0]["error"] == "HTTPStatusError"


# ── #2: the sidecar cookie expiry is captured and drives proactive refresh ─────


@respx.mock
@pytest.mark.asyncio
async def test_get_clearance_records_cookie_expiry() -> None:
    """A solve that returns ``cf_clearance_expires`` tracks it for the refresh loop."""
    respx.post(f"{_SIDECAR}/solve").mock(
        return_value=_solve_response(expires=1_900_000_000.0)
    )
    solver = _solver()
    try:
        await solver.get_clearance("mangadot")
    finally:
        await solver.aclose()
    assert solver._expires_at["mangadot"] == 1_900_000_000.0


@respx.mock
@pytest.mark.asyncio
async def test_missing_or_session_expiry_is_not_tracked() -> None:
    """An OLDER sidecar (field absent) and a session cookie (expires=None / -1) both
    leave NO tracked expiry → that key is refreshed reactively only (never proactively).
    Degrades cleanly without a sidecar bump."""
    respx.post(f"{_SIDECAR}/solve").mock(
        side_effect=[
            _solve_response(expires=_ABSENT),  # older sidecar: field omitted
            _solve_response(expires=None),  # session/unknown lifetime
            _solve_response(expires=-1),  # CDP session-cookie sentinel
        ]
    )
    solver = _solver()
    try:
        for _ in range(3):
            await solver.get_clearance("mangadot", force_resolve=True)
            assert "mangadot" not in solver._expires_at
    finally:
        await solver.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_force_resolve_clears_tracked_expiry_then_resets() -> None:
    """force_resolve discards the tracked expiry alongside the held clearance (WR-05)
    before re-solving, then the fresh solve re-records it."""
    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response(expires=1.0e9))
    solver = _solver()
    try:
        await solver.get_clearance("mangadot")
        assert solver._expires_at["mangadot"] == 1.0e9
        await solver.get_clearance("mangadot", force_resolve=True)
    finally:
        await solver.aclose()
    assert solver._expires_at["mangadot"] == 1.0e9


@respx.mock
@pytest.mark.asyncio
async def test_refresh_tick_remints_expiring_key_and_swaps_atomically() -> None:
    """The proactive tick re-mints a within-lead key WITHOUT force_resolve, swapping the
    held clearance only on success (the old one keeps serving during the solve)."""
    route = respx.post(f"{_SIDECAR}/solve").mock(
        return_value=_solve_response(token="fresh-token", expires=time.time() + 99999)
    )
    solver = _solver()
    solver._refresh_lead_s = 120.0
    # Seed an about-to-expire held clearance (expiry inside the lead window).
    solver._held["mangadot"] = Clearance(
        cookies={"cf_clearance": "stale-token"}, user_agent=_WEBVIEW_UA
    )
    solver._expires_at["mangadot"] = time.time() + 30.0  # 30s < 120s lead → expiring
    try:
        await solver._refresh_tick()
    finally:
        await solver.aclose()
    assert route.call_count == 1  # re-minted
    assert solver._held["mangadot"].cookies == {"cf_clearance": "fresh-token"}
    assert solver._expires_at["mangadot"] > time.time() + 9000  # expiry advanced


@respx.mock
@pytest.mark.asyncio
async def test_refresh_tick_skips_page_holders_never_solve_or_eval() -> None:
    """Bug 5 follow-on #3: the proactive tick NEVER touches a PAGE-HOLDER (comix) — not
    via ``/solve`` (``pm clear`` would cold-wipe its warm page) and not via the eval
    path (comix has no replayable cf_clearance to re-mint). It is re-captured
    reactively on a 403 self-heal instead. A within-lead comix expiry is left untouched
    (no device hit), and only a real cf_clearance key (mangadot) is re-minted."""
    solve_route = respx.post(f"{_SIDECAR}/solve").mock(
        return_value=_solve_response(
            token="fresh-mangadot", expires=time.time() + 99999
        )
    )
    eval_route = respx.post(f"{_SIDECAR}/eval").mock(
        return_value=_eval_clearance_response()
    )
    solver = _solver(
        challenge_urls={
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
        },
        warm_last_keys={"comix"},
    )
    solver._refresh_lead_s = 120.0
    solver._held["comix"] = Clearance(
        cookies={"cf_clearance": "comix-cap"}, user_agent=_WEBVIEW_UA
    )
    solver._expires_at["comix"] = time.time() + 30.0  # inside lead, but a page-holder
    solver._held["mangadot"] = Clearance(
        cookies={"cf_clearance": "stale-mangadot"}, user_agent=_WEBVIEW_UA
    )
    solver._expires_at["mangadot"] = time.time() + 30.0  # inside lead → refreshed
    try:
        await solver._refresh_tick()
    finally:
        await solver.aclose()
    # comix (page-holder) was NOT touched by either path.
    assert not eval_route.called
    assert solver._held["comix"].cookies == {"cf_clearance": "comix-cap"}
    # mangadot (a real cf_clearance key) WAS proactively re-minted via /solve.
    assert solve_route.called
    assert solver._held["mangadot"].cookies == {"cf_clearance": "fresh-mangadot"}


@respx.mock
@pytest.mark.asyncio
async def test_refresh_tick_skips_on_demand_and_not_yet_expiring() -> None:
    """The tick never re-mints an on-demand key, nor a key still far from expiry."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver(on_demand_keys=frozenset({"mangadot"}))
    solver._refresh_lead_s = 120.0
    # mangadot is on-demand AND expiring; kagane is eager but far from expiry.
    solver._expires_at["mangadot"] = time.time() + 1.0
    solver._expires_at["kagane"] = time.time() + 100000.0
    try:
        await solver._refresh_tick()
    finally:
        await solver.aclose()
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_refresh_tick_keeps_old_clearance_when_resolve_fails() -> None:
    """A failed proactive re-mint keeps the old (about-to-expire) clearance — the
    reactive 403 self-heal remains the backstop; the tick must not raise."""
    respx.post(f"{_SIDECAR}/solve").mock(return_value=httpx.Response(504))
    solver = _solver()
    solver._refresh_lead_s = 120.0
    old = Clearance(cookies={"cf_clearance": "old-token"}, user_agent=_WEBVIEW_UA)
    solver._held["mangadot"] = old
    solver._expires_at["mangadot"] = time.time() + 10.0
    try:
        delay = await solver._refresh_tick()  # must NOT raise
    finally:
        await solver.aclose()
    assert solver._held["mangadot"] is old  # unchanged
    assert isinstance(delay, float)


@respx.mock
@pytest.mark.asyncio
async def test_refresh_tick_sleep_is_clamped() -> None:
    """No known expiries → sleep the max; a far-off expiry is also clamped to max; a
    near one floors at min."""
    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    solver._refresh_lead_s = 120.0
    solver._refresh_min_sleep_s = 30.0
    solver._refresh_max_sleep_s = 600.0
    try:
        assert await solver._refresh_tick() == 600.0  # nothing tracked → max
        solver._expires_at["kagane"] = time.time() + 1_000_000.0  # very far
        assert await solver._refresh_tick() == 600.0  # clamped to max
    finally:
        await solver.aclose()


# ── #2 lifecycle: start() is a guarded no-op, aclose() cancels the loop ────────


def test_start_is_noop_when_sidecar_unconfigured() -> None:
    """An unconfigured sidecar spins NO background task (gate/CI/local-no-redroid stays
    byte-for-byte unchanged)."""
    solver = _solver(base_url=None)
    solver.start()
    assert solver._refresh_task is None


@pytest.mark.asyncio
async def test_start_then_aclose_cancels_refresh_task() -> None:
    """start() launches the refresh loop; aclose() cancels and awaits it."""
    solver = _solver()
    solver.start()
    task = solver._refresh_task
    assert task is not None and not task.done()
    solver.start()  # idempotent — does not spawn a second task
    assert solver._refresh_task is task
    await solver.aclose()
    assert task.cancelled() or task.done()
    assert solver._refresh_task is None


# ── bug 4 Fix B: gateway-side device-op serialization (no `503 solver busy`) ────


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_evals_do_not_overlap_device_ops() -> None:
    """Fix B: two concurrent ``eval_in_webview`` calls QUEUE on the single
    ``_device_lock`` — the second's sidecar POST starts only after the first's
    completes (max in-flight is ever 1, never 2), so the gateway never storms the
    sidecar into `503 solver busy` with its own coordinated evals."""
    inflight = 0
    max_inflight = 0
    entered = asyncio.Semaphore(0)
    release = asyncio.Event()

    async def _gated(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        entered.release()
        await release.wait()
        inflight -= 1
        return httpx.Response(200, json={"value": 1})

    respx.post(f"{_SIDECAR}/eval").mock(side_effect=_gated)
    solver = _solver()
    try:
        t1 = asyncio.create_task(solver.eval_in_webview(_EVAL_URL, "j1"))
        t2 = asyncio.create_task(solver.eval_in_webview(_EVAL_URL, "j2"))
        await entered.acquire()  # the FIRST eval is inside the sidecar handler
        await asyncio.sleep(0.05)  # the second must be blocked on _device_lock
        assert inflight == 1  # only one device op in flight
        assert max_inflight == 1
        release.set()
        await asyncio.gather(t1, t2)
    finally:
        await solver.aclose()
    assert max_inflight == 1  # never two overlapping device ops


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_solve_and_eval_serialize_on_the_device() -> None:
    """Fix B: a concurrent ``_solve`` (via get_clearance) and ``eval_in_webview``
    likewise serialize — one device op at a time across BOTH sidecar endpoints."""
    inflight = 0
    max_inflight = 0
    release = asyncio.Event()

    async def _gated_solve(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await release.wait()
        inflight -= 1
        return _solve_response()

    async def _gated_eval(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await release.wait()
        inflight -= 1
        return httpx.Response(200, json={"value": 1})

    respx.post(f"{_SIDECAR}/solve").mock(side_effect=_gated_solve)
    respx.post(f"{_SIDECAR}/eval").mock(side_effect=_gated_eval)
    solver = _solver()
    try:
        t1 = asyncio.create_task(solver.get_clearance("mangadot", force_resolve=True))
        t2 = asyncio.create_task(solver.eval_in_webview(_EVAL_URL, "j1"))
        await asyncio.sleep(0.05)  # one acquires the lock, the other queues behind it
        assert max_inflight == 1
        release.set()
        await asyncio.gather(t1, t2)
    finally:
        await solver.aclose()
    assert max_inflight == 1  # solve + eval never overlapped on the one device


@pytest.mark.asyncio
async def test_device_op_acquire_timeout_raises_clean_runtimeerror() -> None:
    """Fix B: a ``_device_op`` acquire that cannot be satisfied within
    ``_device_acquire_timeout_s`` raises a clean ``RuntimeError`` naming device
    contention rather than hanging unboundedly."""
    solver = _solver()
    solver._device_acquire_timeout_s = 0.05
    await solver._device_lock.acquire()  # hold the device so the next acquire times out
    try:
        with pytest.raises(RuntimeError, match="device contention"):
            async with solver._device_op():
                pass  # pragma: no cover — the acquire never succeeds
    finally:
        solver._device_lock.release()
        await solver.aclose()
    # The timed-out acquire did NOT leave the lock held (it was never ours).
    assert not solver._device_lock.locked()


# ── bug 4 Fix C: device_session lease + proactive-refresh deferral ─────────────


@pytest.mark.asyncio
async def test_device_session_counts_and_unwinds_including_nested_and_exception() -> (
    None
):
    """Fix C: ``device_session`` increments then decrements ``_foreground_inflight``
    (back to 0 on exit, INCLUDING on exception); nested sessions stack and unwind."""
    solver = _solver()
    try:
        assert solver._foreground_inflight == 0
        async with solver.device_session():
            assert solver._foreground_inflight == 1
            async with solver.device_session():
                assert solver._foreground_inflight == 2  # nested sessions stack
            assert solver._foreground_inflight == 1
        assert solver._foreground_inflight == 0
        with pytest.raises(ValueError, match="boom"):
            async with solver.device_session():
                assert solver._foreground_inflight == 1
                raise ValueError("boom")
        assert solver._foreground_inflight == 0  # decremented even on exception
    finally:
        await solver.aclose()


@pytest.mark.asyncio
async def test_device_session_clean_when_unconfigured() -> None:
    """Fix C: an unconfigured backend (base_url None) still enters/exits the lease
    cleanly — the gate / CI / a local box without redroid path is unchanged (the
    evals inside would raise RuntimeError anyway; the lease is just a counter)."""
    solver = _solver(base_url=None)
    try:
        async with solver.device_session():
            assert solver._foreground_inflight == 1
        assert solver._foreground_inflight == 0
    finally:
        await solver.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_refresh_tick_defers_while_foreground_session_held() -> None:
    """Fix C: ``_refresh_tick`` performs NO solve and returns the short defer delay
    while ``_foreground_inflight > 0``; once the session exits, a subsequent tick
    re-mints the expiring key normally (the deferral is not a permanent skip)."""
    route = respx.post(f"{_SIDECAR}/solve").mock(
        return_value=_solve_response(token="fresh-token", expires=time.time() + 99999)
    )
    solver = _solver()
    solver._refresh_lead_s = 120.0
    solver._held["mangadot"] = Clearance(
        cookies={"cf_clearance": "stale-token"}, user_agent=_WEBVIEW_UA
    )
    solver._expires_at["mangadot"] = time.time() + 30.0  # inside lead → expiring
    try:
        async with solver.device_session():
            delay = await solver._refresh_tick()
            assert delay == solver._refresh_min_sleep_s  # deferred to a short re-check
            assert not route.called  # NO solve while a foreground session holds device
        # Session exited → the next tick re-mints normally.
        await solver._refresh_tick()
    finally:
        await solver.aclose()
    assert route.call_count == 1
    assert solver._held["mangadot"].cookies == {"cf_clearance": "fresh-token"}


# ── bug 5 Fix (a)/(b): close the proactive-refresh deferral race ───────────────
# The bug: the tick-top _foreground_inflight check in _refresh_tick is one-shot and
# RACY — a refresh that passes it while the device is FREE then queues on _device_lock
# and runs AFTER comix claims the foreground lease, stealing the shared WebView mid
# comix sequence (comix's next eval re-navs + pays a ~9s re-clear → 30s budget blown).
# The FakePipeline gate could not see this coordination bug; these test it directly.


@respx.mock
@pytest.mark.asyncio
async def test_refresh_bails_when_foreground_claimed_after_it_queued() -> None:
    """Fix (a): a refresh that committed + queued on the device lock while
    ``_foreground_inflight == 0`` does NOT issue its sidecar /solve once a
    ``device_session`` is entered before the lock frees — the post-acquire re-check in
    ``_device_op`` makes the queued solve BAIL rather than navigate the shared WebView
    away mid comix sequence. This is the exact bug-5 race (a mangadot refresh stealing
    the device between comix's two evals)."""
    route = respx.post(f"{_SIDECAR}/solve").mock(
        return_value=_solve_response(token="fresh-token", expires=time.time() + 99999)
    )
    solver = _solver()
    solver._refresh_lead_s = 120.0
    solver._held["mangadot"] = Clearance(
        cookies={"cf_clearance": "stale-token"}, user_agent=_WEBVIEW_UA
    )
    solver._expires_at["mangadot"] = time.time() + 30.0  # inside lead → expiring
    refresh: asyncio.Task[float] | None = None
    # Hold the device lock so the refresh's solve QUEUES behind it — simulating comix's
    # in-flight cold-solve / first eval holding the single redroid.
    await solver._device_lock.acquire()
    try:
        # The refresh passes its tick-top check (foreground == 0 right now), commits to
        # the mangadot solve, and parks on the held device lock.
        refresh = asyncio.create_task(solver._refresh_tick())
        await asyncio.sleep(0.05)
        assert not route.called  # still queued on the lock — no /solve issued yet
        # comix NOW claims the foreground lease while the refresh is queued.
        async with solver.device_session():
            # Free the device → the queued refresh acquires the lock, re-checks the
            # foreground counter (now 1), and bails.
            solver._device_lock.release()
            delay = await refresh
        assert not route.called  # the refresh BAILED — never navigated the WebView
        assert delay == solver._refresh_min_sleep_s  # rescheduled a short re-check
        # The held clearance was untouched — it keeps serving (D-35 backstops a lapse).
        assert solver._held["mangadot"].cookies == {"cf_clearance": "stale-token"}
        assert solver._foreground_inflight == 0  # lease unwound cleanly
        assert not solver._device_lock.locked()  # the bail released the lock
    finally:
        if refresh is not None and not refresh.done():
            refresh.cancel()
        if solver._device_lock.locked():
            solver._device_lock.release()
        await solver.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_comix_cold_solve_eval_runs_with_foreground_lease_held() -> None:
    """Fix (b) invariant: comix's FIRST device touch — the ``_search_series`` eval whose
    sidecar clear-if-challenged IS the cold clear — runs INSIDE ``device_session``, so
    its device op observes ``_foreground_inflight > 0``. There is therefore no
    ``inflight == 0`` window while comix is mid cold-clear for a background refresh to
    commit into (it pairs with Fix (a)'s post-lock re-check to close the race)."""
    seen: list[int] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        # The sidecar handler stands in for the in-WebView eval (+ its cold clear): the
        # gateway only reaches here while it holds the device op for the eval.
        seen.append(solver._foreground_inflight)
        return httpx.Response(200, json={"value": 1})

    respx.post(f"{_SIDECAR}/eval").mock(side_effect=_handler)
    solver = _solver()
    try:
        # comix wraps its solve+eval sequence in device_session (comix.py search()).
        async with solver.device_session():
            await solver.eval_in_webview(_EVAL_URL, "return await c.list({})")
    finally:
        await solver.aclose()
    assert seen == [1]  # the cold-clear-triggering eval ran with the lease held


# ── bug 5 Lever B: extend the foreground-lease deferral to the STARTUP warm() path ──
# The startup eager warm() solves used defer_if_foreground=False, so a comix search that
# arrived DURING warm() (while warm() was solving / had queued a solve for ANOTHER
# android source) could have its warm cleared WebView page navigated away by the queued
# warm solve — the same steal class as the 5a refresh race, but on the eager warm()
# path. Lever B threads defer_if_foreground=True through warm()'s solves; a key is
# skipped, NOT failed (never force-disabled — it solves on-demand / next refresh).


@respx.mock
@pytest.mark.asyncio
async def test_warm_solve_defers_when_foreground_lease_held() -> None:
    """Lever B: a startup ``warm()`` eager solve that queued on the device lock while
    ``_foreground_inflight == 0`` does NOT issue its sidecar /solve once a
    ``device_session`` foreground lease (a comix search) is entered before the lock
    frees — the post-acquire re-check in ``_device_op`` makes it BAIL. The deferred key
    is NOT reported failed (so the lifespan never force-disables it) and stays unheld
    (it solves on-demand / via the next proactive refresh)."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    # One android key isolates the deferral path deterministically.
    solver = _solver(challenge_urls={"mangadot": "https://mangadot.net/"})
    warm_task: asyncio.Task[list[str]] | None = None
    # Hold the device lock so warm()'s solve QUEUES behind it — standing in for an
    # in-flight device op holding the single redroid as warm() fires.
    await solver._device_lock.acquire()
    try:
        warm_task = asyncio.create_task(solver.warm())
        await asyncio.sleep(0.05)
        assert not route.called  # warm's solve is queued on the lock — no /solve yet
        # A comix search NOW claims the foreground lease while the warm solve is queued.
        async with solver.device_session():
            solver._device_lock.release()  # free the device → warm's solve re-checks
            failed = await warm_task
        assert not route.called  # the warm solve BAILED — never navigated the WebView
        assert failed == []  # a deferral is NOT a failure → key not force-disabled
        assert solver._held == {}  # nothing warmed (it solves on-demand later)
        assert solver._foreground_inflight == 0  # lease unwound cleanly
        assert not solver._device_lock.locked()  # the bail released the lock
    finally:
        if warm_task is not None and not warm_task.done():
            warm_task.cancel()
        if solver._device_lock.locked():
            solver._device_lock.release()
        await solver.aclose()


# ── bug 5 follow-on #3 (Option A2): page-holders are EVAL-PRIMED, NOT /solve'd ──
# The eager cf_clearance /solve runs `pm clear` (cold-wipes the WebView: cf_clearance,
# __cf_bm, the cached env-*.js) then a cold launch, on which comix's managed CF
# challenge presents NEITHER a tappable Turnstile OOPIF NOR a host-scoped cf_clearance →
# the solve burns the full deadline AND poisons the warm page comix's eval path needs.
# So warm() EXCLUDES the page-holders (holds_webview_page → warm_last_keys) from /solve
# and instead eval-primes them LAST via a no-op eval over the warm WebView (the proven
# eval-path clear: warm Page.navigate → interactive OOPIF tap, NO pm clear → hydrated).


@respx.mock
@pytest.mark.asyncio
async def test_warm_eval_primes_page_holder_instead_of_solving_it() -> None:
    """Option A2: ``warm()`` does NOT route the page-HOLDING key (``warm_last_keys`` —
    comix) through the cf_clearance ``/solve`` (which would ``pm clear`` + cold-launch
    and poison the warm page). It ``/solve``s ONLY the clearance-only sources (in
    ``challenge_urls`` order) and EVAL-PRIMES the page-holder LAST — a no-op eval that
    leaves the warm WebView parked on a cleared+hydrated comix page."""
    solved: list[str] = []
    evaled: list[dict[str, object]] = []

    async def _record_solve(request: httpx.Request) -> httpx.Response:
        solved.append(json.loads(request.content)["challenge_url"])
        return _solve_response()

    async def _record_eval(request: httpx.Request) -> httpx.Response:
        evaled.append(json.loads(request.content))
        return _eval_clearance_response(token="comix-prime-token")

    respx.post(f"{_SIDECAR}/solve").mock(side_effect=_record_solve)
    respx.post(f"{_SIDECAR}/eval").mock(side_effect=_record_eval)
    solver = _solver(
        challenge_urls={
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
            "kagane": "https://kagane.to/",
        },
        warm_last_keys={"comix"},
    )
    try:
        failed = await solver.warm()
    finally:
        await solver.aclose()
    assert failed == []
    # The page-holder (comix) is NEVER /solve'd — only the clearance-only keys are,
    # in challenge_urls order.
    assert solved == ["https://mangadot.net/", "https://kagane.to/"]
    # comix is eval-primed exactly once, with the no-op prime JS, NO wait_for (it relies
    # only on the solver's env-*.js hydration signal, never a DOM predicate), and
    # ``extract_clearance`` so the prime ALSO mints+holds comix's clearance (Bug 5 #3).
    assert len(evaled) == 1
    assert evaled[0]["challenge_url"] == "https://comix.to/"
    assert evaled[0]["js"] == _WEBVIEW_PRIME_JS
    assert evaled[0]["extract_clearance"] is True
    assert "wait_for" not in evaled[0]
    # The prime HELD comix's eval-minted clearance — the first httpx image leg has a
    # valid cookie + UA with no /solve.
    assert solver._held["comix"].cookies == {"cf_clearance": "comix-prime-token"}
    assert solver._held["comix"].user_agent == _WEBVIEW_UA


@respx.mock
@pytest.mark.asyncio
async def test_warm_eval_prime_failure_is_best_effort_not_disabled() -> None:
    """Option A2: a FAILED page-holder eval-prime (e.g. CF flaky → /eval non-200) must
    NOT hang startup or mark the source failed (force-disable it). warm() swallows the
    prime error, completes normally, releases the gate, and reports only genuine
    /solve failures — comix is left to clear on its first real /search via the same
    eval path."""
    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    respx.post(f"{_SIDECAR}/eval").mock(return_value=httpx.Response(502))
    solver = _solver(
        challenge_urls={
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
        },
        warm_last_keys={"comix"},
    )
    try:
        failed = await solver.warm()
    finally:
        await solver.aclose()
    # The prime failed but comix is NOT reported failed (never force-disabled); the
    # clearance-only key warmed fine.
    assert failed == []
    assert "comix" not in failed
    assert solver._warm_complete.is_set()  # gate released despite the prime failure


@respx.mock
@pytest.mark.asyncio
async def test_warm_prime_with_no_cookie_leaves_held_empty_not_failed() -> None:
    """Follow-on #3 (the COMMON comix case): a SUCCESSFUL prime that extracts NO
    host-scoped cookie (``clearance: null`` — warm fast-path eval, no Turnstile) parks
    the page cleared+hydrated but leaves ``_held`` empty. It is NOT a failure (comix's
    path is the warm eval page; its image CDN serves bytes with no cookie), the gate is
    released, and comix is never force-disabled."""
    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    respx.post(f"{_SIDECAR}/eval").mock(
        return_value=_eval_clearance_response(clearance=None)
    )
    solver = _solver(
        challenge_urls={
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
        },
        warm_last_keys={"comix"},
    )
    try:
        failed = await solver.warm()
    finally:
        await solver.aclose()
    assert failed == []
    assert "comix" not in solver._held  # nothing to hold — not an error
    assert "comix" not in solver._expires_at
    assert solver._warm_complete.is_set()


@respx.mock
@pytest.mark.asyncio
async def test_warm_gate_released_only_after_page_holder_prime() -> None:
    """Option A2 × Fix #2: the startup-warm gate (``_warm_complete``) is released ONLY
    after BOTH the clearance ``/solve`` loop AND the page-holder eval-prime complete —
    so a parked foreground search proceeds against a device with comix already primed,
    not mid-prime. Holding the /eval response open keeps ``_warm_complete`` UNSET until
    the prime returns."""
    release_eval = asyncio.Event()

    async def _gated_eval(request: httpx.Request) -> httpx.Response:
        await release_eval.wait()
        return _eval_clearance_response()

    respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    respx.post(f"{_SIDECAR}/eval").mock(side_effect=_gated_eval)
    solver = _solver(
        challenge_urls={
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
        },
        warm_last_keys={"comix"},
    )
    warm_task: asyncio.Task[list[str]] | None = None
    try:
        warm_task = asyncio.create_task(solver.warm())
        await asyncio.sleep(0.05)
        # The clearance /solve is done, but the prime is parked on the gated /eval —
        # the gate must NOT be released yet (it waits on the prime too).
        assert not solver._warm_complete.is_set()
        release_eval.set()  # let the prime complete
        failed = await asyncio.wait_for(warm_task, timeout=1.0)
        assert failed == []
        assert solver._warm_complete.is_set()  # released only after the prime returned
    finally:
        release_eval.set()
        if warm_task is not None and not warm_task.done():
            warm_task.cancel()
        await solver.aclose()


# ── bug 5 Lever A: proactive-refresh-by-AGE cap (cf_clearance cookie expiry lies) ──


@pytest.mark.asyncio
async def test_refresh_max_age_caps_tracked_expiry() -> None:
    """Lever A: with ``refresh_max_age_s`` set, the tracked expiry that drives the
    proactive-refresh loop is capped at ``now + max_age`` whenever that is SOONER than
    the (meaningless ~1yr) cookie expiry — and is applied even when the cookie carries
    NO expiry. A cookie expiry that is already sooner than the cap is kept as-is."""
    solver = _solver(refresh_max_age_s=300.0)
    try:
        # Far-future cookie expiry → capped down to ~now + 300s.
        before = time.time()
        solver._record_expiry("mangadot", before + 99_999_999.0)
        capped = solver._expires_at["mangadot"]
        assert before + 300.0 <= capped <= time.time() + 300.0
        # No cookie expiry at all → still tracked at the age cap (not dropped).
        solver._record_expiry("kagane", None)
        assert "kagane" in solver._expires_at
        # A cookie expiry SOONER than the cap is kept verbatim (min wins).
        soon = time.time() + 60.0
        solver._record_expiry("mangadot", soon)
        assert solver._expires_at["mangadot"] == soon
    finally:
        await solver.aclose()


@pytest.mark.asyncio
async def test_refresh_max_age_disabled_by_default_keeps_cookie_expiry_only() -> None:
    """Lever A is OFF by default (and for a non-positive value): a None cookie expiry
    clears the tracked entry and a real cookie expiry is tracked verbatim — the historic
    cookie-expiry-only behavior, byte-for-byte unchanged."""
    for over in ({}, {"refresh_max_age_s": 0.0}, {"refresh_max_age_s": -5.0}):
        solver = _solver(**over)
        try:
            assert solver._refresh_max_age_s is None
            solver._record_expiry("mangadot", None)
            assert "mangadot" not in solver._expires_at
            future = time.time() + 1234.0
            solver._record_expiry("kagane", future)
            assert solver._expires_at["kagane"] == future
        finally:
            await solver.aclose()


# ── bug 5 Fix #2: gate a foreground search on startup warm() completion ─────────
# The startup eager warm() is fired NON-BLOCKING, so a comix /search that arrives during
# the warm storm races the in-flight foreign solves: a queued warm/refresh solve can
# steal the single shared WebView mid-sequence and comix's recovery re-nav burns the
# budget (the collision spike). Fix #2 makes an android FOREGROUND sequence WAIT at the
# device door (device_session) for warm() to finish before it claims the device.


@pytest.mark.asyncio
async def test_device_session_no_gate_until_armed() -> None:
    """Fix #2: until the app arms the gate, ``device_session`` enters IMMEDIATELY even
    though ``_warm_complete`` is unset — a solver that never warms (tests / unconfigured
    sidecar / CI) is byte-for-byte unchanged and NEVER waits."""
    solver = _solver()
    try:
        assert not solver._warm_gate_armed
        assert not solver._warm_complete.is_set()
        async with solver.device_session():  # must NOT block on the unset event
            assert solver._foreground_inflight == 1
        assert solver._foreground_inflight == 0
    finally:
        await solver.aclose()


@pytest.mark.asyncio
async def test_device_session_waits_for_warm_then_proceeds() -> None:
    """Fix #2: once armed, a foreground sequence PARKS at the device door (does NOT
    claim the device — ``_foreground_inflight`` stays 0) until warm() sets the
    completion event, then proceeds."""
    solver = _solver()
    solver.arm_warm_gate()  # armed; _warm_complete cleared
    entered = asyncio.Event()

    async def _seq() -> None:
        async with solver.device_session():
            entered.set()

    task = asyncio.create_task(_seq())
    try:
        await asyncio.sleep(0.05)
        assert not entered.is_set()  # parked at the gate
        assert solver._foreground_inflight == 0  # has NOT claimed the device yet
        solver._warm_complete.set()  # warm() finished → release the gate
        await asyncio.wait_for(task, timeout=1.0)
        assert entered.is_set()
        assert solver._foreground_inflight == 0  # entered then unwound cleanly
    finally:
        if not task.done():
            task.cancel()
        await solver.aclose()


@pytest.mark.asyncio
async def test_device_session_warm_gate_falls_through_on_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fix #2: a slow/hung warm() must NOT hang the search forever — the gate falls
    THROUGH on timeout (and the search proceeds; Lever B defers any straggler warm
    solve)."""
    solver = _solver(warm_gate_timeout_s=0.05)
    solver.arm_warm_gate()  # armed but warm() never completes (event never set)
    try:
        with caplog.at_level("WARNING", logger="manga_gateway"):
            async with solver.device_session():  # must fall through, NOT hang
                assert solver._foreground_inflight == 1
        assert solver._foreground_inflight == 0
    finally:
        await solver.aclose()
    assert any("warm gate timed out" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_warm_sets_completion_gate_even_when_every_key_fails() -> None:
    """Fix #2: warm() releases the gate on EVERY exit. An unconfigured sidecar fails
    every key, yet the completion event is still set (and warm() self-armed) so a parked
    foreground sequence is never stranded."""
    solver = _solver(base_url=None)  # unconfigured → every key fails
    try:
        assert not solver._warm_complete.is_set()
        failed = await solver.warm()
        assert set(failed) == set(_CHALLENGE_URLS)  # all failed (boot-disabled)
        assert solver._warm_complete.is_set()  # gate released regardless
        assert solver._warm_gate_armed  # warm() self-arms
    finally:
        await solver.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_warm_completes_while_foreground_search_waits_at_gate() -> None:
    """Fix #2 × Lever B: because the search PARKS at the gate (``_foreground_inflight``
    stays 0 during warm), warm()'s own eager solves (defer_if_foreground=True) do NOT
    bail — warm clears every source FIRST, then the search proceeds against a
    fully-cleared device. This is the whole point: gate the search instead of letting it
    steal the device mid-warm."""
    route = respx.post(f"{_SIDECAR}/solve").mock(return_value=_solve_response())
    solver = _solver()
    solver.arm_warm_gate()
    entered = asyncio.Event()

    async def _search() -> None:
        async with solver.device_session():
            entered.set()

    search = asyncio.create_task(_search())
    try:
        await asyncio.sleep(0.05)
        assert not entered.is_set()  # parked at the gate while warm runs
        assert solver._foreground_inflight == 0  # 0 ⇒ warm's solves never bail
        failed = await solver.warm()  # warm runs with inflight 0 → no deferral
        assert failed == []  # both keys solved (none deferred, none failed)
        assert route.call_count == len(_CHALLENGE_URLS)
        await asyncio.wait_for(search, timeout=1.0)
        assert entered.is_set()  # search proceeds once warm released the gate
    finally:
        if not search.done():
            search.cancel()
        await solver.aclose()


# ── #3: eval_in_webview emits a real-timed, redacted eval metric (260622-pl9) ──


def _eval_solver(**over: object) -> AndroidSolver:
    """A solver whose challenge map attributes evals to comix (reverse-mapped)."""
    kwargs: dict[str, object] = {
        "base_url": _SIDECAR,
        "api_key": SecretStr("sidecar-secret"),
        "challenge_urls": {"comix": "https://comix.to/"},
        "timeout_s": 5.0,
    }
    kwargs.update(over)
    return AndroidSolver(**kwargs)  # type: ignore[arg-type]


# Sentinels: if EITHER appears in any emitted metric field the redaction broke.
_EVAL_JS_SENTINEL = "SENTINEL_JS_must_never_be_recorded_42"
_EVAL_RESULT_SENTINEL = "SENTINEL_RESULT_must_never_be_recorded_99"


@pytest.mark.asyncio
async def test_eval_in_webview_emits_real_timed_redacted_eval_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the REAL Collector/InMemoryStore (not a call-asserting mock): a
    successful eval emits exactly ONE kind="eval" event with duration_ms>0 attributed
    to comix, and NEITHER the eval js NOR the eval result leaks into any field."""
    from dataclasses import asdict

    from manga_gateway.metrics.collector import Collector, set_collector
    from manga_gateway.metrics.context import current_source
    from manga_gateway.metrics.store import InMemoryStore
    from tests._metrics_helpers import CapturingRingWriter

    ring = CapturingRingWriter()
    set_collector(Collector(InMemoryStore(slow_factor=3.0), ring_writer=ring))  # type: ignore[arg-type]

    async def _fake_post_eval(*_a: object, **_k: object) -> dict[str, object]:
        return {"value": _EVAL_RESULT_SENTINEL}

    solver = _eval_solver()
    monkeypatch.setattr(solver, "_post_eval", _fake_post_eval)
    try:
        result = await solver.eval_in_webview("https://comix.to/", _EVAL_JS_SENTINEL)
    finally:
        await solver.aclose()
        set_collector(None)
        current_source.set(None)

    assert result == _EVAL_RESULT_SENTINEL  # observability only — value untouched

    evals = [e for e in ring.iter_recent() if e.kind == "eval"]
    assert len(evals) == 1  # exactly one emit per outer call
    ev = evals[0]
    assert ev.outcome == "ok"
    assert ev.op == "eval"
    assert ev.source_key == "comix"  # reverse-mapped challenge_url → comix
    assert ev.duration_ms > 0.0  # real perf_counter timing, not 0ms CACHE
    assert ev.url is None  # redaction: structurally no js/result/token

    # REDACTION — neither the js nor the eval result appears in ANY field.
    for field_value in asdict(ev).values():
        text = str(field_value)
        assert _EVAL_JS_SENTINEL not in text
        assert _EVAL_RESULT_SENTINEL not in text


@pytest.mark.asyncio
async def test_eval_in_webview_emits_error_metric_then_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing eval (its _post_eval raises a non-retryable error) emits exactly ONE
    kind="eval" event with outcome="error" + the exc type name, then the original
    exception re-raises unchanged."""
    from manga_gateway.metrics.collector import Collector, set_collector
    from manga_gateway.metrics.context import current_source
    from manga_gateway.metrics.store import InMemoryStore
    from tests._metrics_helpers import CapturingRingWriter

    ring = CapturingRingWriter()
    set_collector(Collector(InMemoryStore(slow_factor=3.0), ring_writer=ring))  # type: ignore[arg-type]

    class _EvalBoom(RuntimeError):
        pass

    async def _boom_post_eval(*_a: object, **_k: object) -> dict[str, object]:
        raise _EvalBoom(_EVAL_RESULT_SENTINEL)

    solver = _eval_solver()
    monkeypatch.setattr(solver, "_post_eval", _boom_post_eval)
    try:
        with pytest.raises(_EvalBoom):
            await solver.eval_in_webview("https://comix.to/", _EVAL_JS_SENTINEL)
    finally:
        await solver.aclose()
        set_collector(None)
        current_source.set(None)

    evals = [e for e in ring.iter_recent() if e.kind == "eval"]
    assert len(evals) == 1
    ev = evals[0]
    assert ev.outcome == "error"
    assert ev.error == "_EvalBoom"
    assert ev.source_key == "comix"
    assert ev.duration_ms > 0.0
    assert ev.url is None
