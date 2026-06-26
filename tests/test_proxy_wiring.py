"""Proxy-wiring tests (#65, PROXY-01).

Wire a single static residential proxy through BOTH egress legs — the stealth
browser launch closures AND the shared httpx client — from ONE shared
``build_proxy(settings)`` helper (the future pool/rotation seam). ``cf_clearance``
is IP-bound, so browser and httpx MUST share one egress IP by construction.

All deterministic + OFFLINE (D-42): no real browser, no real proxy, NO real
credentials. The proxy password is a ``SecretStr`` — its plaintext must never
appear in ``repr(settings)``, ``str(settings)``, or any log line. Tests use an
obviously-fake sentinel (``_FAKE_PASS``) for the password, never a real
credential.

Regression contract (T-odg-04): when ``cloudflare_proxy_server`` is unset, NO
``proxy=`` kwarg is threaded anywhere — today's behavior is byte-for-byte
unchanged. Asserted on all three legs (helper, browser x2, httpx).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from manga_gateway.config import Settings
from manga_gateway.framework.antibot import CloudflareSolver
from manga_gateway.framework.proxy import build_proxy
from manga_gateway.framework.transport import HttpxTransport

TEST_API_KEY = "test-key-deterministic-0123456789"
# Obviously-fake local-only values — NEVER a real proxy credential (T-odg-02).
# The password is a distinctive sentinel (no substring of any other field value)
# so the redaction asserts can't false-pass on an incidental collision.
_FAKE_SERVER = "http://proxy.invalid:8080"
_FAKE_USER = "fakeuser"
_FAKE_PASS = "NOTAREALSECRET-zzz9"


def _settings(**over: object) -> Settings:
    return Settings(api_key=TEST_API_KEY, **over)  # type: ignore[arg-type]


# ────────────────────────── Task 1: build_proxy helper ──────────────────────────


def test_build_proxy_returns_none_pair_when_unconfigured() -> None:
    """No ``cloudflare_proxy_server`` ⇒ ``(None, None)`` (the regression guard)."""
    pw, hx = build_proxy(_settings())
    assert pw is None
    assert hx is None


def test_empty_string_proxy_secrets_normalize_to_none() -> None:
    """Unset CI secrets arrive as ``""`` (not absent); they must be treated as
    UNSET so ``build_proxy`` returns ``(None, None)`` — bare egress, NOT a broken
    ``httpx.Proxy(url="")`` / ``{"server": ""}`` (CodeRabbit, #138).

    Also guards the mixed case (server set, blank creds) → server-only, no auth.
    """
    s = _settings(
        cloudflare_proxy_server="",
        cloudflare_proxy_username="",
        cloudflare_proxy_password="",
    )
    assert s.cloudflare_proxy_server is None
    assert s.cloudflare_proxy_username is None
    assert s.cloudflare_proxy_password is None
    assert build_proxy(s) == (None, None)

    # Whitespace-only server is also unset ⇒ bare egress (CodeRabbit, #138).
    s_ws = _settings(cloudflare_proxy_server="   ")
    assert s_ws.cloudflare_proxy_server is None
    assert build_proxy(s_ws) == (None, None)

    # Whitespace-only is also unset; blank creds with a real server ⇒ no auth.
    s2 = _settings(
        cloudflare_proxy_server=_FAKE_SERVER,
        cloudflare_proxy_username="   ",
        cloudflare_proxy_password="",
    )
    pw, hx = build_proxy(s2)
    assert pw == {"server": _FAKE_SERVER}
    assert hx == _FAKE_SERVER


def test_build_proxy_server_only_omits_credentials() -> None:
    """Server only ⇒ Playwright dict has NO username/password keys; the httpx
    value is the plain server URL with no embedded credentials."""
    pw, hx = build_proxy(_settings(cloudflare_proxy_server=_FAKE_SERVER))
    assert pw == {"server": _FAKE_SERVER}
    assert "username" not in pw
    assert "password" not in pw
    # No-auth httpx leg: the plain server URL string, credential-free.
    assert hx == _FAKE_SERVER


def test_build_proxy_full_auth_builds_both_shapes() -> None:
    """Server + username + password ⇒ Playwright dict carries plaintext creds;
    httpx value carries the same auth WITHOUT the password in the URL string."""
    pw, hx = build_proxy(
        _settings(
            cloudflare_proxy_server=_FAKE_SERVER,
            cloudflare_proxy_username=_FAKE_USER,
            cloudflare_proxy_password=_FAKE_PASS,
        )
    )
    assert pw == {
        "server": _FAKE_SERVER,
        "username": _FAKE_USER,
        "password": _FAKE_PASS,
    }
    # httpx leg uses httpx.Proxy(auth=...) so the password never enters the URL.
    assert isinstance(hx, httpx.Proxy)
    assert str(hx.url) == _FAKE_SERVER
    assert _FAKE_PASS not in str(hx.url)
    # The auth password round-trips for the actual outbound connection.
    assert hx.auth == (_FAKE_USER, _FAKE_PASS)


def test_secretstr_password_redacted_in_repr_and_str() -> None:
    """``repr``/``str`` of Settings with a password set MUST NOT leak it."""
    settings = _settings(
        cloudflare_proxy_server=_FAKE_SERVER,
        cloudflare_proxy_username=_FAKE_USER,
        cloudflare_proxy_password=_FAKE_PASS,
    )
    assert _FAKE_PASS not in repr(settings)
    assert _FAKE_PASS not in str(settings)


def test_build_proxy_emits_no_log_with_password(
    caplog: Any,
) -> None:
    """Building proxy config emits no log record containing the plaintext password."""
    with caplog.at_level(logging.DEBUG):
        build_proxy(
            _settings(
                cloudflare_proxy_server=_FAKE_SERVER,
                cloudflare_proxy_username=_FAKE_USER,
                cloudflare_proxy_password=_FAKE_PASS,
            )
        )
    for record in caplog.records:
        assert _FAKE_PASS not in record.getMessage()


# ─────────────── Task 2: proxy threaded into both launch closures ───────────────


async def test_patchright_launch_forwards_proxy_when_configured(
    monkeypatch: Any,
) -> None:
    """Patchright launch closure threads ``proxy=`` when the solver has one."""
    captured: dict[str, Any] = {}

    class _FakeChromium:
        async def launch_persistent_context(
            self, user_data_dir: str, **kwargs: Any
        ) -> object:
            captured["user_data_dir"] = user_data_dir
            captured["kwargs"] = kwargs
            return object()

    class _FakePlaywright:
        chromium = _FakeChromium()

    class _FakeAsyncPlaywright:
        def start(self) -> Any:
            async def _start() -> _FakePlaywright:
                return _FakePlaywright()

            return _start()

    monkeypatch.setattr(
        "patchright.async_api.async_playwright", lambda: _FakeAsyncPlaywright()
    )

    proxy = {"server": _FAKE_SERVER, "username": _FAKE_USER, "password": _FAKE_PASS}
    solver = CloudflareSolver(proxy=proxy)
    await solver._launch_patchright_context()  # type: ignore[attr-defined]
    assert captured["kwargs"].get("proxy") == proxy


async def test_patchright_launch_omits_proxy_when_unconfigured(
    monkeypatch: Any,
) -> None:
    """No proxy ⇒ NO ``proxy`` kwarg forwarded (byte-for-byte the current call)."""
    captured: dict[str, Any] = {}

    class _FakeChromium:
        async def launch_persistent_context(
            self, user_data_dir: str, **kwargs: Any
        ) -> object:
            captured["kwargs"] = kwargs
            return object()

    class _FakePlaywright:
        chromium = _FakeChromium()

    class _FakeAsyncPlaywright:
        def start(self) -> Any:
            async def _start() -> _FakePlaywright:
                return _FakePlaywright()

            return _start()

    monkeypatch.setattr(
        "patchright.async_api.async_playwright", lambda: _FakeAsyncPlaywright()
    )

    solver = CloudflareSolver()
    await solver._launch_patchright_context()  # type: ignore[attr-defined]
    assert "proxy" not in captured["kwargs"]


async def test_camoufox_launch_forwards_proxy_when_configured(
    monkeypatch: Any,
) -> None:
    """Camoufox launch closure threads ``proxy=`` when the solver has one."""
    captured: dict[str, Any] = {}

    async def _fake_new_browser(playwright: Any, **kwargs: Any) -> object:
        captured["kwargs"] = kwargs
        return object()

    class _FakePlaywright:
        async def start(self) -> _FakePlaywright:
            return self

    class _FakeAsyncPlaywright:
        def start(self) -> Any:
            async def _start() -> _FakePlaywright:
                return _FakePlaywright()

            return _start()

    monkeypatch.setattr(
        "camoufox.async_api.AsyncNewBrowser", _fake_new_browser, raising=False
    )
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: _FakeAsyncPlaywright()
    )

    proxy = {"server": _FAKE_SERVER, "username": _FAKE_USER, "password": _FAKE_PASS}
    solver = CloudflareSolver(engine="camoufox", fetch_concurrency=1, proxy=proxy)
    await solver._launch_camoufox_context()  # type: ignore[attr-defined]
    assert captured["kwargs"].get("proxy") == proxy


async def test_camoufox_launch_omits_proxy_when_unconfigured(
    monkeypatch: Any,
) -> None:
    """No proxy ⇒ Camoufox launch forwards NO ``proxy`` kwarg."""
    captured: dict[str, Any] = {}

    async def _fake_new_browser(playwright: Any, **kwargs: Any) -> object:
        captured["kwargs"] = kwargs
        return object()

    class _FakePlaywright:
        async def start(self) -> _FakePlaywright:
            return self

    class _FakeAsyncPlaywright:
        def start(self) -> Any:
            async def _start() -> _FakePlaywright:
                return _FakePlaywright()

            return _start()

    monkeypatch.setattr(
        "camoufox.async_api.AsyncNewBrowser", _fake_new_browser, raising=False
    )
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: _FakeAsyncPlaywright()
    )

    solver = CloudflareSolver(engine="camoufox", fetch_concurrency=1)
    await solver._launch_camoufox_context()  # type: ignore[attr-defined]
    assert "proxy" not in captured["kwargs"]


def test_solver_stores_proxy_dict_verbatim() -> None:
    """The solver stores the helper-built dict as-is; it does NOT unpack SecretStr
    itself (the helper did that at the boundary)."""
    proxy = {"server": _FAKE_SERVER}
    solver = CloudflareSolver(proxy=proxy)
    assert solver._proxy is proxy  # type: ignore[attr-defined]
    assert CloudflareSolver()._proxy is None  # type: ignore[attr-defined]


# ─────────────── Task 3: httpx transport + lifespan single-helper ───────────────


def test_transport_passes_proxy_to_asyncclient_when_configured(
    monkeypatch: Any,
) -> None:
    """``HttpxTransport`` with a proxy configured constructs its AsyncClient with
    the helper's httpx proxy value."""
    captured: dict[str, Any] = {}
    real_init = httpx.AsyncClient.__init__

    def _spy_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy_init)

    settings = _settings(
        cloudflare_proxy_server=_FAKE_SERVER,
        cloudflare_proxy_username=_FAKE_USER,
        cloudflare_proxy_password=_FAKE_PASS,
    )
    HttpxTransport(settings)
    proxy = captured.get("proxy")
    assert isinstance(proxy, httpx.Proxy)
    assert str(proxy.url) == _FAKE_SERVER


def test_transport_omits_proxy_when_unconfigured(monkeypatch: Any) -> None:
    """No proxy ⇒ AsyncClient is built with NO ``proxy`` kwarg (regression guard)."""
    captured: dict[str, Any] = {}
    real_init = httpx.AsyncClient.__init__

    def _spy_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy_init)

    HttpxTransport(_settings())
    assert "proxy" not in captured


async def test_lifespan_threads_same_proxy_into_solver() -> None:
    """The lifespan builds the proxy ONCE and feeds the Playwright dict into the
    solver — both legs share egress by construction (cf_clearance is IP-bound)."""
    from manga_gateway.app import create_app

    app = create_app(
        _settings(
            cloudflare_proxy_server=_FAKE_SERVER,
            cloudflare_proxy_username=_FAKE_USER,
            cloudflare_proxy_password=_FAKE_PASS,
        )
    )
    async with app.router.lifespan_context(app):
        # Phase 10: app.state.solver is a SolverRouter; the proxy threads into its
        # Patchright leg (the browser solver). The Android leg reaches its sidecar
        # over the docker-internal network and takes no Playwright proxy dict.
        assert app.state.solver._patchright._proxy == {
            "server": _FAKE_SERVER,
            "username": _FAKE_USER,
            "password": _FAKE_PASS,
        }


async def test_lifespan_no_proxy_leaves_solver_proxy_none() -> None:
    """No proxy configured ⇒ solver receives no proxy (``_proxy is None``)."""
    from manga_gateway.app import create_app

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        # Phase 10: proxy lives on the router's Patchright leg (see above).
        assert app.state.solver._patchright._proxy is None


async def test_lifespan_threads_same_proxy_into_android_solver() -> None:
    """Req 7: the lifespan feeds the SAME build_proxy Playwright dict into the
    router's Android leg, so the sidecar /solve egress matches the httpx-fetch
    egress for the minted clearance."""
    from manga_gateway.app import create_app

    app = create_app(
        _settings(
            cloudflare_proxy_server=_FAKE_SERVER,
            cloudflare_proxy_username=_FAKE_USER,
            cloudflare_proxy_password=_FAKE_PASS,
        )
    )
    async with app.router.lifespan_context(app):
        # The Android leg carries the same dict the Patchright leg does — one
        # build_proxy call feeds both (no second call, no new setting).
        # Single-lane collapse: the sole "default" lane is the android backend.
        assert app.state.solver._android_by_lane["default"]._proxy == {
            "server": _FAKE_SERVER,
            "username": _FAKE_USER,
            "password": _FAKE_PASS,
        }


async def test_lifespan_no_proxy_leaves_android_solver_proxy_none() -> None:
    """No proxy configured ⇒ the Android leg receives no proxy (``_proxy is
    None``) so its /solve body stays byte-for-byte today (D-08)."""
    from manga_gateway.app import create_app

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        assert app.state.solver._android_by_lane["default"]._proxy is None


# ─────────── Phase 16 Task 1: R1 pinned-proxy singleton + read-only dep ───────────


async def test_lifespan_builds_source_pinned_proxies_once() -> None:
    """PROXY-03/R1: ``app.state.source_pinned_proxies`` is built ONCE in lifespan and
    has stable identity across two reads (never reconstructed per-request)."""
    from manga_gateway.app import create_app
    from manga_gateway.framework.source_pin import SourcePinnedProxies

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        first = app.state.source_pinned_proxies
        second = app.state.source_pinned_proxies
        assert isinstance(first, SourcePinnedProxies)
        assert first is second  # one identity — built once, never per-request


def test_get_source_pinned_proxies_only_reads_never_constructs() -> None:
    """PLAT-02: the dep READS ``request.app.state.source_pinned_proxies`` — when the
    attr is present it returns THAT object (no new construction)."""
    from types import SimpleNamespace

    from manga_gateway.deps import get_source_pinned_proxies
    from manga_gateway.framework.source_pin import SourcePinnedProxies

    sentinel = SourcePinnedProxies(None)
    fake_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(source_pinned_proxies=sentinel))
    )
    out = get_source_pinned_proxies(fake_request)  # type: ignore[arg-type]
    assert out is sentinel  # returned verbatim — read-only, no reconstruction


def test_get_source_pinned_proxies_tolerates_unwired_app() -> None:
    """A pre-wired / unit app (attr absent) falls back to a pool-less singleton so unit
    apps stay green — every method returns None, search/solve egress unchanged."""
    from types import SimpleNamespace

    from manga_gateway.deps import get_source_pinned_proxies
    from manga_gateway.framework.source_pin import SourcePinnedProxies

    fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    out = get_source_pinned_proxies(fake_request)  # type: ignore[arg-type]
    assert isinstance(out, SourcePinnedProxies)
    assert out.current("mangadot") is None  # pool-less ⇒ no pin


# ─────────── Phase 16 Task 1: is_origin_block_fn threaded into ctx sites ───────────


def test_ctx_construction_threads_is_origin_block_for_opted_in_source() -> None:
    """The bound ``is_origin_block`` predicate reaches ``SourceContext`` for an opted-in
    source (the route/engine thread it; Task 3 only CALLS it). A non-opted source (no
    such method) threads ``None`` — both via getattr, no source named by key."""
    from manga_gateway.framework.context import SourceContext, is_cf_challenge
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager
    from manga_gateway.framework.source_pin import SourcePinnedProxies
    from manga_gateway.handles.store import HandleStore

    class _OptedSource:
        key = "mangadot"
        solve_search_via_proxy_pool = True

        def is_origin_block(self, resp: httpx.Response) -> bool:
            return resp.status_code == 403 and not is_cf_challenge(resp)

    class _PlainSource:
        key = "mangadex"

    pins = SourcePinnedProxies(None)

    def _build(src: object) -> SourceContext:
        # Mirror the route/engine getattr threading exactly.
        return SourceContext(
            source_key=src.key,  # type: ignore[attr-defined]
            rate_limit_per_minute=6000,
            session=SessionManager(_RecordingTransport()),  # type: ignore[arg-type]
            ratelimiter=RateLimiter(),
            handle_store=HandleStore(),
            source_pins=pins,
            solve_search_via_proxy_pool=getattr(
                src, "solve_search_via_proxy_pool", False
            ),
            is_origin_block_fn=getattr(src, "is_origin_block", None),
        )

    opted = _build(_OptedSource())
    assert opted._source_pins is pins
    assert opted._solve_search_via_proxy_pool is True
    # The predicate reached ctx and classifies an origin-403 as a block.
    origin_403 = httpx.Response(403, request=httpx.Request("GET", "https://x/"))
    assert opted._is_origin_block_fn is not None
    assert opted._is_origin_block_fn(origin_403) is True

    plain = _build(_PlainSource())
    assert plain._solve_search_via_proxy_pool is False
    assert plain._is_origin_block_fn is None  # non-opted ⇒ predicate is None


class _RecordingTransport:
    """Minimal transport so the ctx-threading test can build a SourceContext."""

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request(method, url))

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


# ─────────── Phase 16 Task 3: /solve body carries the pinned proxy override ───────────


import json  # noqa: E402

import respx  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from manga_gateway.framework.android_solver import AndroidSolver  # noqa: E402
from manga_gateway.framework.proxy_pool import PooledProxy  # noqa: E402

_SIDECAR = "http://android-solver.invalid:8191"
_SOLVE_PIN_PASS = "NOTAREALSECRET-pin9"  # distinctive sentinel, never a real credential
_FAKE_SERVER_HOST = "proxy.invalid"


class _OnePin:
    """A SourcePinnedProxies-shaped fake exposing one PEEKED pin for a source."""

    def __init__(self, pin: PooledProxy | None) -> None:
        self._pin = pin

    def current(self, source_key: str) -> PooledProxy | None:
        return self._pin


def _solve_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "cf_clearance": "android-minted-token",
            "user_agent": "Mozilla/5.0 (Linux; Android 11) Chrome/120 Mobile",
            "host": "mangadot.net",
            "cf_clearance_expires": 2000000000.0,
        },
    )


def _android(**over: object) -> AndroidSolver:
    kwargs: dict[str, object] = {
        "base_url": _SIDECAR,
        "api_key": SecretStr("sidecar-secret"),
        "challenge_urls": {"mangadot": "https://mangadot.net/"},
        "timeout_s": 5.0,
    }
    kwargs.update(over)
    return AndroidSolver(**kwargs)  # type: ignore[arg-type]


@respx.mock
async def test_solve_body_carries_pinned_proxy_for_opted_in_source() -> None:
    """PROXY-04/PROXY-06: an opted-in source's /solve body OVERRIDES the global proxy
    with the source's pinned ``as_solve_dict()`` so the solve egress matches the
    search/image httpx egress (one IP)."""
    bodies: list[dict[str, object]] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _solve_response()

    respx.post(f"{_SIDECAR}/solve").mock(side_effect=_capture)

    pin = PooledProxy(_FAKE_SERVER_HOST, 9000, _FAKE_USER, SecretStr(_SOLVE_PIN_PASS))
    solver = _android(
        proxy={"server": "http://global.invalid:1"},  # the global to be overridden
        source_pins=_OnePin(pin),
        solve_search_keys={"mangadot"},
    )
    try:
        await solver._solve("mangadot")  # type: ignore[attr-defined]
    finally:
        await solver.aclose()

    assert len(bodies) == 1
    # The pinned dict OVERRODE the global proxy.
    assert bodies[0]["proxy"] == pin.as_solve_dict()
    assert bodies[0]["proxy"] != {"server": "http://global.invalid:1"}


@respx.mock
async def test_solve_body_keeps_global_for_non_opted_source() -> None:
    """PROXY-06/D-07: a source NOT in the opted-in key set keeps the global proxy (no
    override) — byte-for-byte today."""
    bodies: list[dict[str, object]] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _solve_response()

    respx.post(f"{_SIDECAR}/solve").mock(side_effect=_capture)

    pin = PooledProxy(_FAKE_SERVER_HOST, 9000, _FAKE_USER, SecretStr(_SOLVE_PIN_PASS))
    solver = _android(
        proxy={"server": "http://global.invalid:1"},
        source_pins=_OnePin(pin),
        solve_search_keys=set(),  # mangadot NOT opted in
    )
    try:
        await solver._solve("mangadot")  # type: ignore[attr-defined]
    finally:
        await solver.aclose()

    assert bodies[0]["proxy"] == {"server": "http://global.invalid:1"}


@respx.mock
async def test_solve_body_proxy_password_never_logged(caplog: Any) -> None:
    """T-16-03: the pinned proxy dict carries creds but the password sentinel never
    appears in ANY log record (host:port identity only)."""
    respx.post(f"{_SIDECAR}/solve").mock(side_effect=lambda r: _solve_response())

    pin = PooledProxy(_FAKE_SERVER_HOST, 9000, _FAKE_USER, SecretStr(_SOLVE_PIN_PASS))
    solver = _android(source_pins=_OnePin(pin), solve_search_keys={"mangadot"})
    try:
        with caplog.at_level(logging.DEBUG, logger="manga_gateway"):
            await solver._solve("mangadot")  # type: ignore[attr-defined]
    finally:
        await solver.aclose()

    for record in caplog.records:
        assert _SOLVE_PIN_PASS not in record.getMessage()
