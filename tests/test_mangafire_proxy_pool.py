"""MangaFire proxy-OUTER / zone-INNER composition (260620-4im, Task 4).

Asserts MangaFire opts into the framework residential proxy pool (one flag) while a
non-opted source stays off, then drives ``fetch_image`` through a REAL ``SourceContext``
built with a fake ``ProxyPool`` (per-identity fake transports) + the opt-in flag to
prove the composition: (a) a clean proxy serves the first zone with no rewrite;
(b) a proxy that 403s ALL zones is ``mark_failed``'d and the framework rotates to a
different clean proxy, whose retried ``fetch_image`` returns bytes. Offline, fake creds.
"""

from __future__ import annotations

import httpx

from manga_gateway.framework.base import Source
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.proxy_pool import PooledProxy
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangadex import MangaDexSource
from manga_gateway.sources.mangafire import MangaFireSource

_HOST = "proxy.invalid"
_CDN = "https://o48.mfcdn1.xyz/mf/abcdef0123/h"
_IMG = b"\xff\xd8\xff\xe0REALIMAGE"  # arbitrary bytes; fetch_image does not validate


class _RecordingTransport:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append(url)
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _FakePool:
    def __init__(self, proxies: list[PooledProxy]) -> None:
        self._proxies = list(proxies)
        self.acquired: list[str] = []
        self.failed: list[str] = []
        self.transports: dict[str, _RecordingTransport] = {}
        self._cooled: set[str] = set()

    def acquire(self, exclude: set[str] | None = None) -> PooledProxy | None:
        excl = (exclude or set()) | self._cooled
        for proxy in self._proxies:
            if proxy.selection_key not in excl:
                self.acquired.append(proxy.selection_key)
                return proxy
        return None

    def mark_failed(self, proxy: PooledProxy) -> None:
        self.failed.append(proxy.selection_key)
        self._cooled.add(proxy.selection_key)

    def transport_for(self, proxy: PooledProxy) -> _RecordingTransport:
        return self.transports[proxy.selection_key]


def _resp(status: int, url: str, content: bytes = b"") -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("GET", url))


def _ctx(pool: _FakePool) -> SourceContext:
    return SourceContext(
        source_key="mangafire",
        rate_limit_per_minute=6000,
        session=SessionManager(_RecordingTransport([])),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        proxy_pool=pool,  # type: ignore[arg-type]
        image_via_proxy_pool=True,
        image_proxy_max_attempts=3,
    )


# ───────────────────────────── opt-in flag ─────────────────────────────


def test_mangafire_opts_in_others_do_not() -> None:
    assert MangaFireSource.image_fetch_via_proxy_pool is True
    assert MangaDexSource.image_fetch_via_proxy_pool is False
    assert Source.image_fetch_via_proxy_pool is False


# ───────────────────────────── composition ─────────────────────────────


async def test_clean_proxy_first_zone_no_rewrite() -> None:
    proxies = [PooledProxy(host=_HOST, port=8000)]
    pool = _FakePool(proxies)
    transport = _RecordingTransport([_resp(200, f"{_CDN}/0.jpg", _IMG)])
    pool.transports[proxies[0].selection_key] = transport
    ctx = _ctx(pool)
    source = MangaFireSource()
    url = f"{_CDN}/0.jpg"  # plain CDN url (the new API has no scramble offset)

    out = await ctx.fetch_image_via_pool(lambda: source.fetch_image(url, ctx))
    assert out == _IMG
    # The first zone answered on the sticky proxy; no zone rewrite happened.
    assert transport.calls == [url]
    assert pool.failed == []
    assert pool.acquired == [proxies[0].selection_key]


async def test_proxy_403_all_zones_rotates_to_clean_proxy() -> None:
    proxies = [PooledProxy(host=_HOST, port=8000), PooledProxy(host=_HOST, port=8001)]
    pool = _FakePool(proxies)
    # Proxy 0 403s every zone candidate (zones 1,2,3 → 3 requests).
    bad = _RecordingTransport([_resp(403, _CDN) for _ in range(3)])
    # Proxy 1 serves the first zone cleanly.
    good = _RecordingTransport([_resp(200, f"{_CDN}/0.jpg", _IMG)])
    pool.transports[proxies[0].selection_key] = bad
    pool.transports[proxies[1].selection_key] = good
    ctx = _ctx(pool)
    source = MangaFireSource()
    url = f"{_CDN}/0.jpg"

    out = await ctx.fetch_image_via_pool(lambda: source.fetch_image(url, ctx))
    assert out == _IMG
    # Proxy 0 tried all 3 zones, all 403 → whole zone-retry failed → marked + rotated.
    assert len(bad.calls) == 3
    assert pool.failed == [proxies[0].selection_key]
    # A DIFFERENT (clean) proxy served the retried fetch and is now sticky.
    assert ctx._sticky_proxy is not None
    assert ctx._sticky_proxy.selection_key == proxies[1].selection_key


async def test_all_proxies_403_raises_terminal() -> None:
    proxies = [PooledProxy(host=_HOST, port=8000)]
    pool = _FakePool(proxies)
    pool.transports[proxies[0].selection_key] = _RecordingTransport(
        [_resp(403, _CDN) for _ in range(3)]
    )
    ctx = _ctx(pool)
    source = MangaFireSource()
    url = f"{_CDN}/0.jpg"

    try:
        await ctx.fetch_image_via_pool(lambda: source.fetch_image(url, ctx))
    except SourceError as exc:
        assert exc.status == 403  # the source's own terminal all-zones-blocked error
    else:  # pragma: no cover - must raise
        raise AssertionError("expected SourceError")
    assert pool.failed == [proxies[0].selection_key]
