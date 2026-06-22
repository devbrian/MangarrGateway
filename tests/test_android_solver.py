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
async def test_refresh_and_reactive_force_resolve_coalesce() -> None:
    """A proactive refresh tick (a held key inside the lead window) and a concurrent
    reactive ``force_resolve`` for the SAME key share ONE sidecar /solve — both funnel
    through the per-key single-flight task."""
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
        # Refresh first so it enters the shared solve before the reactive caller pops
        # the held entry; the reactive force_resolve then attaches to the SAME task.
        refresh = asyncio.create_task(solver._refresh_tick())
        reactive = asyncio.create_task(
            solver.get_clearance("mangadot", force_resolve=True)
        )
        await entered.wait()
        gate.set()
        await asyncio.gather(refresh, reactive)
    finally:
        await solver.aclose()
    assert route.call_count == 1  # refresh + reactive coalesced onto one /solve


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
