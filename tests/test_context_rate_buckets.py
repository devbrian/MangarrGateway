"""Unit tests for the 260625-rom optional second rate-limit bucket (SRC-03 / D-30).

A source opts in via ``download_rate_limit_per_minute``; the framework then provisions
a SECOND, independent ``AsyncLimiter`` (keyed ``{source_key}:download``) that
``_send`` acquires ONLY for a per-call ``bucket="download"`` request. Splitting
kagane's hard-throttled token path off the search bucket means a download flood can no
longer drain the shared bucket and starve interactive search into 30s timeouts.

Coverage (mirrors the ``_RecordingTransport`` + real ``SourceContext`` shape in
``tests/test_transport_pool_split.py`` / ``tests/test_context_get_json_array.py`` —
pure-unit, no network, no Patchright):

(a) two distinct limiters are provisioned only when the source opts in (None ⇒ none);
(b) draining either bucket leaves the other's capacity intact (both directions);
(c) ``bucket="download"`` drains the download limiter, a default call drains the
    primary;
(d) the None path is byte-for-byte: a ``bucket="download"`` call on a non-opted source
    falls back to the primary limiter without error.

``AsyncLimiter.has_capacity()`` touches the running event loop, so every capacity probe
runs INSIDE an async test.
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore

# ───────────────────────────── transport fake ──────────────────────────────


class _RecordingTransport:
    """Fake Transport that always returns a canned 200 JSON object, counting calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _ctx(
    *,
    rate_limit_per_minute: int = 600,
    download_rate_limit_per_minute: int | None = None,
    transport: _RecordingTransport | None = None,
) -> SourceContext:
    session = SessionManager(transport or _RecordingTransport())
    return SourceContext(
        source_key="x",
        rate_limit_per_minute=rate_limit_per_minute,
        download_rate_limit_per_minute=download_rate_limit_per_minute,
        session=session,
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
    )


# ─────────────────── (a) provisioning: opt-in vs None ───────────────────────


@pytest.mark.asyncio
async def test_download_rate_provisions_a_distinct_second_limiter() -> None:
    ctx = _ctx(rate_limit_per_minute=600, download_rate_limit_per_minute=24)
    assert ctx._download_limiter is not None
    assert ctx._download_limiter is not ctx._limiter


@pytest.mark.asyncio
async def test_none_provisions_no_second_limiter() -> None:
    # The regression guard: a source WITHOUT the attr gets a single bucket only.
    ctx = _ctx(rate_limit_per_minute=600, download_rate_limit_per_minute=None)
    assert ctx._download_limiter is None


# ──────────────── (b) drain isolation, both directions ──────────────────────


@pytest.mark.asyncio
async def test_draining_download_does_not_consume_search_tokens() -> None:
    ctx = _ctx(rate_limit_per_minute=600, download_rate_limit_per_minute=1)
    assert ctx._download_limiter is not None
    # Consume the download bucket's single token directly via the public API.
    await ctx._download_limiter.acquire()
    assert ctx._download_limiter.has_capacity() is False
    # The search bucket is untouched — its tokens were NOT consumed.
    assert ctx._limiter.has_capacity() is True


@pytest.mark.asyncio
async def test_draining_search_does_not_consume_download_tokens() -> None:
    # Symmetric direction: a small SEARCH bucket, a roomy download bucket.
    ctx = _ctx(rate_limit_per_minute=1, download_rate_limit_per_minute=600)
    assert ctx._download_limiter is not None
    await ctx._limiter.acquire()
    assert ctx._limiter.has_capacity() is False
    # Draining search leaves the download bucket with capacity.
    assert ctx._download_limiter.has_capacity() is True


# ─────────────── (c) bucket selection routes correctly ──────────────────────


@pytest.mark.asyncio
async def test_download_bucket_call_drains_the_download_limiter() -> None:
    transport = _RecordingTransport()
    ctx = _ctx(
        rate_limit_per_minute=1,
        download_rate_limit_per_minute=1,
        transport=transport,
    )
    assert ctx._download_limiter is not None
    await ctx.post_json_body("https://x/api", body={}, bucket="download")
    assert transport.calls == 1
    # The download limiter was acquired; the primary (search) bucket is untouched.
    assert ctx._download_limiter.has_capacity() is False
    assert ctx._limiter.has_capacity() is True


@pytest.mark.asyncio
async def test_default_bucket_call_drains_the_primary_limiter() -> None:
    transport = _RecordingTransport()
    ctx = _ctx(
        rate_limit_per_minute=1,
        download_rate_limit_per_minute=1,
        transport=transport,
    )
    assert ctx._download_limiter is not None
    # ``bucket`` omitted ⇒ default ⇒ the primary limiter, NOT the download one.
    await ctx.post_json_body("https://x/api", body={})
    assert transport.calls == 1
    assert ctx._limiter.has_capacity() is False
    assert ctx._download_limiter.has_capacity() is True


# ───────────────── (d) None path: byte-for-byte fallback ────────────────────


@pytest.mark.asyncio
async def test_download_bucket_call_falls_back_to_primary_when_not_opted_in() -> None:
    transport = _RecordingTransport()
    ctx = _ctx(
        rate_limit_per_minute=1,
        download_rate_limit_per_minute=None,
        transport=transport,
    )
    assert ctx._download_limiter is None
    # A download-tagged call on a source that did NOT opt in must NOT crash and must
    # fall back to the single primary limiter (the every-other-source-unchanged contract).
    await ctx.post_json_body("https://x/api", body={}, bucket="download")
    assert transport.calls == 1
    assert ctx._limiter.has_capacity() is False
