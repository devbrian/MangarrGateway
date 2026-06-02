"""A parse-time ``SourceError`` must feed the health breaker — for ALL four JSON
helpers (issue #96).

``get_json`` / ``post_json`` / ``get_json_plain`` / ``get_json_array`` each parse the
``200`` body AFTER the transport call. Before #96 the parse ran OUTSIDE the
``try/except SourceError: self._feed_failure()`` block, so a malformed-but-``200``
body (a non-object for ``_parse_json_object``, a non-array for ``_parse_json_array``)
raised ``SourceError`` from the parser WITHOUT recording a breaker failure — the
breaker stayed stale exactly when the upstream shape had broken (D-36). The fix moves
the parse inside the failure-feeding ``try`` for every helper.

These tests assert the breaker actually trips (``record_failure`` → a non-zero
``consecutive_failures``), not merely that the parser raises — the existing
``test_context_get_json_array`` only covered the raise. The harness mirrors
``tests/test_context_get_json_array.py``: a fake transport, no network, no browser.
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.health import SourceHealth
from manga_gateway.handles.store import HandleStore


class _RecordingTransport:
    """Fake Transport returning queued responses (mirrors the array-test harness)."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _ctx(
    transport: _RecordingTransport, *, source_health: SourceHealth
) -> SourceContext:
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager

    return SourceContext(
        source_key="parse-breaker",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        source_health=source_health,
    )


_URL = "https://example.test/api/endpoint"


@pytest.mark.asyncio
async def test_get_json_non_object_200_feeds_breaker() -> None:
    # A top-level ARRAY (non-object) body → _parse_json_object raises SourceError.
    health = SourceHealth(threshold=3)
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([httpx.Response(200, json=[1, 2], request=req)])
    ctx = _ctx(transport, source_health=health)

    with pytest.raises(SourceError):
        await ctx.get_json(_URL)
    assert health.consecutive_failures == 1  # parse failure tripped the breaker


@pytest.mark.asyncio
async def test_post_json_non_object_200_feeds_breaker() -> None:
    health = SourceHealth(threshold=3)
    req = httpx.Request("POST", _URL)
    transport = _RecordingTransport([httpx.Response(200, json=[1, 2], request=req)])
    ctx = _ctx(transport, source_health=health)

    with pytest.raises(SourceError):
        await ctx.post_json(_URL, data={"q": "x"})
    assert health.consecutive_failures == 1


@pytest.mark.asyncio
async def test_get_json_plain_non_object_200_feeds_breaker() -> None:
    health = SourceHealth(threshold=3)
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([httpx.Response(200, json=[1, 2], request=req)])
    ctx = _ctx(transport, source_health=health)

    with pytest.raises(SourceError):
        await ctx.get_json_plain(_URL)
    assert health.consecutive_failures == 1


@pytest.mark.asyncio
async def test_get_json_array_non_array_200_feeds_breaker() -> None:
    # The list-bodied twin: a top-level OBJECT body → _parse_json_array raises.
    health = SourceHealth(threshold=3)
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport(
        [httpx.Response(200, json={"data": []}, request=req)]
    )
    ctx = _ctx(transport, source_health=health)

    with pytest.raises(SourceError):
        await ctx.get_json_array(_URL)
    assert health.consecutive_failures == 1
