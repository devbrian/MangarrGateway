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


# ═══════════ groups (b/c/d): MangaBallSource sanitize-and-retry ════════════════

from manga_gateway.framework.cooldown import SourceFailureCooldown  # noqa: E402
from manga_gateway.framework.fanout import fan_out  # noqa: E402
from manga_gateway.framework.health import SourceHealth  # noqa: E402
from manga_gateway.models.search import SearchRequest  # noqa: E402
from manga_gateway.sources.mangaball import (  # noqa: E402
    MangaBallSource,
    _sanitize_waf_query,
)

# Reuse the live-shaped envelope builders from the search-flow test module so the
# bodies on the sanitized-retry 200 + the per-candidate chapter-listing are identical
# to the ones the search-flow tests assert against (no second fabricated shape).
from tests.test_mangaball_search import (  # noqa: E402
    _chapter,
    _chapter_listing,
    _search_envelope,
    _title,
)

_TITLE_ID = "68515540702284f8341784c8"


def _source_transport(*, search_responses: list[httpx.Response]) -> _RecordingTransport:
    return _RecordingTransport(
        search_responses=search_responses,
        listing_body=_chapter_listing([_chapter()]),
    )


def _titles_200() -> httpx.Response:
    """A search-advanced 200 carrying one ``Solo Leveling`` title candidate."""
    envelope = _search_envelope([_title(title_id=_TITLE_ID, name="Solo Leveling")])
    return httpx.Response(200, json=envelope)


# ─────────────────── _sanitize_waf_query unit coverage ───────────────────────


def test_sanitize_waf_query_strips_only_the_literal_trigger_token() -> None:
    # The trigger token is dropped; surrounding tokens are kept verbatim.
    assert _sanitize_waf_query("Solo Leveling System") == "Solo Leveling"
    # Possessive + trailing punctuation normalize to the bare trigger and drop.
    assert _sanitize_waf_query("The System's Fall") == "The Fall"
    assert _sanitize_waf_query("Beware, System, ahead") == "Beware, ahead"
    # Plural / embedded forms are NOT stripped — only the literal word matches.
    assert _sanitize_waf_query("Two Systems") == "Two Systems"
    assert _sanitize_waf_query("Systemic Shock") == "Systemic Shock"
    # A trigger-only query collapses to empty (caller short-circuits to []).
    assert _sanitize_waf_query("System") == ""
    assert _sanitize_waf_query("system's") == ""


# ─────────────────── (b) sanitize-retry returns pruned-against-original ───────


@pytest.mark.asyncio
async def test_search_with_system_sanitize_retries_and_returns_results() -> None:
    transport = _source_transport(
        search_responses=[
            _waf_403(),  # original "Solo Leveling System" → WAF block
            _titles_200(),  # sanitized "Solo Leveling" → titles
        ]
    )
    ctx = _ctx(transport)
    releases = await MangaBallSource().search(
        SearchRequest(type="manga", query="Solo Leveling System"), ctx
    )

    # Two search-advanced POSTs: the WAF block + the single sanitized retry.
    posts = _search_posts(transport)
    assert len(posts) == 2
    # The retry carried the STRIPPED search_input.
    assert posts[0] is not None and posts[0]["search_input"] == "Solo Leveling System"
    assert posts[1] is not None and posts[1]["search_input"] == "Solo Leveling"
    # The candidate was deep-enumerated and releases minted (pruned vs the ORIGINAL).
    assert releases
    assert all(rel.manga_title == "Solo Leveling" for rel in releases)


# ─────────────────── (c) recovered WAF feeds neither health nor cooldown ──────


@pytest.mark.asyncio
async def test_recovered_waf_records_no_source_health_failure() -> None:
    health = SourceHealth(threshold=3)
    transport = _source_transport(
        search_responses=[
            _waf_403(),
            _titles_200(),
        ]
    )
    ctx = _ctx(transport, source_health=health)
    releases = await MangaBallSource().search(
        SearchRequest(type="manga", query="Solo Leveling System"), ctx
    )
    assert releases  # recovered
    # The WAF block recorded NO failure; the recovered success reset the breaker.
    assert health.consecutive_failures == 0


@pytest.mark.asyncio
async def test_recovered_waf_does_not_feed_fanout_cooldown() -> None:
    cd = SourceFailureCooldown(base_seconds=30, max_seconds=600, clock=lambda: 0.0)
    source = MangaBallSource()
    transport = _source_transport(
        search_responses=[
            _waf_403(),
            _titles_200(),
        ]
    )
    ctx = _ctx(transport)

    async def run_one(src: MangaBallSource) -> list[Any]:
        return await src.search(
            SearchRequest(type="manga", query="Solo Leveling System"), ctx
        )

    releases, warnings = await fan_out([source], run_one, cooldown=cd)
    assert releases  # the recovered search returned results through fan_out
    # No collect_warnings here → soft ctx.warn warnings are not pulled; what matters
    # is that fan_out synthesizes NO hard (cooldown-feeding) warning for a recovery.
    assert warnings == []
    assert cd.in_cooldown("mangaball") is False  # cooldown never fed


# ─────────────────── (d) trigger-only query → [] with a single POST ───────────


@pytest.mark.asyncio
async def test_trigger_only_query_returns_empty_with_single_post() -> None:
    health = SourceHealth(threshold=3)
    transport = _source_transport(search_responses=[_waf_403()])
    ctx = _ctx(transport, source_health=health)
    releases = await MangaBallSource().search(
        SearchRequest(type="manga", query="System"), ctx
    )
    # Soft empty result — never source_unavailable.
    assert releases == []
    # Exactly ONE search-advanced POST (no retry — the sanitized query is empty).
    assert len(_search_posts(transport)) == 1
    # No source-health failure recorded (waf_blocked is not fed; no success either).
    assert health.consecutive_failures == 0


# ─────────────── (e) unrecovered WAF blocks are SURFACED (not silent) ──────────
# A waf_blocked absorbed to [] is invisible to fanout/cooldown, so the source MUST
# surface it via a soft ``ctx.warn`` (rides the success path → shows in the response
# ``warnings[]`` WITHOUT feeding the cooldown). Otherwise a silent coverage loss —
# or a NEW WAF trigger word our denylist misses — looks identical to a real 0-results.


def _waf_warnings(ctx: SourceContext) -> list[tuple[str, str]]:
    return [w for w in ctx.warnings if w[0] == "waf_blocked"]


@pytest.mark.asyncio
async def test_trigger_only_query_surfaces_soft_warning() -> None:
    transport = _source_transport(search_responses=[_waf_403()])
    ctx = _ctx(transport)
    releases = await MangaBallSource().search(
        SearchRequest(type="manga", query="System"), ctx
    )
    assert releases == []
    # The drop is surfaced as a soft warning, never silently swallowed.
    assert len(_waf_warnings(ctx)) == 1


@pytest.mark.asyncio
async def test_unknown_trigger_word_surfaces_soft_warning() -> None:
    # The query carries NO known trigger token, yet the WAF blocks it → a NEW trigger
    # word the denylist misses. Sanitize strips nothing (sanitized == original), so
    # there is a single POST and the block is surfaced (loud log + soft warning).
    transport = _source_transport(search_responses=[_waf_403()])
    ctx = _ctx(transport)
    releases = await MangaBallSource().search(
        SearchRequest(type="manga", query="Mahouka Koukou"), ctx
    )
    assert releases == []
    assert len(_search_posts(transport)) == 1  # nothing to retry with
    assert len(_waf_warnings(ctx)) == 1


@pytest.mark.asyncio
async def test_sanitized_retry_still_blocked_surfaces_soft_warning() -> None:
    # Both the original AND the sanitized retry WAF-block → give up softly, surfaced.
    transport = _source_transport(search_responses=[_waf_403(), _waf_403()])
    ctx = _ctx(transport)
    releases = await MangaBallSource().search(
        SearchRequest(type="manga", query="Solo Leveling System"), ctx
    )
    assert releases == []
    assert len(_search_posts(transport)) == 2  # original + one sanitized retry
    assert len(_waf_warnings(ctx)) == 1


@pytest.mark.asyncio
async def test_recovered_waf_surfaces_soft_warning() -> None:
    # A recovered block returned results, but for a DEGRADED (sanitized) query — so it
    # STILL surfaces a soft warning disclosing the fallback (results are approximate,
    # not an exact-query match). The warning rides the success path → no cooldown.
    transport = _source_transport(search_responses=[_waf_403(), _titles_200()])
    ctx = _ctx(transport)
    releases = await MangaBallSource().search(
        SearchRequest(type="manga", query="Solo Leveling System"), ctx
    )
    assert releases  # recovered
    assert len(_waf_warnings(ctx)) == 1


@pytest.mark.asyncio
async def test_unrecovered_waf_warning_surfaces_via_fanout_without_cooldown() -> None:
    # End-to-end: an unrecovered WAF block surfaces in the fanout warnings (via
    # collect_warnings on the SUCCESS path) AND never feeds the cooldown.
    cd = SourceFailureCooldown(base_seconds=30, max_seconds=600, clock=lambda: 0.0)
    source = MangaBallSource()
    transport = _source_transport(search_responses=[_waf_403(), _waf_403()])
    ctx = _ctx(transport)

    async def run_one(src: MangaBallSource) -> list[Any]:
        return await src.search(
            SearchRequest(type="manga", query="Solo Leveling System"), ctx
        )

    releases, warnings = await fan_out(
        [source],
        run_one,
        collect_warnings=lambda src: list(ctx.warnings),
        cooldown=cd,
    )
    assert releases == []
    assert ("mangaball", "waf_blocked") in [(k, c) for k, c, _ in warnings]
    assert cd.in_cooldown("mangaball") is False  # surfaced, but NOT a cooldown failure
