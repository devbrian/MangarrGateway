"""Decoupled credential-seam invariants (260624-ize: MangaBall doomed-CF-solve fix).

The forced-retry path in ``_request_response`` historically threaded ONE
``force_resolve`` flag through ``_send`` → ``_clearance_kwargs``, driving BOTH
seams of a cf+csrf union source at once. When only the CSRF token went stale on a
``cloudflare_challenge_optional`` source (mangaball.net homepage 200, no live CF
challenge), the forced retry still called the solver with ``force_resolve=True``,
firing a doomed Android solve (no Turnstile → 120s WebView timeout → 30s fan-out
timeout → MangaBall cooldown).

The fix splits ``force_resolve`` into two independent booleans — ``force_cf`` (a
fresh CF solve, D-35) and ``force_csrf`` (a session-prep CSRF refresh, D-03/D-05)
— so the forced retry refreshes ONLY the seam that actually went stale.

These four tests pin the invariants:

* Test 1 — union CSRF-only-stale: refresh ONLY CSRF, never solve (the fix).
* Test 2 — Comix cloudflare-only CF-challenge: still force exactly one re-solve.
* Test 3 — both-stale: force BOTH seams.
* Test 4 — MangaDex (antibot=none): happy path unchanged, no injected headers.

Deterministic — fakes mirror ``_RecordingTransport`` / ``_OnDemandSolver`` (from
``tests/test_on_demand_clearance.py``) and ``_CredsPrep`` (from
``tests/test_session_prep.py``). No real browser, no sidecar, no network.
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.antibot import Clearance
from manga_gateway.framework.context import SourceContext, is_csrf_failure
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.framework.session_prep import SessionCredentials
from manga_gateway.handles.store import HandleStore

# ───────────────────────────── transport / solver / prep fakes ───────────────────


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


class _OnDemandSolver:
    """Solver mirroring AndroidSolver's hold/force/peek semantics, counting paths.

    ``solve_if_missing=False`` with nothing held → ``None`` and NO solve (the peek);
    ``force_resolve=True`` → a fresh solve (D-35); otherwise serve held or
    solve-if-missing. Tracks ``solves`` / ``forced`` / ``peeks_missing`` so a test
    pins the exact path taken.
    """

    USER_AGENT = "Mozilla/5.0 Chrome/cf"

    def __init__(self, *, held: bool = False) -> None:
        self.solves = 0
        self.forced = 0
        self.peeks_missing = 0
        self._held: Clearance | None = (
            Clearance(cookies={"cf_clearance": "HELD"}, user_agent=self.USER_AGENT)
            if held
            else None
        )

    def _fresh(self, marker: str) -> Clearance:
        self._held = Clearance(
            cookies={"cf_clearance": marker}, user_agent=self.USER_AGENT
        )
        return self._held

    async def get_clearance(
        self,
        source_key: str,
        *,
        force_resolve: bool = False,
        solve_if_missing: bool = True,
    ) -> Clearance | None:
        if force_resolve:
            self.forced += 1
            self.solves += 1
            return self._fresh("FRESH")
        if self._held is not None:
            return self._held
        if not solve_if_missing:
            self.peeks_missing += 1
            return None
        self.solves += 1
        return self._fresh("FRESH")


class _CredsPrep:
    """Minimal SessionPrep returning fixed credentials, recording each force_refresh."""

    _TOKEN = "a" * 64

    def __init__(self, cookie: str = "sess-1") -> None:
        self._token = self._TOKEN
        self._cookie = cookie
        self.prepare_calls: list[bool] = []

    async def prepare(
        self, source_key: str, *, force_refresh: bool = False
    ) -> SessionCredentials | None:
        self.prepare_calls.append(force_refresh)
        if force_refresh:
            self._token = self._token[:-1] + "F"  # rotate so a retry differs
        return SessionCredentials(
            cookies={"PHPSESSID": self._cookie}, csrf_token=self._token
        )


def _ctx(
    transport: _RecordingTransport,
    *,
    solver: _OnDemandSolver | None = None,
    session_prep: _CredsPrep | None = None,
    antibot: str = "none",
    cloudflare_challenge_optional: bool = False,
) -> SourceContext:
    """Build a SourceContext wiring BOTH a solver AND a session_prep at once.

    Neither existing ``_ctx`` helper combines the two seams — the union ctx is the
    new wrinkle these tests exercise (a real cf+csrf source like MangaBall).
    """
    return SourceContext(
        source_key="mangaball",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,  # type: ignore[arg-type]
        antibot=antibot,  # type: ignore[arg-type]
        cloudflare_challenge_optional=cloudflare_challenge_optional,
        session_prep=session_prep,  # type: ignore[arg-type]
    )


_URL = "https://mangaball.net/api/v1/title/search-advanced/"


def _csrf_403(req: httpx.Request) -> httpx.Response:
    """A CSRF-failure 403 with NO CF markers (is_cf_challenge False, is_csrf True)."""
    return httpx.Response(
        403,
        json={"error": "CSRF token validation failed"},
        request=req,
    )


def _cf_403(req: httpx.Request) -> httpx.Response:
    """A Cloudflare-challenge 403 (is_cf_challenge True, is_csrf_failure False)."""
    return httpx.Response(
        403,
        headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        content=b"...challenge-platform...",
        request=req,
    )


def _both_403(req: httpx.Request) -> httpx.Response:
    """A 403 that is BOTH a CF challenge AND a CSRF rejection."""
    return httpx.Response(
        403,
        headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        content=b"...challenge-platform... csrf token validation failed",
        request=req,
    )


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": []}, request=req)


# ─────────────────── Test 1: union, CSRF-only-stale → no doomed solve ─────────────


@pytest.mark.asyncio
async def test_union_csrf_only_stale_refreshes_csrf_without_solving() -> None:
    """THE FIX: a union source hit with a CSRF-only-stale 403 (no CF markers)
    refreshes ONLY the CSRF token and retries — the cf half stays peek-only, so the
    solver is NEVER asked to mint clearance (no doomed Android solve)."""
    req = httpx.Request("POST", _URL)
    transport = _RecordingTransport([_csrf_403(req), _ok(req)])
    solver = _OnDemandSolver()
    prep = _CredsPrep()
    ctx = _ctx(
        transport,
        solver=solver,
        session_prep=prep,
        antibot="cloudflare",
        cloudflare_challenge_optional=True,
    )

    out = await ctx.post_json(_URL, data={"search_input": "one piece"})

    assert out == {"code": 200, "data": []}
    assert solver.solves == 0  # no blocking solve EVER
    assert solver.forced == 0  # the doomed forced solve never fires (the fix)
    assert prep.prepare_calls.count(True) == 1  # CSRF seam refreshed exactly once
    assert len(transport.calls) == 2  # original 403 + one retry


# ─────────────────── Test 2: Comix cloudflare-only → one forced solve ─────────────


@pytest.mark.asyncio
async def test_comix_cf_challenge_forces_one_resolve() -> None:
    """Comix invariant: a cloudflare-only (eager) source hit with a CF-challenge 403
    still forces EXACTLY one CF re-solve + one retry — byte-for-byte unchanged."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_cf_403(req), _ok(req)])
    solver = _OnDemandSolver()
    ctx = _ctx(
        transport,
        solver=solver,
        session_prep=None,
        antibot="cloudflare",
        cloudflare_challenge_optional=False,
    )

    out = await ctx.get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert solver.forced == 1  # exactly one forced CF re-solve
    assert len(transport.calls) == 2  # original + one retry


# ─────────────────────── Test 3: both-stale → both seams forced ───────────────────


@pytest.mark.asyncio
async def test_both_stale_forces_both_seams() -> None:
    """Both-stale: a 403 that is BOTH a CF challenge AND a CSRF rejection on a union
    source forces BOTH a CF re-solve and a CSRF refresh."""
    req = httpx.Request("POST", _URL)
    transport = _RecordingTransport([_both_403(req), _ok(req)])
    solver = _OnDemandSolver()
    prep = _CredsPrep()
    ctx = _ctx(
        transport,
        solver=solver,
        session_prep=prep,
        antibot="cloudflare",
        cloudflare_challenge_optional=True,
    )

    out = await ctx.post_json(_URL, data={"search_input": "x"})

    assert out == {"code": 200, "data": []}
    assert solver.forced == 1  # CF re-solved
    assert prep.prepare_calls.count(True) == 1  # CSRF refreshed
    assert len(transport.calls) == 2  # original + one retry


# ───────────────────────── Test 4: MangaDex happy path unchanged ──────────────────


@pytest.mark.asyncio
async def test_mangadex_no_antibot_unchanged() -> None:
    """MangaDex invariant: antibot=none + solver=None + session_prep=None has no
    reconcile path; the happy path injects no Cookie / X-CSRF-Token header."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_ok(req)])
    ctx = _ctx(transport, solver=None, session_prep=None, antibot="none")

    out = await ctx.get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert len(transport.calls) == 1  # one request, no reconcile retry
    sent_headers = transport.calls[0].get("headers", {})  # type: ignore[union-attr]
    assert "Cookie" not in sent_headers
    assert "X-CSRF-Token" not in sent_headers


# Imported for symmetry with the reference modules / to assert the predicate import
# stays wired (the both-stale body must satisfy is_csrf_failure).
def test_both_stale_body_satisfies_csrf_predicate() -> None:
    req = httpx.Request("POST", _URL)
    assert is_csrf_failure(_both_403(req)) is True


# ─────────────────── Phase 16 Task 3: D-08 origin-403 rotation branch ───────────────
#
# An opted-in source's PINNED IP that hits an ORIGIN reputation-block 403 (no CF
# challenge, no CSRF, no WAF) rotates to a fresh IP, re-solves on it, and retries. A CF
# 403 re-solves IN PLACE (no rotation). Budget exhaustion → source_unavailable. A
# non-opted source NEVER rotates (regression).

from manga_gateway.framework.context import is_cf_challenge  # noqa: E402


class _FakePin:
    """A minimal PooledProxy-shaped stand-in with a distinct selection_key/identity."""

    def __init__(self, port: int) -> None:
        self._port = port

    @property
    def selection_key(self) -> str:
        return f"proxy.invalid:{self._port}"

    @property
    def identity(self) -> str:
        return f"proxy.invalid:{self._port}"

    def as_solve_dict(self) -> dict[str, str]:
        return {"server": f"http://proxy.invalid:{self._port}"}


class _FakePool:
    """A ProxyPool-shaped fake whose ``transport_for`` always returns ONE transport.

    The origin-403 rotation tests route ALL pinned egress through the same recording
    transport (the test asserts on rotation bookkeeping + the /403 sequence, not on the
    physical proxy hop), so ``transport_for`` ignores the proxy and returns the session
    transport handed in.
    """

    def __init__(self, transport: _RecordingTransport) -> None:
        self._transport = transport

    def transport_for(self, proxy: object) -> _RecordingTransport:
        return self._transport


class _CountingPins:
    """A SourcePinnedProxies-shaped fake that counts rotate() calls and hands out pins.

    ``get_or_acquire`` pins port 8000; ``rotate`` cycles 8001, 8002, ... until
    ``available`` is spent, then returns ``None`` (exhaustion). ``current`` peeks.
    ``pool`` exposes a fake pool so ``_send_emitting`` can resolve a transport for the
    active pin (the SEARCH ctx is not given a ``proxy_pool``, Phase 16).
    """

    def __init__(self, *, available: int, transport: _RecordingTransport) -> None:
        self.rotate_calls = 0
        self._available = available
        self._pin: _FakePin | None = None
        self._pool = _FakePool(transport)

    @property
    def pool(self) -> _FakePool:
        return self._pool

    def get_or_acquire(self, source_key: str) -> _FakePin | None:
        if self._pin is None:
            self._pin = _FakePin(8000)
        return self._pin

    def current(self, source_key: str) -> _FakePin | None:
        return self._pin

    def rotate(self, source_key: str, *, exclude: set[str]) -> _FakePin | None:
        self.rotate_calls += 1
        if self.rotate_calls > self._available:
            self._pin = None
            return None
        self._pin = _FakePin(8000 + self.rotate_calls)
        return self._pin


def _origin_403(req: httpx.Request) -> httpx.Response:
    """An openresty origin-403: NO cf-mitigated header, NO CF body markers, NOT CSRF."""
    return httpx.Response(
        403,
        headers={"server": "openresty"},
        content=b"<html>403 Forbidden (openresty)</html>",
        request=req,
    )


def _origin_ctx(
    transport: _RecordingTransport,
    pins: object,
    *,
    solver: _OnDemandSolver | None = None,
    opted: bool = True,
) -> SourceContext:
    """A cloudflare opted-in (or non-opted) ctx wiring the pinned-proxy singleton."""

    def _is_origin_block(resp: httpx.Response) -> bool:
        return resp.status_code == 403 and not is_cf_challenge(resp)

    return SourceContext(
        source_key="mangadot",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,  # type: ignore[arg-type]
        antibot="cloudflare",
        source_pins=pins,  # type: ignore[arg-type]
        solve_search_via_proxy_pool=opted,
        is_origin_block_fn=_is_origin_block if opted else None,
        image_proxy_max_attempts=3,
    )


@pytest.mark.asyncio
async def test_origin_block_rotates_resolves_and_retries_to_success() -> None:
    """(a) An opted-in origin-403 triggers ONE rotation + a forced re-solve on the new
    IP + a retry that succeeds."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_origin_403(req), _ok(req)])
    pins = _CountingPins(available=3, transport=transport)
    solver = _OnDemandSolver(held=True)  # held so the first send does not solve

    out = await _origin_ctx(transport, pins, solver=solver).get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert pins.rotate_calls == 1  # exactly ONE rotation
    assert solver.forced == 1  # re-solved on the rotated IP (D-05)
    assert len(transport.calls) == 2  # origin-403 + the rotated retry


@pytest.mark.asyncio
async def test_cf_challenge_403_resolves_in_place_without_rotating() -> None:
    """(b) A CF-challenge 403 re-solves IN PLACE — NO rotation (D-35 unchanged)."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_cf_403(req), _ok(req)])
    pins = _CountingPins(available=3, transport=transport)
    solver = _OnDemandSolver()

    out = await _origin_ctx(transport, pins, solver=solver).get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert pins.rotate_calls == 0  # CF re-solves in place, never rotates
    assert solver.forced == 1  # exactly one in-place forced re-solve
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_origin_block_exhaustion_raises_source_unavailable() -> None:
    """(c) Budget exhaustion (rotate returns None) → terminal source_unavailable."""
    req = httpx.Request("GET", _URL)
    # Every send returns an origin-403; the pool exhausts after 1 rotate.
    transport = _RecordingTransport([_origin_403(req) for _ in range(6)])
    pins = _CountingPins(available=0, transport=transport)  # first rotate returns None
    solver = _OnDemandSolver(held=True)

    with pytest.raises(SourceError) as exc:
        await _origin_ctx(transport, pins, solver=solver).get_json(_URL)
    assert exc.value.code == "source_unavailable"
    assert pins.rotate_calls == 1  # tried to rotate once, got None → terminal


@pytest.mark.asyncio
async def test_non_opted_source_origin_403_never_rotates() -> None:
    """(d) A non-opted source's origin-403 NEVER rotates — it hits the unchanged
    source_unavailable raise (regression: _is_origin_block_fn is None)."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_origin_403(req)])
    pins = _CountingPins(available=3, transport=transport)

    with pytest.raises(SourceError) as exc:
        await _origin_ctx(transport, pins, opted=False).get_json(_URL)
    assert exc.value.code == "source_unavailable"
    assert pins.rotate_calls == 0  # non-opted ⇒ predicate None ⇒ no rotation
    assert len(transport.calls) == 1  # one request, no retry
