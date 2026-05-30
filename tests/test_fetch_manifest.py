"""R6 fetch primitives: ``get_bytes`` + MangaDex at-home hooks (03-02 Task 1).

Covers the testable pure-ish units of the fetch half of the R6 loop:

* ``get_bytes`` mirrors ``get_json`` (same tenacity policy, same permanent-4xx
  gate) but acquires NO per-source ``AsyncLimiter``: image bytes ride the at-home
  node host, not ``api.mangadex.org`` (D-31 / Pitfall 3).
* MangaDex ``fetch_manifest`` resolves a FRESH at-home manifest into ordered
  ``{baseUrl}/data/{hash}/{file}`` URLs internally, never round-tripped
  (PKG-01/R6, D-17).
* ``fetch_image`` delegates to ``get_bytes``.
* The ``Source`` base declares both hooks abstractly so the engine stays
  source-agnostic (SRC-01).
"""

from __future__ import annotations

import abc

import httpx
import pytest

from manga_gateway.framework.base import Source
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.errors import SourceError
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangadex import MangaDexSource

# ───────────────────────────── transport / ctx helpers ──────────────────────────


class _SequenceTransport:
    """Fake Transport returning a queued sequence of responses, one per request."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls += 1
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _LimiterSpy:
    """Wraps the AsyncLimiter, counting acquisitions to prove get_bytes skips it."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.acquisitions = 0

    async def __aenter__(self) -> object:
        self.acquisitions += 1
        return await self._inner.__aenter__()  # type: ignore[attr-defined]

    async def __aexit__(self, *exc: object) -> None:
        await self._inner.__aexit__(*exc)  # type: ignore[attr-defined]


def _ctx_over(transport: _SequenceTransport) -> SourceContext:
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager

    return SourceContext(
        source_key="x",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        # Plan 04-02 anti-bot seams — default-off so MangaDex/manifest tests stay
        # byte-for-byte unchanged (no clearance, no decrypt, no health feed).
        solver=None,
        antibot="none",
        decrypt_scheme=None,
        decrypt_config=None,
        source_health=None,
    )


# ─────────────────────────────── get_bytes (D-31) ───────────────────────────────


@pytest.mark.asyncio
async def test_get_bytes_returns_raw_content_unchanged() -> None:
    blob = b"\x89PNG\r\n\x1a\n raw image bytes, never recompressed"
    req = httpx.Request("GET", "https://node/data/h/1.png")
    transport = _SequenceTransport([httpx.Response(200, content=blob, request=req)])
    ctx = _ctx_over(transport)

    out = await ctx.get_bytes("https://node/data/h/1.png")

    assert out == blob  # exact bytes, no recompression
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_get_bytes_does_not_acquire_the_limiter() -> None:
    # D-31 / Pitfall 3: image bytes hit the at-home node host, NOT api.mangadex.org —
    # the per-job Semaphore is the ceiling, the per-source limiter must NOT be acquired.
    req = httpx.Request("GET", "https://node/data/h/1.png")
    transport = _SequenceTransport([httpx.Response(200, content=b"img", request=req)])
    ctx = _ctx_over(transport)
    spy = _LimiterSpy(ctx._limiter)
    ctx._limiter = spy  # type: ignore[assignment]

    await ctx.get_bytes("https://node/data/h/1.png")

    assert spy.acquisitions == 0  # limiter NEVER acquired for image bytes


@pytest.mark.asyncio
async def test_get_bytes_raises_source_error_on_permanent_4xx() -> None:
    req = httpx.Request("GET", "https://node/data/h/1.png")
    transport = _SequenceTransport([httpx.Response(404, request=req)])
    ctx = _ctx_over(transport)

    with pytest.raises(SourceError):
        await ctx.get_bytes("https://node/data/h/1.png")
    assert transport.calls == 1  # permanent 4xx → no retry


@pytest.mark.asyncio
async def test_get_bytes_retries_5xx_then_succeeds() -> None:
    req = httpx.Request("GET", "https://node/data/h/1.png")
    transport = _SequenceTransport(
        [
            httpx.Response(503, request=req),
            httpx.Response(200, content=b"ok", request=req),
        ]
    )
    ctx = _ctx_over(transport)

    out = await ctx.get_bytes("https://node/data/h/1.png")

    assert out == b"ok"
    assert transport.calls == 2  # retried once after the 503


# ───────────────────────── MangaDex at-home fetch_manifest ───────────────────────

_AT_HOME = {
    "result": "ok",
    "baseUrl": "https://node-7.mangadex.network/abc",
    "chapter": {
        "hash": "deadbeefhash",
        "data": ["x1-full.png", "x2-full.png", "x3-full.png"],
        "dataSaver": ["x1-ds.jpg", "x2-ds.jpg", "x3-ds.jpg"],
    },
}


@pytest.mark.asyncio
async def test_fetch_manifest_builds_ordered_full_quality_urls() -> None:
    req = httpx.Request("GET", "https://api.mangadex.org/at-home/server/CID")
    transport = _SequenceTransport([httpx.Response(200, json=_AT_HOME, request=req)])
    ctx = _ctx_over(transport)
    src = MangaDexSource()

    urls = await src.fetch_manifest("CID", ctx)

    base = "https://node-7.mangadex.network/abc"
    assert urls == [
        f"{base}/data/deadbeefhash/x1-full.png",
        f"{base}/data/deadbeefhash/x2-full.png",
        f"{base}/data/deadbeefhash/x3-full.png",
    ]
    # full-quality `data`, not `dataSaver` (Pitfall 5 / RESEARCH).
    assert all("-full.png" in u for u in urls)
    assert all("-ds.jpg" not in u for u in urls)


@pytest.mark.asyncio
async def test_fetch_manifest_calls_the_at_home_endpoint() -> None:
    captured: dict[str, str] = {}

    class _CapturingTransport(_SequenceTransport):
        async def request(
            self, method: str, url: str, **kwargs: object
        ) -> httpx.Response:
            captured["url"] = url
            return await super().request(method, url, **kwargs)

    req = httpx.Request("GET", "https://api.mangadex.org/at-home/server/CID")
    transport = _CapturingTransport([httpx.Response(200, json=_AT_HOME, request=req)])
    ctx = _ctx_over(transport)

    await MangaDexSource().fetch_manifest("CID", ctx)

    assert captured["url"] == "https://api.mangadex.org/at-home/server/CID"


@pytest.mark.asyncio
async def test_fetch_manifest_embeds_the_responses_fresh_base_url() -> None:
    # D-17/D-32: baseUrl read FRESH from the response, never stored/hard-coded.
    fresh = dict(_AT_HOME)
    fresh["baseUrl"] = "https://other-node.example/xyz"
    req = httpx.Request("GET", "https://api.mangadex.org/at-home/server/CID")
    transport = _SequenceTransport([httpx.Response(200, json=fresh, request=req)])
    ctx = _ctx_over(transport)

    urls = await MangaDexSource().fetch_manifest("CID", ctx)

    assert all(u.startswith("https://other-node.example/xyz/data/") for u in urls)


@pytest.mark.parametrize(
    "body",
    [
        {"result": "ok", "chapter": {"hash": "h", "data": ["1.png"]}},  # no baseUrl
        {"baseUrl": "https://n/x", "chapter": {"data": ["1.png"]}},  # no hash
        {"baseUrl": "https://n/x", "chapter": {"hash": "h"}},  # no data
        {"baseUrl": "https://n/x", "chapter": {"hash": "h", "data": "1.png"}},  # str
        {"baseUrl": "https://n/x", "chapter": "nope"},  # chapter not a dict
        {"baseUrl": "https://n/x", "chapter": {"hash": "h", "data": ["a", ""]}},
        {"baseUrl": "https://n/x", "chapter": {"hash": "h", "data": ["a", 7]}},
    ],
)
@pytest.mark.asyncio
async def test_fetch_manifest_raises_on_malformed_response(body: dict) -> None:
    # WR-06: a malformed at-home body must surface as a typed SourceError, never a
    # raw KeyError/TypeError, and never an invalid page URL deferred to image fetch.
    req = httpx.Request("GET", "https://api.mangadex.org/at-home/server/CID")
    transport = _SequenceTransport([httpx.Response(200, json=body, request=req)])
    ctx = _ctx_over(transport)

    with pytest.raises(SourceError) as ei:
        await MangaDexSource().fetch_manifest("CID", ctx)
    assert ei.value.code == "source_unavailable"


@pytest.mark.asyncio
async def test_fetch_image_delegates_to_get_bytes() -> None:
    req = httpx.Request("GET", "https://node/data/h/1.png")
    transport = _SequenceTransport(
        [httpx.Response(200, content=b"page-bytes", request=req)]
    )
    ctx = _ctx_over(transport)

    out = await MangaDexSource().fetch_image("https://node/data/h/1.png", ctx)

    assert out == b"page-bytes"


# ───────────────────────── source-agnostic abstract seam ─────────────────────────


def test_source_declares_fetch_hooks_abstract() -> None:
    # SRC-01: a subclass missing fetch_manifest/fetch_image cannot be instantiated.
    assert "fetch_manifest" in Source.__abstractmethods__
    assert "fetch_image" in Source.__abstractmethods__


def test_incomplete_source_fails_to_instantiate() -> None:
    class _Partial(Source):
        key = "partial"
        name = "Partial"
        base_url = "https://x"
        id_types = ["x"]
        languages = ["en"]
        rate_limit_per_minute = 10

        async def search(self, req, ctx):  # type: ignore[no-untyped-def]
            return []

        async def recent(self, *, languages, limit, since, ctx):  # type: ignore[no-untyped-def]
            return []

        # fetch_manifest / fetch_image intentionally omitted

    with pytest.raises(TypeError):
        _Partial()  # type: ignore[abstract]

    assert issubclass(Source, abc.ABC)
