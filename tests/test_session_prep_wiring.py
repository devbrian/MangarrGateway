"""Three-site SourceContext wiring-equivalence guard (Plan 07-02, RESEARCH Pitfall 3).

A new framework kwarg is only as good as its weakest construction site. There are
THREE places that build a per-source ``SourceContext``:

* ``api/routes/search.py:_run_one``  (POST /search fan-out)
* ``api/routes/recent.py:_run_one``  (GET /recent fan-out)
* ``jobs/engine.py:_build_context``  (download surface)

Before this plan ``recent.py`` silently DROPPED ``solver``/``antibot``/
``decrypt_scheme``/``decrypt_config``/``source_health`` — a cloudflare* or
csrf-bootstrap ``/recent`` fan-out went out with no credentials → 403. This module
is the deterministic regression guard: it intercepts every ``SourceContext(...)``
call each site makes for the SAME registered source and asserts the three builds are
EQUIVALENT in the kwargs that matter — including the Phase-7 ``session_prep`` provider.
It FAILS if any one site drops a kwarg (the drift this plan exists to prevent).

All deterministic: a fixed-key in-process app, no real browser warm (conftest
autouse no-ops it), no network — the route handlers are exercised through the ASGI
app and the engine ctx is built directly from its lifespan-wired collaborators.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.framework import context as context_module
from manga_gateway.framework.context import SourceContext

TEST_API_KEY = "test-key-deterministic-0123456789"

# The representative source every site is asked to build a ctx for. MangaDex is
# always registered, non-cloudflare, and session_prep=None — so the equivalence
# holds for the simplest source (a divergent site would still differ on the kwarg
# KEYS even when the values are None/"none").
REPRESENTATIVE_KEY = "mangadex"

# The kwargs whose PRESENCE the three sites must agree on (the drift surface).
# We compare the set of keys each site passes plus a normalized view of the
# salient values so a site that passes a key as a literal None still counts as
# "present" (presence, not truthiness, is what guards against the recent.py gap).
_SALIENT_KEYS = frozenset(
    {
        "source_key",
        "rate_limit_per_minute",
        "session",
        "ratelimiter",
        "handle_store",
        "solver",
        "antibot",
        "decrypt_scheme",
        "decrypt_config",
        "source_health",
        "session_prep",
    }
)


def _settings(**over: object) -> Settings:
    return Settings(api_key=TEST_API_KEY, **over)  # type: ignore[arg-type]


def _stub_source_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the representative source's network hooks so the test never hits the net.

    The ctx is recorded at ``SourceContext.__init__`` time — BEFORE the route calls
    ``src.search``/``src.recent`` — so stubbing the hooks to return empty leaves the
    recorded kwargs intact while removing the only network path (real MangaDex)."""
    from manga_gateway.sources.mangadex import MangaDexSource

    async def _empty_search(self: object, req: object, ctx: object) -> list[Any]:
        return []

    async def _empty_recent(self: object, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(MangaDexSource, "search", _empty_search)
    monkeypatch.setattr(MangaDexSource, "recent", _empty_recent)


class _CtxKwargRecorder:
    """Patch ``SourceContext.__init__`` to record the kwargs each call site passes.

    Records the kwargs for the representative source only (so a multi-source fan-out
    does not mix sites), then forwards to the real constructor so behavior is
    unchanged.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        real_init = SourceContext.__init__

        def _recording_init(ctx: SourceContext, **kwargs: Any) -> None:
            if kwargs.get("source_key") == REPRESENTATIVE_KEY:
                self.calls.append(dict(kwargs))
            real_init(ctx, **kwargs)

        # Patch on both the class and the module reference the routes import.
        monkeypatch.setattr(SourceContext, "__init__", _recording_init)
        monkeypatch.setattr(context_module.SourceContext, "__init__", _recording_init)

    def keys_passed(self) -> list[frozenset[str]]:
        return [frozenset(call.keys()) for call in self.calls]


def _kwarg_fingerprint(call: dict[str, Any]) -> dict[str, Any]:
    """A comparable, identity-stable view of the salient kwargs for one ctx build.

    Provider objects (solver, session, ratelimiter, handle_store, session_prep) are
    compared by ``id`` — every site MUST thread the SAME lifespan-built singleton
    (R1). Scalars compare by value. Presence of every salient key is asserted
    separately (the actual gap guard)."""
    fp: dict[str, Any] = {}
    for key in _SALIENT_KEYS:
        if key not in call:
            fp[key] = "<MISSING>"
            continue
        value = call[key]
        if key in {
            "session",
            "ratelimiter",
            "handle_store",
            "solver",
            "session_prep",
        }:
            fp[key] = id(value) if value is not None else None
        else:
            fp[key] = value
    return fp


async def _exercise_routes(
    app: object, recorder: _CtxKwargRecorder
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drive POST /search and GET /recent for the representative source.

    Returns the (search_call, recent_call) recorded kwargs."""
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    headers = {"X-Api-Key": TEST_API_KEY}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver/api/v1"
    ) as client:
        recorder.calls.clear()
        await client.post(
            "/search",
            headers=headers,
            json={"type": "manga", "query": "x", "sources": [REPRESENTATIVE_KEY]},
        )
        search_calls = [c for c in recorder.calls]
        recorder.calls.clear()
        await client.get(
            "/recent",
            headers=headers,
            params={"sources": REPRESENTATIVE_KEY, "limit": "5"},
        )
        recent_calls = [c for c in recorder.calls]
    assert search_calls, "search route built no SourceContext for the source"
    assert recent_calls, "recent route built no SourceContext for the source"
    return search_calls[0], recent_calls[0]


def _engine_ctx_call(app: object, recorder: _CtxKwargRecorder) -> dict[str, Any]:
    """Build the engine ctx via ``JobEngine._build_context`` for the source."""
    engine = app.state.job_manager._engine  # type: ignore[attr-defined]
    registry = app.state.registry  # type: ignore[attr-defined]
    source = registry.get(REPRESENTATIVE_KEY)()
    recorder.calls.clear()
    engine._build_context(source)
    assert recorder.calls, "engine _build_context built no SourceContext"
    return recorder.calls[0]


async def test_three_ctx_sites_pass_identical_kwarg_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three sites pass the SAME salient kwarg KEYS (the recent.py gap guard)."""
    recorder = _CtxKwargRecorder(monkeypatch)
    _stub_source_hooks(monkeypatch)
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        search_call, recent_call = await _exercise_routes(app, recorder)
        engine_call = _engine_ctx_call(app, recorder)

    for call in (search_call, recent_call, engine_call):
        passed = frozenset(call.keys())
        missing = _SALIENT_KEYS - passed
        assert not missing, f"a ctx site dropped salient kwargs: {sorted(missing)}"


async def test_three_ctx_sites_are_equivalent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three sites build an EQUIVALENT ctx for the same source (R1 singletons)."""
    recorder = _CtxKwargRecorder(monkeypatch)
    _stub_source_hooks(monkeypatch)
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        search_call, recent_call = await _exercise_routes(app, recorder)
        engine_call = _engine_ctx_call(app, recorder)

    search_fp = _kwarg_fingerprint(search_call)
    recent_fp = _kwarg_fingerprint(recent_call)
    engine_fp = _kwarg_fingerprint(engine_call)

    # recent.py was the divergent site (Pitfall 3) — assert it explicitly first so a
    # regression points straight at the gap this plan closed.
    assert recent_fp == search_fp, (
        "recent.py ctx diverges from search.py — the Pitfall-3 gap reopened"
    )
    assert engine_fp == search_fp, "engine _build_context ctx diverges from search.py"


async def test_session_prep_is_the_one_shared_provider_at_every_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every site threads the SAME lifespan-built session_prep provider (R1)."""
    recorder = _CtxKwargRecorder(monkeypatch)
    _stub_source_hooks(monkeypatch)
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        search_call, recent_call = await _exercise_routes(app, recorder)
        engine_call = _engine_ctx_call(app, recorder)
        shared = app.state.session_prep  # type: ignore[attr-defined]

    for call in (search_call, recent_call, engine_call):
        assert "session_prep" in call, "a ctx site omitted session_prep"
        assert call["session_prep"] is shared, (
            "a ctx site threaded a different session_prep instance (not the R1 shared)"
        )
