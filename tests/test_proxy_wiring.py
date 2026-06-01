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
