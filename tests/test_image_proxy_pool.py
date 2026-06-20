"""Unit tests for the ``ProxyPool`` framework component (260620-4im, Task 1).

Offline + deterministic, fake-credential sentinels ONLY (mirror
``tests/test_proxy_wiring.py``): no real proxy host or credential anywhere. Covers
identity-excludes-creds, ``httpx_proxy`` auth-tuple + password-not-in-URL + SecretStr
repr redaction, ``from_file`` parse/skip/None, ``acquire`` exclude + cooldown +
None-when-exhausted (injected fake ``clock``), ``mark_failed`` cooldown + post-deadline
re-availability, and ``transport_for`` per-identity caching + ``aclose`` closes all.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from manga_gateway.config import Settings
from manga_gateway.framework.proxy_pool import PooledProxy, ProxyPool

TEST_API_KEY = "test-key-deterministic-0123456789"
# Obviously-fake values — NEVER a real proxy credential (T-4im-01). The password is a
# distinctive sentinel (no substring of any other field) so redaction asserts can't
# false-pass on an incidental collision.
_FAKE_HOST = "proxy.invalid"
_FAKE_USER = "fakeuser"
_FAKE_PASS = "NOTAREALSECRET-zzz9"


def _settings() -> Settings:
    return Settings(api_key=TEST_API_KEY)  # type: ignore[call-arg]


class _FakeClock:
    """A monotonic clock stub whose value the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeTransport:
    """Records that ``aclose`` was awaited exactly once."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = 0

    async def request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:  # pragma: no cover - interface completeness
        raise NotImplementedError

    async def aclose(self) -> None:
        self.closed += 1


# ───────────────────────────── PooledProxy ─────────────────────────────


def test_identity_is_host_port_only_never_creds() -> None:
    proxy = PooledProxy(
        host=_FAKE_HOST, port=8080, username=_FAKE_USER, password=SecretStr(_FAKE_PASS)
    )
    assert proxy.identity == f"{_FAKE_HOST}:8080"
    assert _FAKE_USER not in proxy.identity
    assert _FAKE_PASS not in proxy.identity


def test_httpx_proxy_with_creds_keeps_password_out_of_url() -> None:
    proxy = PooledProxy(
        host=_FAKE_HOST, port=8080, username=_FAKE_USER, password=SecretStr(_FAKE_PASS)
    )
    hx = proxy.httpx_proxy()
    assert isinstance(hx, httpx.Proxy)
    assert str(hx.url) == f"http://{_FAKE_HOST}:8080"
    assert _FAKE_PASS not in str(hx.url)
    assert hx.auth == (_FAKE_USER, _FAKE_PASS)


def test_httpx_proxy_without_creds_is_plain_url() -> None:
    proxy = PooledProxy(host=_FAKE_HOST, port=8080)
    assert proxy.httpx_proxy() == f"http://{_FAKE_HOST}:8080"


def test_secretstr_password_redacted_in_repr() -> None:
    proxy = PooledProxy(
        host=_FAKE_HOST, port=8080, username=_FAKE_USER, password=SecretStr(_FAKE_PASS)
    )
    assert _FAKE_PASS not in repr(proxy)
    assert _FAKE_PASS not in str(proxy)


# ───────────────────────────── from_file ─────────────────────────────


def test_from_file_parses_skips_and_builds(tmp_path: Path) -> None:
    f = tmp_path / "proxies.txt"
    f.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                f"{_FAKE_HOST}:8080:{_FAKE_USER}:{_FAKE_PASS}",
                f"{_FAKE_HOST}:9090",  # no-creds 2-part line
                f"{_FAKE_HOST}:notaport:u:p",  # malformed port → skipped
                "only-three:1:2",  # wrong part count → skipped
            ]
        ),
        encoding="utf-8",
    )
    pool = ProxyPool.from_file(f, settings=_settings(), cooldown_seconds=300.0)
    assert pool is not None
    assert pool.available_count() == 2
    # The creds line round-trips into an authed httpx proxy; the no-creds line plain.
    first = pool.acquire()
    assert first is not None


def test_from_file_missing_returns_none(tmp_path: Path) -> None:
    assert (
        ProxyPool.from_file(
            tmp_path / "nope.txt", settings=_settings(), cooldown_seconds=300.0
        )
        is None
    )


def test_from_file_zero_valid_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "proxies.txt"
    f.write_text("# only comments\n\nbad-line\n", encoding="utf-8")
    assert ProxyPool.from_file(f, settings=_settings(), cooldown_seconds=300.0) is None


# ───────────────────────────── acquire / cooldown ─────────────────────────────


def _pool(n: int, *, clock: _FakeClock, cooldown: float = 300.0) -> ProxyPool:
    proxies = [PooledProxy(host=_FAKE_HOST, port=8000 + i) for i in range(n)]
    return ProxyPool(
        proxies,
        settings=_settings(),
        cooldown_seconds=cooldown,
        rng=random.Random(0),
        clock=clock,
    )


def test_acquire_excludes_selection_keys() -> None:
    clock = _FakeClock()
    proxies = [PooledProxy(host=_FAKE_HOST, port=8000 + i) for i in range(3)]
    pool = ProxyPool(
        proxies,
        settings=_settings(),
        cooldown_seconds=300.0,
        rng=random.Random(0),
        clock=clock,
    )
    exclude = {proxies[0].selection_key, proxies[1].selection_key}
    got = pool.acquire(exclude=exclude)
    assert got is not None
    assert got.identity == f"{_FAKE_HOST}:8002"


def test_acquire_returns_none_when_all_excluded() -> None:
    clock = _FakeClock()
    proxies = [PooledProxy(host=_FAKE_HOST, port=8000 + i) for i in range(2)]
    pool = ProxyPool(
        proxies,
        settings=_settings(),
        cooldown_seconds=300.0,
        rng=random.Random(0),
        clock=clock,
    )
    exclude = {p.selection_key for p in proxies}
    assert pool.acquire(exclude=exclude) is None


def test_selection_key_distinguishes_same_host_port_different_creds() -> None:
    """Sticky-session proxies share host:port but differ by username — cooldown and the
    transport cache must NOT collide them (CodeRabbit, credential-distinct key)."""
    clock = _FakeClock()
    a = PooledProxy(
        host=_FAKE_HOST, port=7777, username="session-a", password=SecretStr(_FAKE_PASS)
    )
    b = PooledProxy(
        host=_FAKE_HOST, port=7777, username="session-b", password=SecretStr(_FAKE_PASS)
    )
    assert a.identity == b.identity  # same host:port (would collide on identity)
    assert a.selection_key != b.selection_key  # but distinct bookkeeping keys
    transports: list[_FakeTransport] = []

    def _factory(*args: Any, **kwargs: Any) -> _FakeTransport:
        t = _FakeTransport(*args, **kwargs)
        transports.append(t)
        return t

    pool = ProxyPool(
        [a, b],
        settings=_settings(),
        cooldown_seconds=300.0,
        transport_factory=_factory,
        rng=random.Random(0),
        clock=clock,
    )
    # Distinct per-credential transports (not one shared by host:port).
    assert pool.transport_for(a) is not pool.transport_for(b)
    assert len(transports) == 2
    # Cooling down ``a`` must NOT sideline ``b`` (same host:port, different creds).
    pool.mark_failed(a)
    assert pool.acquire(exclude=set()) is b


def test_mark_failed_cools_down_then_reappears_after_deadline() -> None:
    clock = _FakeClock()
    pool = _pool(1, clock=clock, cooldown=300.0)
    only = PooledProxy(host=_FAKE_HOST, port=8000)
    pool.mark_failed(only)
    # On cooldown → unavailable.
    assert pool.acquire() is None
    # Before the deadline → still unavailable.
    clock.now += 299.0
    assert pool.acquire() is None
    # Past the deadline → available again.
    clock.now += 2.0
    got = pool.acquire()
    assert got is not None
    assert got.identity == f"{_FAKE_HOST}:8000"


def test_mark_failed_logs_identity_not_creds(caplog: pytest.LogCaptureFixture) -> None:
    clock = _FakeClock()
    pool = ProxyPool(
        [PooledProxy(_FAKE_HOST, 8000, _FAKE_USER, SecretStr(_FAKE_PASS))],
        settings=_settings(),
        cooldown_seconds=300.0,
        rng=random.Random(0),
        clock=clock,
    )
    with caplog.at_level(logging.INFO):
        pool.mark_failed(
            PooledProxy(_FAKE_HOST, 8000, _FAKE_USER, SecretStr(_FAKE_PASS))
        )
    for record in caplog.records:
        assert _FAKE_PASS not in record.getMessage()
        assert _FAKE_USER not in record.getMessage()
    assert any(f"{_FAKE_HOST}:8000" in r.getMessage() for r in caplog.records)


# ───────────────────────────── transport cache ─────────────────────────────


async def test_transport_for_caches_per_identity_and_aclose_closes_all() -> None:
    clock = _FakeClock()
    built: list[_FakeTransport] = []

    def _factory(settings: Any, **kwargs: Any) -> _FakeTransport:
        t = _FakeTransport(settings, **kwargs)
        built.append(t)
        return t

    proxies = [
        PooledProxy(_FAKE_HOST, 8000, _FAKE_USER, SecretStr(_FAKE_PASS)),
        PooledProxy(_FAKE_HOST, 8001),
    ]
    pool = ProxyPool(
        proxies,
        settings=_settings(),
        cooldown_seconds=300.0,
        transport_factory=_factory,
        rng=random.Random(0),
        clock=clock,
    )
    t0a = pool.transport_for(proxies[0])
    t0b = pool.transport_for(proxies[0])  # cache HIT — same object
    t1 = pool.transport_for(proxies[1])
    assert t0a is t0b
    assert t0a is not t1
    assert len(built) == 2
    # The per-proxy transport receives the proxy override (creds → httpx.Proxy).
    assert isinstance(t0a.kwargs.get("proxy_override"), httpx.Proxy)
    await pool.aclose()
    assert all(t.closed == 1 for t in built)
