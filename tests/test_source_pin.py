"""Unit tests for the per-source search+solve proxy surfaces (Phase 16, Plan 01).

Offline + deterministic, fake-credential sentinels ONLY (mirror
``tests/test_image_proxy_pool.py``): no real proxy host or credential anywhere.

Covers the three NEW surfaces this plan introduces, with NO wiring into the hot
request paths (that is Plan 02):

* ``PooledProxy.as_solve_dict()`` — the one new ``SecretStr`` unpack site, producing
  the android ``/solve``-body proxy dict (PROXY-04 solve leg, PROXY-06, T-4im-01);
* ``SourcePinnedProxies`` — the R1 singleton that pins ONE residential proxy per
  source and delegates rotate/cooldown to the shared ``ProxyPool`` (PROXY-04/05/06,
  D-04); pin/peek/rotate/exhaustion/None-pool + the credential-never-logged assertion;
* ``Source.solve_search_via_proxy_pool`` + ``Source.is_origin_block`` — the declarative
  opt-in attr and the overridable origin-block rotation trigger (PROXY-02/07, D-08).
"""

from __future__ import annotations

import logging
import random

import httpx
import pytest
from pydantic import SecretStr

from manga_gateway.config import Settings
from manga_gateway.framework.base import Source
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.proxy_pool import PooledProxy, ProxyPool
from manga_gateway.framework.source_pin import SourcePinnedProxies
from manga_gateway.models.search import Release, SearchRequest

TEST_API_KEY = "test-key-deterministic-0123456789"
# Obviously-fake values — NEVER a real proxy credential (T-4im-01). The password is a
# distinctive sentinel (no substring of any other field) so redaction/log asserts can't
# false-pass on an incidental collision.
_FAKE_HOST = "proxy.invalid"
_FAKE_USER = "fakeuser"
_FAKE_PASS = "NOTAREALSECRET-zzz9"


def _settings() -> Settings:
    return Settings(api_key=TEST_API_KEY)  # type: ignore[call-arg]


# ───────────────────────── PooledProxy.as_solve_dict (Task 1) ─────────────────────────


def test_as_solve_dict_with_creds_has_server_username_password() -> None:
    proxy = PooledProxy(
        host=_FAKE_HOST, port=8080, username=_FAKE_USER, password=SecretStr(_FAKE_PASS)
    )
    d = proxy.as_solve_dict()
    assert set(d) == {"server", "username", "password"}
    assert d["server"] == f"http://{_FAKE_HOST}:8080"
    assert d["server"].startswith("http://")
    assert d["username"] == _FAKE_USER
    assert d["password"] == _FAKE_PASS


def test_as_solve_dict_without_creds_is_server_only() -> None:
    proxy = PooledProxy(host=_FAKE_HOST, port=9090)
    d = proxy.as_solve_dict()
    assert set(d) == {"server"}
    assert d["server"] == f"http://{_FAKE_HOST}:9090"


def test_as_solve_dict_username_only_is_server_only() -> None:
    # Auth present iff BOTH a username and a password are set (mirrors build_proxy).
    proxy = PooledProxy(host=_FAKE_HOST, port=9090, username=_FAKE_USER)
    assert set(proxy.as_solve_dict()) == {"server"}


def test_as_solve_dict_does_not_change_redaction() -> None:
    proxy = PooledProxy(
        host=_FAKE_HOST, port=8080, username=_FAKE_USER, password=SecretStr(_FAKE_PASS)
    )
    # Calling the new accessor does not leak the secret into repr/str.
    _ = proxy.as_solve_dict()
    assert _FAKE_PASS not in repr(proxy)
    assert _FAKE_PASS not in str(proxy)


# ───────────────────────── SourcePinnedProxies (Task 2) ─────────────────────────


def _pool(n: int) -> ProxyPool:
    """A real ``ProxyPool`` over ``n`` fake-credential (no real proxy) entries."""
    proxies = [PooledProxy(host=_FAKE_HOST, port=8000 + i) for i in range(n)]
    return ProxyPool(
        proxies,
        settings=_settings(),
        cooldown_seconds=300.0,
        rng=random.Random(0),
    )


def test_get_or_acquire_pins_once_and_returns_same_proxy() -> None:
    pins = SourcePinnedProxies(_pool(3))
    first = pins.get_or_acquire("mangadot")
    assert first is not None
    # Subsequent calls return the SAME pinned proxy without re-acquiring.
    assert pins.get_or_acquire("mangadot") is first
    assert pins.get_or_acquire("mangadot") is first


def test_current_peeks_without_acquiring() -> None:
    pins = SourcePinnedProxies(_pool(3))
    assert pins.current("mangadot") is None  # nothing pinned yet
    acquired = pins.get_or_acquire("mangadot")
    assert pins.current("mangadot") is acquired


def test_rotate_marks_failed_and_acquires_a_different_excluded_proxy() -> None:
    pins = SourcePinnedProxies(_pool(3))
    first = pins.get_or_acquire("mangadot")
    assert first is not None
    rotated = pins.rotate("mangadot", exclude={first.selection_key})
    assert rotated is not None
    assert rotated.selection_key != first.selection_key
    # The new proxy is now the pin.
    assert pins.current("mangadot") is rotated


def test_rotate_returns_none_on_exhaustion_and_leaves_no_pin() -> None:
    pins = SourcePinnedProxies(_pool(1))
    first = pins.get_or_acquire("mangadot")
    assert first is not None
    # Only proxy is excluded → acquire returns None → no pin remains.
    assert pins.rotate("mangadot", exclude={first.selection_key}) is None
    assert pins.current("mangadot") is None


def test_get_or_acquire_returns_none_when_pool_empty() -> None:
    pins = SourcePinnedProxies(_pool(0))
    assert pins.get_or_acquire("mangadot") is None
    assert pins.current("mangadot") is None


def test_none_pool_returns_none_for_all_methods() -> None:
    pins = SourcePinnedProxies(None)
    assert pins.get_or_acquire("mangadot") is None
    assert pins.current("mangadot") is None
    assert pins.rotate("mangadot", exclude=set()) is None


def test_per_source_isolation() -> None:
    pins = SourcePinnedProxies(_pool(3))
    a = pins.get_or_acquire("source_a")
    b = pins.get_or_acquire("source_b")
    assert a is not None and b is not None
    # Each source has its OWN independent pin.
    assert pins.current("source_a") is a
    assert pins.current("source_b") is b
    # Rotating A does not touch B.
    pins.rotate("source_a", exclude={a.selection_key})
    assert pins.current("source_b") is b


def test_rotate_cycle_logs_identity_never_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = ProxyPool(
        [
            PooledProxy(
                host=_FAKE_HOST,
                port=8000 + i,
                username=_FAKE_USER,
                password=SecretStr(_FAKE_PASS),
            )
            for i in range(3)
        ],
        settings=_settings(),
        cooldown_seconds=300.0,
        rng=random.Random(0),
    )
    pins = SourcePinnedProxies(pool)
    with caplog.at_level(logging.INFO, logger="manga_gateway"):
        first = pins.get_or_acquire("mangadot")
        assert first is not None
        pins.rotate("mangadot", exclude={first.selection_key})
    # host:port identity surfaces in the cooldown log; the password never does.
    assert any(_FAKE_HOST in rec.getMessage() for rec in caplog.records)
    for rec in caplog.records:
        assert _FAKE_PASS not in rec.getMessage()


# ──────── Source.solve_search_via_proxy_pool / is_origin_block (Task 3) ────────


class _DummySource(Source):
    """A minimal concrete ``Source`` for default-attr / hook assertions."""

    key = "dummy"
    name = "Dummy"
    base_url = "https://dummy.invalid"
    id_types = ["slug"]
    languages = ["en"]
    rate_limit_per_minute = 30

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        return []

    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        return []

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        return []

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        return b""


def test_solve_search_via_proxy_pool_defaults_false() -> None:
    assert _DummySource().solve_search_via_proxy_pool is False


def _resp(
    status: int, *, headers: dict[str, str] | None = None, body: bytes = b""
) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, content=body)


def test_is_origin_block_true_for_plain_403() -> None:
    # A 403 with no CF challenge markers is an origin reputation-block → rotate.
    assert _DummySource().is_origin_block(_resp(403)) is True


def test_is_origin_block_false_for_cf_challenge_403() -> None:
    cf = _resp(403, headers={"server": "cloudflare", "cf-mitigated": "challenge"})
    assert _DummySource().is_origin_block(cf) is False


def test_is_origin_block_false_for_non_403() -> None:
    assert _DummySource().is_origin_block(_resp(200)) is False
    assert _DummySource().is_origin_block(_resp(500)) is False
