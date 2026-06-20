"""Framework orchestration — ``SourceContext.fetch_image_via_pool`` (260620-4im, T3).

Offline + deterministic, fake-credential sentinels only. A fake ``ProxyPool`` records
``acquire``/``mark_failed``/``transport_for``; a recording transport stands in for the
per-proxy egress (mirror ``tests/test_comix_fetch._RecordingTransport``). Covers:
pool-inactive passthrough, sticky reuse, rotate-on-``SourceError(403)``, rotate-on-httpx
error, exhaustion → last exc, no-proxy-available → terminal SourceError, and
``_active_proxy`` routing through the per-proxy transport (NOT download_transport).
"""

from __future__ import annotations

from typing import Any

import httpx

from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.proxy_pool import PooledProxy
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore

_HOST = "proxy.invalid"


class _RecordingTransport:
    """Fake Transport returning queued responses, recording each request."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _FakePool:
    """Records acquire/mark_failed/transport_for; cooled proxies are excluded."""

    def __init__(self, proxies: list[PooledProxy]) -> None:
        self._proxies = list(proxies)
        self.acquired: list[str] = []
        self.failed: list[str] = []
        self.transports: dict[str, _RecordingTransport] = {}
        self._cooled: set[str] = set()

    def acquire(self, exclude: set[str] | None = None) -> PooledProxy | None:
        excl = (exclude or set()) | self._cooled
        for proxy in self._proxies:
            if proxy.identity not in excl:
                self.acquired.append(proxy.identity)
                return proxy
        return None

    def mark_failed(self, proxy: PooledProxy) -> None:
        self.failed.append(proxy.identity)
        self._cooled.add(proxy.identity)

    def transport_for(self, proxy: PooledProxy) -> _RecordingTransport:
        return self.transports.setdefault(proxy.identity, _RecordingTransport([]))


def _ctx(
    pool: Any,
    *,
    image_via: bool = True,
    max_attempts: int = 3,
    transport: _RecordingTransport | None = None,
) -> SourceContext:
    return SourceContext(
        source_key="mangafire",
        rate_limit_per_minute=6000,
        session=SessionManager(transport or _RecordingTransport([])),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        proxy_pool=pool,
        image_via_proxy_pool=image_via,
        image_proxy_max_attempts=max_attempts,
    )


def _proxies(n: int) -> list[PooledProxy]:
    return [PooledProxy(host=_HOST, port=8000 + i) for i in range(n)]


# ───────────────────────────── passthrough ─────────────────────────────


async def test_pool_inactive_no_pool_is_passthrough() -> None:
    ctx = _ctx(None)
    calls = 0

    async def _fetch() -> bytes:
        nonlocal calls
        calls += 1
        return b"img"

    out = await ctx.fetch_image_via_pool(_fetch)
    assert out == b"img"
    assert calls == 1
    assert ctx._active_proxy is None


async def test_pool_present_but_not_opted_in_is_passthrough() -> None:
    pool = _FakePool(_proxies(2))
    ctx = _ctx(pool, image_via=False)

    async def _fetch() -> bytes:
        return b"img"

    out = await ctx.fetch_image_via_pool(_fetch)
    assert out == b"img"
    assert pool.acquired == []  # no proxy machinery touched
    assert ctx._active_proxy is None


# ───────────────────────────── sticky ─────────────────────────────


async def test_first_page_picks_sticky_later_pages_reuse_it() -> None:
    pool = _FakePool(_proxies(3))
    ctx = _ctx(pool)
    used: list[str | None] = []

    async def _fetch() -> bytes:
        used.append(ctx._active_proxy.identity if ctx._active_proxy else None)
        return b"img"

    await ctx.fetch_image_via_pool(_fetch)
    await ctx.fetch_image_via_pool(_fetch)
    # Exactly ONE acquire (the second page reuses the sticky), same proxy both times.
    assert len(pool.acquired) == 1
    assert used[0] == used[1] == pool.acquired[0]
    assert ctx._active_proxy is None  # cleared after each call


# ───────────────────────────── rotate ─────────────────────────────


async def test_rotate_on_source_error_403_then_succeed() -> None:
    pool = _FakePool(_proxies(2))
    ctx = _ctx(pool)
    attempts: list[str] = []

    async def _fetch() -> bytes:
        ident = ctx._active_proxy.identity  # type: ignore[union-attr]
        attempts.append(ident)
        if len(attempts) == 1:
            raise SourceError("source_unavailable", "upstream 403", status=403)
        return b"img"

    out = await ctx.fetch_image_via_pool(_fetch)
    assert out == b"img"
    # First proxy failed + cooled; a DIFFERENT proxy served + became sticky.
    assert pool.failed == [attempts[0]]
    assert attempts[1] != attempts[0]
    assert ctx._sticky_proxy is not None
    assert ctx._sticky_proxy.identity == attempts[1]


async def test_rotate_on_httpx_error() -> None:
    pool = _FakePool(_proxies(2))
    ctx = _ctx(pool)
    attempts: list[str] = []

    async def _fetch() -> bytes:
        attempts.append(ctx._active_proxy.identity)  # type: ignore[union-attr]
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("proxy connect timed out")
        return b"img"

    out = await ctx.fetch_image_via_pool(_fetch)
    assert out == b"img"
    assert len(pool.failed) == 1
    assert attempts[1] != attempts[0]


async def test_all_attempts_fail_raises_last_exc_and_marks_each() -> None:
    pool = _FakePool(_proxies(5))
    ctx = _ctx(pool, max_attempts=3)
    raised: list[SourceError] = []

    async def _fetch() -> bytes:
        exc = SourceError("source_unavailable", "upstream 403", status=403)
        raised.append(exc)
        raise exc

    try:
        await ctx.fetch_image_via_pool(_fetch)
    except SourceError as exc:
        assert exc is raised[-1]  # the LAST failure, unchanged
    else:  # pragma: no cover - must raise
        raise AssertionError("expected SourceError")
    assert len(pool.failed) == 3  # one per attempt
    assert ctx._active_proxy is None


async def test_no_proxy_available_raises_terminal_source_error() -> None:
    pool = _FakePool([])  # zero proxies → acquire returns None up front
    ctx = _ctx(pool)

    async def _fetch() -> bytes:  # pragma: no cover - never reached
        return b"img"

    try:
        await ctx.fetch_image_via_pool(_fetch)
    except SourceError as exc:
        assert exc.code == "source_unavailable"
        assert "no image proxy available" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected SourceError")
    assert pool.acquired == []


# ───────────────────────────── active-proxy routing ─────────────────────────────


async def test_active_proxy_routes_send_emitting_to_per_proxy_transport() -> None:
    pool = _FakePool(_proxies(1))
    proxy = pool._proxies[0]
    per_proxy = _RecordingTransport(
        [httpx.Response(200, content=b"REALIMAGE", request=httpx.Request("GET", "x"))]
    )
    pool.transports[proxy.identity] = per_proxy
    # The download transport must NOT receive the request when a proxy is active.
    download = _RecordingTransport([])
    ctx = _ctx(pool, transport=download)

    url = "https://o48.mfcdn1.xyz/mf/abc/h/0.jpg"

    async def _fetch() -> bytes:
        return await ctx.get_bytes(url)

    out = await ctx.fetch_image_via_pool(_fetch)
    assert out == b"REALIMAGE"
    # The per-proxy transport received the request; the download transport did not.
    assert len(per_proxy.calls) == 1
    assert per_proxy.calls[0]["url"] == url
    assert download.calls == []
    assert ctx._active_proxy is None  # cleared afterward
