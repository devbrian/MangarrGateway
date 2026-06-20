"""MangaBall WAF-block detection + sanitize-and-retry tests (260620-5yq).

mangaball.net sits behind a WAF that returns HTTP 403 with body
``{"error":"Malicious payload detected", ... "code":403}`` for ANY search POST whose
``search_input`` contains the literal word "System" (a SQL-injection false positive).
This module covers the two layers of the fix:

* Group (a) — the framework :func:`is_waf_block` predicate, mirroring
  ``test_is_csrf_failure_predicate`` in ``tests/test_session_prep.py``: it matches
  ONLY a 403 carrying ``Malicious payload``, rejecting a plain 403, a CSRF-failure
  403, a 200 carrying the words, and any 5xx. The framework-level ``waf_blocked``
  SourceError code + the ``post_json`` no-feed-failure guard are exercised through a
  REAL ``SourceContext`` over a recording transport (so the framework WAF detection
  actually runs, unlike the post_json-shorting fake in ``test_mangaball_search.py``).
* Groups (b/c/d) — the mangaball-scoped sanitize-and-retry in
  ``MangaBallSource.search``: a "System"-containing query sanitize-retries once and
  returns results pruned against the ORIGINAL query; a recovered WAF block records NO
  source-health failure and never feeds the fanout cooldown; a trigger-only query
  returns ``[]`` with a single POST.

No network: a recording transport routes by URL (search-advanced vs chapter-listing)
and serves queued responses so the WAF-403 -> 200 sequence is deterministic.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from manga_gateway.framework.context import SourceContext, is_waf_block

_API_URL = "https://mangaball.net/api/v1/title/search-advanced/"


def _waf_403(req: httpx.Request | None = None) -> httpx.Response:
    """A WAF-block 403 carrying the live ``Malicious payload detected`` body."""
    return httpx.Response(
        403,
        json={"error": "Malicious payload detected", "code": 403},
        request=req,
    )


# ═══════════════════════════ group (a): is_waf_block predicate ═════════════════


def test_is_waf_block_predicate() -> None:
    req = httpx.Request("POST", _API_URL)
    # Exact WAF body at 403 → True.
    assert is_waf_block(_waf_403(req)) is True
    # A plain 403 (no marker) is NOT a WAF block.
    assert is_waf_block(httpx.Response(403, text="forbidden", request=req)) is False
    # A CSRF-failure 403 is NOT a WAF block.
    csrf = httpx.Response(
        403, json={"error": "CSRF token validation failed"}, request=req
    )
    assert is_waf_block(csrf) is False
    # A 200 carrying the marker words is not a 403 → not a WAF block.
    ok_200 = httpx.Response(
        200, json={"error": "Malicious payload detected"}, request=req
    )
    assert is_waf_block(ok_200) is False
    # A 5xx is not a WAF block (status guard fires first).
    assert is_waf_block(httpx.Response(503, text="Malicious payload", request=req)) is (
        False
    )


# ─────────────────────────── recording transport / ctx ───────────────────────


class _RecordingTransport:
    """Fake Transport serving queued responses, recording each request's data.

    Routes by URL: ``search-advanced`` POSTs pop from ``search_responses`` (so the
    WAF-403 -> sanitized-200 sequence is deterministic); ``chapter-listing-by-title-id``
    POSTs build a fresh listing response per call (one per deep-enumerated candidate).
    """

    def __init__(
        self,
        *,
        search_responses: list[httpx.Response],
        listing_body: dict[str, Any] | None = None,
    ) -> None:
        self._search = list(search_responses)
        self._listing_body = listing_body or {}
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, url, kwargs.get("data")))
        req = httpx.Request(method, url)
        if url.endswith("/title/search-advanced/"):
            resp = self._search.pop(0)
        elif url.endswith("/chapter-listing-by-title-id/"):
            resp = httpx.Response(200, json=self._listing_body, request=req)
        else:  # pragma: no cover - guards against an unexpected call
            raise AssertionError(f"unexpected request url: {url}")
        if resp._request is None:  # noqa: SLF001 — test fixture binding
            resp.request = req
        return resp

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _ctx(transport: _RecordingTransport, *, source_health: Any = None) -> SourceContext:
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager
    from manga_gateway.handles.store import HandleStore

    return SourceContext(
        source_key="mangaball",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        source_health=source_health,
    )


def _search_posts(transport: _RecordingTransport) -> list[dict[str, Any] | None]:
    return [
        data
        for method, url, data in transport.requests
        if method == "POST" and url.endswith("/title/search-advanced/")
    ]


# ───────────── group (a) cont.: framework waf_blocked code + health guard ─────


@pytest.mark.asyncio
async def test_post_json_raises_distinct_waf_blocked_code() -> None:
    from manga_gateway.framework.errors import SourceError

    transport = _RecordingTransport(search_responses=[_waf_403()])
    ctx = _ctx(transport)
    with pytest.raises(SourceError) as ei:
        await ctx.post_json(_API_URL, data={"search_input": "System"})
    assert ei.value.code == "waf_blocked"  # distinct from source_unavailable
    assert ei.value.status == 403
    # tenacity does NOT retry a SourceError → exactly one transport call.
    assert len(_search_posts(transport)) == 1


@pytest.mark.asyncio
async def test_plain_403_still_raises_source_unavailable() -> None:
    from manga_gateway.framework.errors import SourceError

    req = httpx.Request("POST", _API_URL)
    transport = _RecordingTransport(
        search_responses=[httpx.Response(403, text="forbidden", request=req)]
    )
    ctx = _ctx(transport)
    with pytest.raises(SourceError) as ei:
        await ctx.post_json(_API_URL, data={"search_input": "x"})
    assert ei.value.code == "source_unavailable"  # non-WAF 403 unchanged
    assert ei.value.status == 403


@pytest.mark.asyncio
async def test_waf_blocked_does_not_feed_source_health() -> None:
    from manga_gateway.framework.errors import SourceError
    from manga_gateway.framework.health import SourceHealth

    health = SourceHealth(threshold=3)
    transport = _RecordingTransport(search_responses=[_waf_403()])
    ctx = _ctx(transport, source_health=health)
    with pytest.raises(SourceError):
        await ctx.post_json(_API_URL, data={"search_input": "System"})
    # A waf_blocked is a recoverable soft signal — it must NOT feed the breaker.
    assert health.consecutive_failures == 0


@pytest.mark.asyncio
async def test_plain_403_still_feeds_source_health() -> None:
    from manga_gateway.framework.errors import SourceError
    from manga_gateway.framework.health import SourceHealth

    health = SourceHealth(threshold=3)
    req = httpx.Request("POST", _API_URL)
    transport = _RecordingTransport(
        search_responses=[httpx.Response(403, text="forbidden", request=req)]
    )
    ctx = _ctx(transport, source_health=health)
    with pytest.raises(SourceError):
        await ctx.post_json(_API_URL, data={"search_input": "x"})
    # A non-WAF terminal 403 feeds health byte-for-byte as before.
    assert health.consecutive_failures == 1
