"""Unit tests for ``SourceContext.get_json_array`` (mangadot bare-array transport).

``get_json_array`` is the list-bodied twin of ``get_json``: it parses a top-level
JSON ARRAY (mangadot's ``GET /api/manga/{id}/chapters/list`` returns ``[{…}, …]``,
which ``_parse_json_object`` rejects) while keeping FULL parity with ``get_json`` on
the cross-cutting transport path — the call-site rate limit, cf_clearance injection
(D-40), the challenge-403 single re-solve + retry (D-35), the permanent-4xx STOP, the
decrypt seam (D-39), and the health feed (D-36).

The harness mirrors ``tests/test_comix_fetch.py`` (the ``_RecordingTransport`` /
``_CountingSolver`` / ``_ctx`` shape) so the parity assertions read identically to the
``get_json`` coverage in that file. No real Patchright browser, no network.
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.antibot import Clearance
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.decrypt import _SCHEMES, register_scheme
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.health import SourceHealth
from manga_gateway.handles.store import HandleStore

# ───────────────────────────── transport / solver fakes ──────────────────────────


class _RecordingTransport:
    """Fake Transport returning queued responses, recording each request's kwargs."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _CountingSolver:
    """Fake solver supporting the internal ``force_resolve`` path (kept off Protocol).

    Returns a fixed ``Clearance`` and counts both plain and forced ``get_clearance``
    calls so tests can assert the re-solve happened exactly once (D-35).
    """

    USER_AGENT = "Mozilla/5.0 Chrome/cf"

    def __init__(self) -> None:
        self.calls = 0
        self.forced = 0

    async def get_clearance(
        self, source_key: str, *, force_resolve: bool = False
    ) -> Clearance:
        self.calls += 1
        if force_resolve:
            self.forced += 1
        return Clearance(cookies={"cf_clearance": "X"}, user_agent=self.USER_AGENT)


def _ctx(
    transport: _RecordingTransport,
    *,
    antibot: str = "none",
    solver: object | None = None,
    decrypt_scheme: str | None = None,
    source_health: SourceHealth | None = None,
) -> SourceContext:
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager

    return SourceContext(
        source_key="mangadot",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,  # type: ignore[arg-type]
        antibot=antibot,  # type: ignore[arg-type]
        decrypt_scheme=decrypt_scheme,
        source_health=source_health,
    )


def _cf_challenge_response(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        content=b"...challenge-platform...",
        request=req,
    )


_LIST_URL = "https://mangadot.net/api/manga/101/chapters/list"


# ───────────────────────── array parse + non-array guard ─────────────────────────


@pytest.mark.asyncio
async def test_get_json_array_returns_top_level_list() -> None:
    req = httpx.Request("GET", _LIST_URL)
    transport = _RecordingTransport(
        [httpx.Response(200, json=[{"id": 388872}, {"id": 388873}], request=req)]
    )
    ctx = _ctx(transport, antibot="cloudflare", solver=_CountingSolver())

    out = await ctx.get_json_array(_LIST_URL)

    assert out == [{"id": 388872}, {"id": 388873}]


@pytest.mark.asyncio
async def test_get_json_array_non_array_body_raises_source_error() -> None:
    # A JSON OBJECT body (the envelope every other JSON method expects) is rejected
    # by the new _parse_json_array guard, mirroring _parse_json_object's non-object
    # guard. The error message names the actual decoded type for diagnosability.
    req = httpx.Request("GET", _LIST_URL)
    transport = _RecordingTransport(
        [httpx.Response(200, json={"manga_list": []}, request=req)]
    )
    ctx = _ctx(transport, antibot="cloudflare", solver=_CountingSolver())

    with pytest.raises(SourceError) as exc:
        await ctx.get_json_array(_LIST_URL)
    assert "non-array JSON body (got dict)" in str(exc.value)


# ───────────────────── parity: rate-limit + clearance injection ──────────────────


@pytest.mark.asyncio
async def test_get_json_array_is_rate_limited_and_injects_clearance() -> None:
    # Parity with get_json: the call rides the call-site limiter (limited=True path)
    # and injects the cf_clearance cookie + its bound UA (D-40, Pitfall 1).
    req = httpx.Request("GET", _LIST_URL)
    transport = _RecordingTransport([httpx.Response(200, json=[], request=req)])
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare", solver=solver)

    out = await ctx.get_json_array(_LIST_URL)

    assert out == []
    assert solver.calls == 1
    call = transport.calls[0]
    assert call["headers"]["User-Agent"] == _CountingSolver.USER_AGENT  # type: ignore[index]
    assert call["headers"]["Cookie"] == "cf_clearance=X"  # type: ignore[index]
    assert "cookies" not in call


# ───────────────────────── parity: 403 challenge → single re-solve ──────────────


@pytest.mark.asyncio
async def test_get_json_array_challenge_403_triggers_single_resolve_and_retry() -> None:
    req = httpx.Request("GET", _LIST_URL)
    transport = _RecordingTransport(
        [
            _cf_challenge_response(req),  # challenge → forced re-solve + retry
            httpx.Response(200, json=[{"id": 1}], request=req),  # retry succeeds
        ]
    )
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare", solver=solver)

    out = await ctx.get_json_array(_LIST_URL)

    assert out == [{"id": 1}]
    assert solver.forced == 1  # exactly ONE forced re-solve (D-35, T-04-07)
    assert len(transport.calls) == 2  # initial + single retry


# ───────────────────────── parity: permanent 4xx → STOP + health ─────────────────


@pytest.mark.asyncio
async def test_get_json_array_permanent_403_stops_and_feeds_failure() -> None:
    req = httpx.Request("GET", _LIST_URL)
    transport = _RecordingTransport([httpx.Response(403, request=req)])  # no CF markers
    solver = _CountingSolver()
    health = SourceHealth(threshold=3)
    ctx = _ctx(transport, antibot="cloudflare", solver=solver, source_health=health)

    with pytest.raises(SourceError):
        await ctx.get_json_array(_LIST_URL)
    assert solver.forced == 0  # not a challenge → no re-solve
    assert len(transport.calls) == 1  # no retry on a permanent 4xx
    assert health.consecutive_failures == 1  # terminal failure fed the breaker


@pytest.mark.asyncio
async def test_get_json_array_clean_parse_records_success() -> None:
    health = SourceHealth(threshold=3)
    health.record_failure()  # prime the breaker
    req = httpx.Request("GET", _LIST_URL)
    transport = _RecordingTransport([httpx.Response(200, json=[], request=req)])
    ctx = _ctx(
        transport,
        antibot="cloudflare",
        solver=_CountingSolver(),
        source_health=health,
    )

    await ctx.get_json_array(_LIST_URL)

    assert health.consecutive_failures == 0  # success reset the breaker


# ───────────────────── parity: decrypt seam IS applied (not bypassed) ─────────────


@pytest.mark.asyncio
async def test_get_json_array_applies_registered_decrypt_scheme() -> None:
    """``get_json_array`` decrypts by default (decrypt=True) like ``get_json`` — it
    must NOT special-case a plaintext bypass. With a registered scheme the body is
    routed through it before parse (mangadot is plaintext, so decrypt is identity
    in practice, but the method stays uniform with get_json)."""
    scheme = "test-rot1-json-array"

    @register_scheme(scheme)
    def _rot1(body: bytes, config: dict[str, object]) -> bytes:
        # ROT-1 the bytes; the test feeds a pre-encoded body so decrypt yields
        # a valid JSON array. Confirms the seam runs (NOT bypassed).
        return bytes((b - 1) % 256 for b in body)

    try:
        plain = b"[{}]"
        encoded = bytes((b + 1) % 256 for b in plain)  # what the "server" returns
        req = httpx.Request("GET", _LIST_URL)
        transport = _RecordingTransport(
            [httpx.Response(200, content=encoded, request=req)]
        )
        ctx = _ctx(
            transport,
            antibot="cloudflare",
            solver=_CountingSolver(),
            decrypt_scheme=scheme,
        )

        out = await ctx.get_json_array(_LIST_URL)
        assert out == [{}]  # decrypt ran → ROT-1 reversed → valid array parsed
    finally:
        _SCHEMES.pop(scheme, None)
