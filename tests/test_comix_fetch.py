"""Comix anti-bot fetch seam: clearance injection, 403 re-solve, decrypt, health.

Exercises the load-bearing ``SourceContext`` refactor (Plan 04-02) with the in-repo
``_SequenceTransport`` harness + a fake solver — no real Patchright browser, no network:

* D-40: a ``cloudflare*`` source injects ``clearance.cookies`` (serialized into a
  per-request ``Cookie`` header — NOT the httpx-0.28-deprecated ``cookies=`` kwarg) +
  the EXACT ``clearance.user_agent`` per outbound request; the injected UA equals the
  cookie's UA (Pitfall 1). MangaDex (``antibot="none"``) sends NO Cookie/UA override.
* D-35: a ``cloudflare*`` 403 carrying CF challenge markers triggers exactly ONE forced
  re-solve + retry; a non-challenge 403, a second challenge, or a MangaDex 403 is
  terminal (Pitfall 2).
* D-39: a declared ``decrypt_scheme`` routes the response body through the framework
  decrypt seam; ``None`` is identity pass-through.
* D-36: a terminal failure feeds ``source_health.record_failure()``; a success feeds
  ``record_success()``.
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.antibot import Clearance
from manga_gateway.framework.context import SourceContext, is_cf_challenge
from manga_gateway.framework.decrypt import _SCHEMES, register_scheme
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.health import SourceHealth
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.comix import ComixSource

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

    Returns a fixed ``Clearance`` (cf_clearance=X, UA ``Chrome/cf``) and counts both
    plain and forced ``get_clearance`` calls so tests can assert the re-solve happened.
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


class _NullSolver:
    """A solver returning ``None`` (no clearance needed)."""

    async def get_clearance(
        self, source_key: str, *, force_resolve: bool = False
    ) -> None:
        return None


def _ctx(
    transport: _RecordingTransport,
    *,
    antibot: str = "none",
    solver: object | None = None,
    decrypt_scheme: str | None = None,
    decrypt_config: dict[str, object] | None = None,
    source_health: SourceHealth | None = None,
) -> SourceContext:
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager

    return SourceContext(
        source_key="comix",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,  # type: ignore[arg-type]
        antibot=antibot,  # type: ignore[arg-type]
        decrypt_scheme=decrypt_scheme,
        decrypt_config=decrypt_config,
        source_health=source_health,
    )


def _cf_challenge_response(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        content=b"...challenge-platform...",
        request=req,
    )


def _cf_challenge_503_response(req: httpx.Request) -> httpx.Response:
    # Issue #172: Cloudflare also serves challenges as 503 interstitials.
    return httpx.Response(
        503,
        headers={"server": "cloudflare"},
        content=b"...challenge-platform...",
        request=req,
    )


# ───────────────────────── D-40: clearance injection (UA match) ──────────────────


@pytest.mark.asyncio
async def test_cloudflare_get_json_injects_clearance_with_matching_ua() -> None:
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport(
        [httpx.Response(200, json={"ok": True}, request=req)]
    )
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare+encrypted", solver=solver)

    out = await ctx.get_json("https://comix/api")

    assert out == {"ok": True}
    assert solver.calls == 1
    call = transport.calls[0]
    # Pitfall 1: the injected UA MUST equal the cookie's UA.
    assert call["headers"]["User-Agent"] == _CountingSolver.USER_AGENT  # type: ignore[index]
    # Clearance rides a per-request Cookie header, NOT the deprecated cookies= kwarg.
    assert call["headers"]["Cookie"] == "cf_clearance=X"  # type: ignore[index]
    assert "cookies" not in call


@pytest.mark.asyncio
async def test_cloudflare_get_bytes_injects_clearance_with_matching_ua() -> None:
    req = httpx.Request("GET", "https://comix/p/1")
    transport = _RecordingTransport([httpx.Response(200, content=b"img", request=req)])
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare", solver=solver)

    out = await ctx.get_bytes("https://comix/p/1")

    assert out == b"img"
    call = transport.calls[0]
    assert call["headers"]["User-Agent"] == _CountingSolver.USER_AGENT  # type: ignore[index]
    assert call["headers"]["Cookie"] == "cf_clearance=X"  # type: ignore[index]
    assert "cookies" not in call


class _MultiCookieSolver:
    """Solver returning a multi-cookie Clearance (cf_clearance + Laravel session).

    Mirrors the real issue #71 Comix clearance, which carries cf_clearance plus the
    Laravel ``session`` cookie — exercises the ordered ``"k=v; k2=v2"`` serialization.
    """

    USER_AGENT = "Mozilla/5.0 Chrome/cf"

    async def get_clearance(
        self, source_key: str, *, force_resolve: bool = False
    ) -> Clearance:
        return Clearance(
            cookies={"cf_clearance": "X", "session": "Y"},
            user_agent=self.USER_AGENT,
        )


@pytest.mark.asyncio
async def test_cloudflare_multi_cookie_serializes_to_ordered_cookie_header() -> None:
    # Multiple clearance cookies serialize in dict order to "k=v; k2=v2".
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport(
        [httpx.Response(200, json={"ok": True}, request=req)]
    )
    ctx = _ctx(transport, antibot="cloudflare+encrypted", solver=_MultiCookieSolver())

    await ctx.get_json("https://comix/api")

    call = transport.calls[0]
    assert call["headers"]["Cookie"] == "cf_clearance=X; session=Y"  # type: ignore[index]
    assert call["headers"]["User-Agent"] == _MultiCookieSolver.USER_AGENT  # type: ignore[index]
    assert "cookies" not in call


@pytest.mark.asyncio
async def test_mangadex_sends_no_cookie_or_ua_override() -> None:
    # Regression: antibot="none" / no solver → request kwargs identical to today.
    req = httpx.Request("GET", "https://api.mangadex.org/x")
    transport = _RecordingTransport(
        [httpx.Response(200, json={"result": "ok"}, request=req)]
    )
    ctx = _ctx(transport, antibot="none", solver=None)

    await ctx.get_json("https://api.mangadex.org/x")

    call = transport.calls[0]
    assert "cookies" not in call or call["cookies"] is None
    assert "headers" not in call or call["headers"] is None


@pytest.mark.asyncio
async def test_cloudflare_with_null_clearance_sends_no_override() -> None:
    # A cloudflare source whose solver returns None still works, with no override.
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport([httpx.Response(200, json={"ok": 1}, request=req)])
    ctx = _ctx(transport, antibot="cloudflare", solver=_NullSolver())

    await ctx.get_json("https://comix/api")

    call = transport.calls[0]
    assert "cookies" not in call or call["cookies"] is None
    assert "headers" not in call or call["headers"] is None


# ───────────────────────── D-35: 403 challenge → re-solve ─────────────────────────


def test_is_cf_challenge_detects_markers() -> None:
    req = httpx.Request("GET", "https://comix/api")
    assert is_cf_challenge(_cf_challenge_response(req)) is True
    # Issue #172: a 503 CF interstitial is ALSO a challenge.
    assert is_cf_challenge(_cf_challenge_503_response(req)) is True
    # A plain 403 (no CF markers) is NOT a challenge.
    assert is_cf_challenge(httpx.Response(403, request=req)) is False
    # A 200 is never a challenge.
    assert is_cf_challenge(httpx.Response(200, request=req)) is False


@pytest.mark.asyncio
async def test_cloudflare_challenge_403_triggers_single_resolve_and_retry() -> None:
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport(
        [
            _cf_challenge_response(req),  # first → challenge → re-solve + retry
            httpx.Response(200, json={"ok": True}, request=req),  # retry succeeds
        ]
    )
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare+encrypted", solver=solver)

    out = await ctx.get_json("https://comix/api")

    assert out == {"ok": True}
    assert solver.forced == 1  # exactly ONE forced re-solve
    assert len(transport.calls) == 2  # initial + single retry


@pytest.mark.asyncio
async def test_cloudflare_challenge_503_triggers_single_resolve_and_retry() -> None:
    # Issue #172: a 503 CF interstitial must take the SAME forced-re-solve path as
    # a 403 challenge — not skip it and burn retries against a stale cookie/UA.
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport(
        [
            _cf_challenge_503_response(req),  # first → 503 challenge → re-solve+retry
            httpx.Response(200, json={"ok": True}, request=req),  # retry succeeds
        ]
    )
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare+encrypted", solver=solver)

    out = await ctx.get_json("https://comix/api")

    assert out == {"ok": True}
    assert solver.forced == 1  # exactly ONE forced re-solve, same as the 403 path
    assert len(transport.calls) == 2  # initial + single retry


@pytest.mark.asyncio
async def test_cloudflare_second_challenge_after_resolve_is_terminal() -> None:
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport(
        [
            _cf_challenge_response(req),  # challenge → re-solve
            _cf_challenge_response(req),  # still challenge after re-solve → terminal
        ]
    )
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare", solver=solver)

    with pytest.raises(SourceError):
        await ctx.get_json("https://comix/api")
    assert solver.forced == 1  # bounded — only ONE re-solve (T-04-07)
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_cloudflare_challenge_surviving_resolve_logs_distinct_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # debug comix-recent-403 follow-up: when a CF challenge SURVIVES the forced
    # re-solve (the "headless can't solve" signature), the gateway emits a distinct
    # WARN naming the source + GATEWAY_CLOUDFLARE_HEADLESS, so it is diagnosable from
    # logs immediately rather than only via the later generic "upstream 403" warning.
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport(
        [
            _cf_challenge_response(req),  # challenge → re-solve
            _cf_challenge_response(req),  # still challenge after re-solve → survived
        ]
    )
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare", solver=solver)

    with caplog.at_level("WARNING", logger="manga_gateway"), pytest.raises(SourceError):
        await ctx.get_json("https://comix/api")

    survived = [
        r for r in caplog.records if "SURVIVED a forced re-solve" in r.getMessage()
    ]
    assert len(survived) == 1
    msg = survived[0].getMessage()
    assert "comix" in msg  # names the source
    assert "GATEWAY_CLOUDFLARE_HEADLESS" in msg  # points at the lever


@pytest.mark.asyncio
async def test_cloudflare_resolved_challenge_logs_no_survival_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Control: when the re-solve CLEARS the challenge (2nd response is 200), the
    # distinct "survived" warning must NOT fire — it is specific to a failed re-solve.
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport(
        [
            _cf_challenge_response(req),  # challenge → re-solve
            httpx.Response(200, json={"ok": True}, request=req),  # cleared
        ]
    )
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare", solver=solver)

    with caplog.at_level("WARNING", logger="manga_gateway"):
        assert await ctx.get_json("https://comix/api") == {"ok": True}
    assert not [
        r for r in caplog.records if "SURVIVED a forced re-solve" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_cloudflare_non_challenge_403_is_terminal_without_resolve() -> None:
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport([httpx.Response(403, request=req)])  # no CF markers
    solver = _CountingSolver()
    ctx = _ctx(transport, antibot="cloudflare", solver=solver)

    with pytest.raises(SourceError):
        await ctx.get_json("https://comix/api")
    assert solver.forced == 0  # not a challenge → no re-solve


@pytest.mark.asyncio
async def test_mangadex_403_stops_immediately_no_resolve() -> None:
    # Regression: MangaDex 403 still STOPs immediately (no re-solve, no solver).
    req = httpx.Request("GET", "https://api.mangadex.org/x")
    transport = _RecordingTransport([httpx.Response(403, request=req)])
    ctx = _ctx(transport, antibot="none", solver=None)

    with pytest.raises(SourceError):
        await ctx.get_json("https://api.mangadex.org/x")
    assert len(transport.calls) == 1


# ───────────────────────── D-39: decrypt seam ─────────────────────────


@pytest.mark.asyncio
async def test_get_bytes_passthrough_when_no_scheme() -> None:
    req = httpx.Request("GET", "https://comix/p/1")
    transport = _RecordingTransport(
        [httpx.Response(200, content=b"raw-bytes", request=req)]
    )
    ctx = _ctx(transport, antibot="cloudflare", solver=_CountingSolver())

    out = await ctx.get_bytes("https://comix/p/1")

    assert out == b"raw-bytes"  # decrypt_scheme=None → unchanged


@pytest.mark.asyncio
async def test_get_bytes_routes_through_registered_scheme() -> None:
    scheme = "test-rot1"

    @register_scheme(scheme)
    def _rot1(body: bytes, config: dict[str, object]) -> bytes:
        return bytes((b + 1) % 256 for b in body)

    try:
        req = httpx.Request("GET", "https://comix/p/1")
        transport = _RecordingTransport(
            [httpx.Response(200, content=b"abc", request=req)]
        )
        ctx = _ctx(
            transport,
            antibot="cloudflare+encrypted",
            solver=_CountingSolver(),
            decrypt_scheme=scheme,
        )

        out = await ctx.get_bytes("https://comix/p/1")

        assert out == bytes((b + 1) % 256 for b in b"abc")
    finally:
        _SCHEMES.pop(scheme, None)


# ─────────────────────── plain-path bypass of the decrypt seam ──────────


@pytest.mark.asyncio
async def test_get_bytes_plain_bypasses_registered_scheme() -> None:
    """``get_bytes_plain`` must NOT run the decrypt seam — Comix's image CDN
    serves plaintext WebP per live recon, so routing the binary blob through a
    registered ``decrypt_scheme`` would corrupt the bytes. This regression test
    pins the seam-bypass even when a scheme IS registered for the source."""
    scheme = "test-rot1-bytes-plain"

    @register_scheme(scheme)
    def _rot1(body: bytes, config: dict[str, object]) -> bytes:  # pragma: no cover
        return bytes((b + 1) % 256 for b in body)

    try:
        plain = b"\x52\x49\x46\x46\x10\x00\x00\x00WEBPVP8 "  # WebP header
        req = httpx.Request("GET", "https://cdn.example.store/si/TOKEN/01.webp")
        transport = _RecordingTransport(
            [httpx.Response(200, content=plain, request=req)]
        )
        ctx = _ctx(
            transport,
            antibot="cloudflare+encrypted",
            solver=_CountingSolver(),
            decrypt_scheme=scheme,
        )

        out = await ctx.get_bytes_plain("https://cdn.example.store/si/TOKEN/01.webp")
        assert out == plain  # untouched by the scheme
    finally:
        _SCHEMES.pop(scheme, None)


@pytest.mark.asyncio
async def test_get_bytes_plain_with_headers_surfaces_response_headers() -> None:
    """``get_bytes_plain_with_headers`` returns ``(bytes, headers)`` so a source can
    run a per-source post-fetch decode keyed by a RESPONSE header — and, like
    ``get_bytes_plain``, NEVER runs the framework decrypt seam (the bytes stay raw
    even when a scheme IS registered for the source)."""
    scheme = "test-rot1-bytes-headers"

    @register_scheme(scheme)
    def _rot1(body: bytes, config: dict[str, object]) -> bytes:  # pragma: no cover
        return bytes((b + 1) % 256 for b in body)

    try:
        plain = b"\x52\x49\x46\x46\x10\x00\x00\x00WEBPVP8 "  # WebP-shaped bytes
        req = httpx.Request("GET", "https://cdn.example.store/si/TOKEN/04.webp")
        transport = _RecordingTransport(
            [
                httpx.Response(
                    200,
                    content=plain,
                    headers={"x-enc-seed": "907127381", "x-enc-len": "4096"},
                    request=req,
                )
            ]
        )
        ctx = _ctx(
            transport,
            antibot="cloudflare+encrypted",
            solver=_CountingSolver(),
            decrypt_scheme=scheme,
        )

        out, headers = await ctx.get_bytes_plain_with_headers(
            "https://cdn.example.store/si/TOKEN/04.webp"
        )

        assert out == plain  # seam stays decrypt-opted-out — bytes UNCHANGED
        assert headers.get("x-enc-seed") == "907127381"
        assert headers["x-enc-len"] == "4096"
    finally:
        _SCHEMES.pop(scheme, None)


@pytest.mark.asyncio
async def test_get_json_plain_bypasses_registered_scheme() -> None:
    """``get_json_plain`` must NOT run the decrypt seam — Comix's search and
    chapter-index endpoints are plaintext per live recon, and routing them
    through a registered scheme would corrupt plaintext JSON."""
    scheme = "test-rot1-json-plain"

    @register_scheme(scheme)
    def _rot1(body: bytes, config: dict[str, object]) -> bytes:  # pragma: no cover
        return bytes((b + 1) % 256 for b in body)

    try:
        plain = b'{"status":"ok","result":{"items":[]}}'
        req = httpx.Request("GET", "https://comix.to/api/v1/manga")
        transport = _RecordingTransport(
            [httpx.Response(200, content=plain, request=req)]
        )
        ctx = _ctx(
            transport,
            antibot="cloudflare+encrypted",
            solver=_CountingSolver(),
            decrypt_scheme=scheme,
        )

        out = await ctx.get_json_plain("https://comix.to/api/v1/manga")
        assert out == {"status": "ok", "result": {"items": []}}
    finally:
        _SCHEMES.pop(scheme, None)


# ───────────────────────── D-36: health failure-feed ─────────────────────────


@pytest.mark.asyncio
async def test_record_success_on_clean_fetch() -> None:
    health = SourceHealth(threshold=3)
    health.record_failure()  # prime the counter
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport([httpx.Response(200, json={"ok": 1}, request=req)])
    ctx = _ctx(
        transport,
        antibot="cloudflare",
        solver=_CountingSolver(),
        source_health=health,
    )

    await ctx.get_json("https://comix/api")

    assert health.consecutive_failures == 0  # success reset the breaker


@pytest.mark.asyncio
async def test_record_failure_on_terminal_failure() -> None:
    health = SourceHealth(threshold=3)
    req = httpx.Request("GET", "https://comix/api")
    transport = _RecordingTransport([httpx.Response(403, request=req)])  # terminal
    ctx = _ctx(
        transport,
        antibot="cloudflare",
        solver=_CountingSolver(),
        source_health=health,
    )

    with pytest.raises(SourceError):
        await ctx.get_json("https://comix/api")

    assert health.consecutive_failures == 1  # terminal failure fed the breaker


# ───────────────────────── D-38: dynamic /caps enabled per source ─────────────────


def test_registry_caps_threads_health_so_a_tripped_breaker_disables() -> None:
    # D-38 + the PATTERNS TTLCache-masking caveat: a tripped Comix breaker flips its
    # SourceCap.enabled in the SAME computation, while MangaDex (no health) stays True.
    from manga_gateway.framework.base import Source
    from manga_gateway.framework.registry import SourceRegistry

    class _FakeSource(Source):
        languages = ["en"]
        id_types = ["x"]
        rate_limit_per_minute = 10

        async def search(self, req, ctx):  # type: ignore[no-untyped-def]
            return []

        async def recent(self, *, languages, limit, since, ctx):  # type: ignore[no-untyped-def]
            return []

        async def fetch_manifest(self, chapter_id, ctx):  # type: ignore[no-untyped-def]
            return []

        async def fetch_image(self, url, ctx):  # type: ignore[no-untyped-def]
            return b""

    class _MangaDexLike(_FakeSource):
        key = "mangadex"
        name = "MangaDex"
        base_url = "https://api.mangadex.org"
        antibot = "none"

    class _ComixLike(_FakeSource):
        key = "comix"
        name = "Comix"
        base_url = "https://comix"
        antibot = "cloudflare+encrypted"

    registry = SourceRegistry()
    registry.register("mangadex")(_MangaDexLike)
    registry.register("comix")(_ComixLike)

    comix_health = SourceHealth(threshold=2)
    comix_health.record_failure()
    comix_health.record_failure()  # breaker OPEN
    assert comix_health.is_enabled is False

    caps = {c.key: c for c in registry.caps({"comix": comix_health})}

    assert caps["comix"].enabled is False  # tripped breaker → disabled in this poll
    assert caps["mangadex"].enabled is True  # no health → unchanged always-enabled


@pytest.mark.asyncio
async def test_caps_route_reports_tripped_breaker_in_same_poll(
    app,
    client,  # type: ignore[no-untyped-def]
) -> None:
    # The 12h skeleton cache must NOT mask a freshly-tripped breaker (D-38). Trip a
    # registered source's health, then assert /caps reports enabled:false immediately.
    registry = app.state.registry
    keys = registry.keys()
    if not keys:  # pragma: no cover - no sources registered in this build
        pytest.skip("no sources registered")
    target = keys[0]

    # Warm the skeleton cache (first poll: target enabled).
    first = (await client.get("/caps")).json()
    assert any(s["key"] == target and s["enabled"] for s in first["sources"])

    # Trip the breaker for the target source via the app-state health map.
    health = SourceHealth(threshold=1)
    health.record_failure()  # OPEN
    app.state.source_health = {target: health}

    # Same poll cadence: the cached skeleton must NOT mask the live enabled flip.
    second = (await client.get("/caps")).json()
    assert any(s["key"] == target and s["enabled"] is False for s in second["sources"])


# ─────────── Issue #171: cold-start manifest race → one bounded re-nav ───────────


_VALID_PAGE_URL = "https://test.wowpic9.store/si/TOKENXXXXXXXXXXXXXX/01.webp"
_COMIX_COMPOSITE = ComixSource._make_composite_chapter_id(
    "9001596", "mr3m0", "cipher-tales", "1"
)


class _ManifestSequenceSolver:
    """Fake solver whose ``fetch_via_browser`` returns queued results in order.

    Backs the Issue #171 cold-race test: stage e.g. ``[[], [url]]`` and assert
    ``fetch_manifest`` re-navigates once and resolves the warm second capture.
    ``fetch_via_browser_paginated`` exists only so ``_solver_from_ctx``'s
    hasattr guard passes — the resolved (non-DEFERRED) composite never walks the
    series.
    """

    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.browser_calls = 0

    async def get_clearance(
        self, source_key: str, *, force_resolve: bool = False
    ) -> Clearance:
        return Clearance(cookies={"cf_clearance": "X"}, user_agent="UA")

    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        self.browser_calls += 1
        return self._results.pop(0)

    async def fetch_via_browser_paginated(  # pragma: no cover — never called
        self, url: str, **kwargs: object
    ) -> object:
        raise AssertionError("resolved composite must not walk the series list")


@pytest.mark.asyncio
async def test_cold_race_empty_manifest_retries_once_then_resolves() -> None:
    # Issue #171: a cold browser yields an EMPTY capture; one re-nav warms it and
    # the second capture resolves the real manifest.
    solver = _ManifestSequenceSolver([[], [_VALID_PAGE_URL]])
    ctx = _ctx(_RecordingTransport([]), antibot="cloudflare+encrypted", solver=solver)

    urls = await ComixSource().fetch_manifest(_COMIX_COMPOSITE, ctx)

    assert urls == [_VALID_PAGE_URL]
    assert solver.browser_calls == 2  # initial empty + ONE warm re-nav


@pytest.mark.asyncio
async def test_persistently_empty_manifest_raises_after_bounded_retry() -> None:
    # A genuinely broken page stays empty across the re-nav → raise, but only after
    # exactly ONE retry (bounded — no hot-loop on a permanently empty chapter).
    solver = _ManifestSequenceSolver([[], []])
    ctx = _ctx(_RecordingTransport([]), antibot="cloudflare+encrypted", solver=solver)

    with pytest.raises(SourceError, match="malformed chapter manifest"):
        await ComixSource().fetch_manifest(_COMIX_COMPOSITE, ctx)
    assert solver.browser_calls == 2  # initial + single bounded retry, then raise


@pytest.mark.asyncio
async def test_populated_but_invalid_manifest_does_not_consume_retry() -> None:
    # A populated-but-malformed manifest (off-domain URL) is a REAL fault, not a
    # cold race — it raises immediately without spending the cold-race re-nav.
    solver = _ManifestSequenceSolver(
        [["https://evil.example/si/TOKENXXXXXXXXXXXXXX/01.webp"]]
    )
    ctx = _ctx(_RecordingTransport([]), antibot="cloudflare+encrypted", solver=solver)

    with pytest.raises(SourceError, match="malformed chapter manifest"):
        await ComixSource().fetch_manifest(_COMIX_COMPOSITE, ctx)
    assert solver.browser_calls == 1  # no retry — only the empty signature retries
