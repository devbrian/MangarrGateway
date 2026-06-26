"""Regression + proxied-path + credential-non-leak tests (260620-4im, Task 5).

Three guarantees, all offline with fake credentials only:

1. **Pool unset ⇒ DIRECT path byte-for-byte unchanged** — with no pool wired a download
   image fetch egresses through ``download_transport``, ``_active_proxy`` is never set,
   no proxy machinery is touched (the hard regression contract).
2. **Proxied path proven end-to-end (respx)** — a real ``ProxyPool`` (fake
   ``proxies.txt``) + real per-proxy ``HttpxTransport``: an image fetch returns bytes
   via the pool path, respx intercepting the httpx request.
3. **Security** — a distinctive fake password sentinel never appears in any log record,
   warning, or raised error across the proxied + rotation paths; the logged proxy
   identity is ``host:port`` only.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

import httpx
import respx
from pydantic import SecretStr

from manga_gateway.config import Settings
from manga_gateway.framework.context import _ACTIVE_PROXY, SourceContext
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.proxy_pool import PooledProxy, ProxyPool
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.framework.source_pin import SourcePinnedProxies
from manga_gateway.handles.store import HandleStore

TEST_API_KEY = "test-key-deterministic-0123456789"
_HOST = "proxy.invalid"
_FAKE_USER = "fakeuser"
_FAKE_PASS = "NOTAREALSECRET-zzz9"  # distinctive sentinel (T-4im-01)
_CDN_URL = "https://o48.mfcdn1.xyz/mf/abcdef0123/h/0.jpg"
_IMG = b"\xff\xd8\xff\xe0REALIMAGE"


def _settings() -> Settings:
    return Settings(api_key=TEST_API_KEY)  # type: ignore[call-arg]


class _RecordingTransport:
    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self._responses = responses or []
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append(url)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(200, content=_IMG, request=httpx.Request(method, url))

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


# ───────────────────────── (1) regression: pool unset = DIRECT ─────────────────────


async def test_pool_unset_download_egresses_direct() -> None:
    search = _RecordingTransport()
    download = _RecordingTransport()  # default path returns _IMG with request set
    ctx = SourceContext(
        source_key="mangafire",
        rate_limit_per_minute=6000,
        session=SessionManager(search, download),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        use_download_transport=True,
        proxy_pool=None,  # UNCONFIGURED — the regression contract
        image_via_proxy_pool=False,
    )
    calls = 0

    async def _fetch() -> bytes:
        nonlocal calls
        calls += 1
        return await ctx.get_bytes(_CDN_URL)

    out = await ctx.fetch_image_via_pool(_fetch)
    assert out == _IMG
    assert calls == 1  # transparent passthrough — fetch ran exactly once
    assert _ACTIVE_PROXY.get() is None  # no proxy machinery touched
    # The DIRECT download pool received the request; the search pool did not.
    assert download.calls == [_CDN_URL]
    assert search.calls == []


# ───────────────────────── (2) proxied path end-to-end (respx) ─────────────────────


@respx.mock
async def test_proxied_path_returns_bytes_via_pool(tmp_path: Path) -> None:
    pf = tmp_path / "proxies.txt"
    pf.write_text(f"{_HOST}:8080:{_FAKE_USER}:{_FAKE_PASS}\n", encoding="utf-8")
    pool = ProxyPool.from_file(pf, settings=_settings(), cooldown_seconds=300.0)
    assert pool is not None  # real pool with a real per-proxy HttpxTransport

    ctx = SourceContext(
        source_key="mangafire",
        rate_limit_per_minute=6000,
        session=SessionManager(_RecordingTransport()),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        proxy_pool=pool,
        image_via_proxy_pool=True,
    )
    route = respx.get(_CDN_URL).mock(
        return_value=httpx.Response(
            200, content=_IMG, headers={"content-type": "image/jpeg"}
        )
    )
    try:
        out = await ctx.fetch_image_via_pool(lambda: ctx.get_bytes(_CDN_URL))
        assert out == _IMG
        assert route.called
    finally:
        await pool.aclose()


# ───────────────────────── (3) security: no credential leak ─────────────────────


class _StatusTransport:
    """Returns a fixed status; 200 carries the image, else a deny body."""

    def __init__(self, settings: Any, *, proxy_override: Any, status: int) -> None:
        self.status = status

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        content = _IMG if self.status == 200 else b"deny"
        return httpx.Response(
            self.status, content=content, request=httpx.Request(method, url)
        )

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _OrderedRng:
    """A deterministic ``choice`` that always returns the first available proxy."""

    def choice(self, seq: list[PooledProxy]) -> PooledProxy:
        return seq[0]


def _creds_proxies() -> list[PooledProxy]:
    return [
        PooledProxy(_HOST, 8000, _FAKE_USER, SecretStr(_FAKE_PASS)),
        PooledProxy(_HOST, 8001, _FAKE_USER, SecretStr(_FAKE_PASS)),
    ]


async def test_rotation_logs_identity_never_credentials(
    caplog: Any,
) -> None:
    statuses = iter([403, 200])  # proxy 0 fails, proxy 1 succeeds

    def _factory(settings: Any, *, proxy_override: Any) -> _StatusTransport:
        return _StatusTransport(
            settings, proxy_override=proxy_override, status=next(statuses)
        )

    pool = ProxyPool(
        _creds_proxies(),
        settings=_settings(),
        cooldown_seconds=300.0,
        transport_factory=_factory,
        rng=_OrderedRng(),  # type: ignore[arg-type]
    )
    ctx = SourceContext(
        source_key="mangafire",
        rate_limit_per_minute=6000,
        session=SessionManager(_RecordingTransport()),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        proxy_pool=pool,
        image_via_proxy_pool=True,
    )

    with caplog.at_level(logging.DEBUG, logger="manga_gateway"):
        out = await ctx.fetch_image_via_pool(lambda: ctx.get_bytes(_CDN_URL))
    assert out == _IMG  # rotated from the 403 proxy to the clean one

    messages = [r.getMessage() for r in caplog.records]
    # The distinctive password never appears in ANY log record.
    for msg in messages:
        assert _FAKE_PASS not in msg
        assert _FAKE_USER not in msg
    # No source warning leaked a credential either.
    assert all(_FAKE_PASS not in code + text for code, text in ctx.warnings)
    # Observability still works WITHOUT leaking creds: a host:port identity is logged.
    assert any(f"{_HOST}:8000" in msg for msg in messages)


# ─────── Phase 16 STEP B: non-opted source unchanged after the rename + pinning ───────


async def test_non_opted_source_transport_pick_unchanged_after_rename() -> None:
    """The contextvar rename + the opted-in search-leg pinning must NOT touch a source
    that did NOT opt in: with ``solve_search_via_proxy_pool`` False (the default) a
    plain ``get_bytes`` egresses the SEARCH transport, ``_ACTIVE_PROXY`` is never set,
    and the pinned-proxy singleton acquires nothing — byte-for-byte today (the #65 /
    260620-4im regression contract for the search dimension)."""
    search = _RecordingTransport()
    download = _RecordingTransport()
    pf_pool = None  # no pin involvement at all on the non-opted path
    ctx = SourceContext(
        source_key="mangadex",
        rate_limit_per_minute=6000,
        session=SessionManager(search, download),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        # A pinned-proxy singleton IS threaded (as in production), but the source did
        # NOT opt in, so it must be ignored entirely.
        source_pins=SourcePinnedProxies(pf_pool),
        solve_search_via_proxy_pool=False,
    )

    out = await ctx.get_bytes(_CDN_URL)

    assert out == _IMG
    assert _ACTIVE_PROXY.get() is None  # never set for a non-opted source
    # The SEARCH transport served it (no download_transport, no proxy pool).
    assert len(search.calls) == 1
    assert download.calls == []


async def test_exhaustion_error_carries_no_credentials() -> None:
    def _factory(settings: Any, *, proxy_override: Any) -> _StatusTransport:
        return _StatusTransport(settings, proxy_override=proxy_override, status=403)

    pool = ProxyPool(
        [PooledProxy(_HOST, 8000, _FAKE_USER, SecretStr(_FAKE_PASS))],
        settings=_settings(),
        cooldown_seconds=300.0,
        transport_factory=_factory,
        rng=random.Random(0),
    )
    ctx = SourceContext(
        source_key="mangafire",
        rate_limit_per_minute=6000,
        session=SessionManager(_RecordingTransport()),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        proxy_pool=pool,
        image_via_proxy_pool=True,
    )
    try:
        await ctx.fetch_image_via_pool(lambda: ctx.get_bytes(_CDN_URL))
    except SourceError as exc:
        assert _FAKE_PASS not in str(exc)
        assert _FAKE_USER not in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected SourceError")
