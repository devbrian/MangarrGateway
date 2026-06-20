"""Settings + HttpxTransport.proxy_override + lifespan wiring (260620-4im, Task 2).

Offline + deterministic, fake-credential sentinels ONLY (mirror
``tests/test_proxy_wiring.py``). Covers: empty-string ``image_proxy_pool_file`` → None;
field defaults; ``HttpxTransport(proxy_override=…)`` threads ``proxy=`` while the
no-override unconfigured path stays byte-for-byte today (regression); the lifespan
builds a ``ProxyPool`` only when configured and logs the COUNT (never creds).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from manga_gateway.config import Settings
from manga_gateway.framework.proxy_pool import ProxyPool
from manga_gateway.framework.transport import HttpxTransport

TEST_API_KEY = "test-key-deterministic-0123456789"
_FAKE_HOST = "proxy.invalid"
_FAKE_USER = "fakeuser"
_FAKE_PASS = "NOTAREALSECRET-zzz9"


def _settings(**over: object) -> Settings:
    return Settings(api_key=TEST_API_KEY, **over)  # type: ignore[arg-type]


# ───────────────────────────── settings ─────────────────────────────


def test_empty_image_proxy_pool_file_normalizes_to_none() -> None:
    # Unset CI secrets arrive as "" — must be treated as UNSET so the pool stays off.
    assert _settings(image_proxy_pool_file="").image_proxy_pool_file is None
    assert _settings(image_proxy_pool_file="   ").image_proxy_pool_file is None


def test_image_proxy_field_defaults() -> None:
    s = _settings()
    assert s.image_proxy_pool_file is None
    assert s.image_proxy_cooldown_seconds == 300.0
    assert s.image_proxy_max_attempts == 3


# ───────────────────────────── HttpxTransport.proxy_override ─────────────────────────


def test_transport_proxy_override_threads_proxy(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    real_init = httpx.AsyncClient.__init__

    def _spy_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy_init)

    override = httpx.Proxy(
        url=f"http://{_FAKE_HOST}:8080", auth=(_FAKE_USER, _FAKE_PASS)
    )
    HttpxTransport(_settings(), proxy_override=override)
    assert captured.get("proxy") is override


def test_transport_no_override_unconfigured_omits_proxy(monkeypatch: Any) -> None:
    """No override + unconfigured settings ⇒ NO ``proxy`` kwarg (regression guard)."""
    captured: dict[str, Any] = {}
    real_init = httpx.AsyncClient.__init__

    def _spy_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy_init)

    HttpxTransport(_settings())
    assert "proxy" not in captured


def test_transport_explicit_none_override_omits_proxy(monkeypatch: Any) -> None:
    """An explicit ``proxy_override=None`` still omits ``proxy=`` (no broken proxy)."""
    captured: dict[str, Any] = {}
    real_init = httpx.AsyncClient.__init__

    def _spy_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy_init)

    HttpxTransport(_settings(), proxy_override=None)
    assert "proxy" not in captured


# ───────────────────────────── lifespan ─────────────────────────────


async def test_lifespan_builds_pool_when_configured(
    tmp_path: Path, caplog: Any
) -> None:
    from manga_gateway.app import create_app

    pf = tmp_path / "proxies.txt"
    pf.write_text(
        "\n".join(
            [
                f"{_FAKE_HOST}:8080:{_FAKE_USER}:{_FAKE_PASS}",
                f"{_FAKE_HOST}:9090:{_FAKE_USER}:{_FAKE_PASS}",
            ]
        ),
        encoding="utf-8",
    )
    app = create_app(_settings(image_proxy_pool_file=str(pf)))
    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            pool = app.state.image_proxy_pool
            assert isinstance(pool, ProxyPool)
            assert pool.available_count() == 2
    # The startup log carries the COUNT only — never the fake credentials.
    for record in caplog.records:
        assert _FAKE_PASS not in record.getMessage()
        assert _FAKE_USER not in record.getMessage()


async def test_lifespan_no_pool_when_unconfigured() -> None:
    from manga_gateway.app import create_app

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        assert app.state.image_proxy_pool is None
