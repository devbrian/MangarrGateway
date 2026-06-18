"""On-demand (challenge-triggered) Cloudflare clearance (debug pooltimeout-recurrence).

A ``cloudflare_challenge_optional`` source must NOT block on a fresh solve up front:
the cf half of ``_clearance_kwargs`` peeks held clearance only (``solve_if_missing=
False``) on the first attempt, lets the request go out, and a REAL solve happens only
when the response is an actual Cloudflare challenge (``is_cf_challenge`` → the existing
D-35 force-resolve reconcile in ``_request_response``). This is what makes a source
whose CF managed challenge is intermittent self-heal whether or not the challenge is
live — so the antibot config never has to be flipped back and forth.

Root cause this guards (2026-06-18): MangaBall dropped its 2026-06-15 site-wide
challenge (GET / → 200, no ``cf-mitigated``). Eager clearance then routed every request
through the android-solver for a cf_clearance it could never mint (no challenge → no
``challenges.cloudflare.com`` OOPIF → the WebView solve timed out), hanging every
download with a ReadTimeout.

Deterministic — the ``_RecordingTransport`` / ``_ctx`` shape mirrors
``tests/test_context_get_json_array.py``. No real browser, no sidecar, no network.
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.antibot import Clearance
from manga_gateway.framework.context import SourceContext
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


class _OnDemandSolver:
    """Solver mirroring AndroidSolver's hold/force/peek semantics, counting each path.

    ``solve_if_missing=False`` with nothing held → ``None`` and NO solve (the peek);
    ``force_resolve=True`` → discard held + a fresh solve (D-35); otherwise serve held
    or solve-if-missing. Tracks ``solves`` (real solves performed), ``forced``, and
    ``peeks_missing`` (peeks that found nothing) so tests pin the exact path taken.
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


def _ctx(
    transport: _RecordingTransport,
    *,
    solver: _OnDemandSolver,
    cloudflare_challenge_optional: bool,
    antibot: str = "cloudflare",
    source_health: SourceHealth | None = None,
) -> SourceContext:
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager

    return SourceContext(
        source_key="mangaball",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,  # type: ignore[arg-type]
        antibot=antibot,  # type: ignore[arg-type]
        cloudflare_challenge_optional=cloudflare_challenge_optional,
        source_health=source_health,
    )


_URL = "https://mangaball.net/api/v1/title/search-advanced/"


def _cf_challenge(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        content=b"...challenge-platform...",
        request=req,
    )


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": []}, request=req)


# ───────────────────────── no live challenge → never solve ──────────────────────


@pytest.mark.asyncio
async def test_optional_no_challenge_never_solves() -> None:
    """The MangaBall-today case: no challenge fires → the request succeeds directly and
    the solver is NEVER asked to mint a clearance (only peeked, found nothing)."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_ok(req)])
    solver = _OnDemandSolver()
    ctx = _ctx(transport, solver=solver, cloudflare_challenge_optional=True)

    out = await ctx.get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert solver.solves == 0  # no blocking solve EVER
    assert solver.peeks_missing == 1  # first attempt peeked the empty cache
    assert len(transport.calls) == 1  # one request, no challenge retry
    # The request went out WITHOUT a cf_clearance cookie (none held to attach).
    sent_headers = transport.calls[0].get("headers", {})  # type: ignore[union-attr]
    assert "Cookie" not in sent_headers


@pytest.mark.asyncio
async def test_optional_attaches_held_clearance_without_resolving() -> None:
    """If the sidecar already holds a clearance (a prior challenge was solved), the
    peek attaches it — still WITHOUT a fresh solve."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_ok(req)])
    solver = _OnDemandSolver(held=True)
    ctx = _ctx(transport, solver=solver, cloudflare_challenge_optional=True)

    out = await ctx.get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert solver.solves == 0  # served the held clearance, no solve
    assert transport.calls[0]["headers"]["Cookie"] == "cf_clearance=HELD"  # type: ignore[index]


# ───────────────────── live challenge → solve-on-demand + retry ─────────────────


@pytest.mark.asyncio
async def test_optional_challenge_triggers_single_solve_and_retry() -> None:
    """If MangaBall RE-ARMS the challenge: the first (clearance-less) request gets a CF
    403, ``is_cf_challenge`` fires, and the source force-resolves ONCE + retries — no
    config change needed. Proves the same source self-heals the other direction."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_cf_challenge(req), _ok(req)])
    solver = _OnDemandSolver()
    ctx = _ctx(transport, solver=solver, cloudflare_challenge_optional=True)

    out = await ctx.get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert solver.peeks_missing == 1  # first attempt: peek (no solve)
    assert solver.forced == 1  # challenge detected → exactly one forced solve
    assert solver.solves == 1
    assert len(transport.calls) == 2  # original + one retry
    # The RETRY carried the freshly-minted clearance.
    assert transport.calls[1]["headers"]["Cookie"] == "cf_clearance=FRESH"  # type: ignore[index]


# ─────────────────────── eager source byte-for-byte unchanged ───────────────────


@pytest.mark.asyncio
async def test_eager_source_solves_up_front_unchanged() -> None:
    """Regression: a NON-optional cloudflare source (comix/kagane/mangadot) keeps the
    eager path — it mints clearance on the first attempt with no challenge needed."""
    req = httpx.Request("GET", _URL)
    transport = _RecordingTransport([_ok(req)])
    solver = _OnDemandSolver()
    ctx = _ctx(transport, solver=solver, cloudflare_challenge_optional=False)

    out = await ctx.get_json(_URL)

    assert out == {"code": 200, "data": []}
    assert solver.solves == 1  # eager: solved up front
    assert solver.peeks_missing == 0  # never used the peek path
    assert transport.calls[0]["headers"]["Cookie"] == "cf_clearance=FRESH"  # type: ignore[index]
