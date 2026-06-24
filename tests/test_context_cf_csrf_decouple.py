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
