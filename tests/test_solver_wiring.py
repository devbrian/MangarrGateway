"""Lifespan solver-wiring tests (Plan 04-04 Task 2 + Phase 10, BOT-01/SRC-01/SRC-02).

All deterministic — a MOCKED solver/browser, no real Chromium, no real sleeps:

* the lifespan swaps ``NoopSolver`` for a ``SolverRouter`` as the ONE shared
  solver (R1/BOT-01): its Patchright leg is a ``CloudflareSolver`` owning the
  patchright sources (comix), its Android leg an ``AndroidSolver`` covering the
  android sources (mangadot/kagane). A per-source ``SourceHealth`` map (D-38)
  still covers EVERY cloudflare source across both engines;
* the Patchright leg's ``_challenge_urls`` is partitioned to patchright sources
  ONLY — comix is present, mangadot/kagane are NOT (they route to the Android
  leg) — so comix's browser warm/solve stays byte-for-byte unchanged;
* a Patchright warm failure leaves Comix ``force_disabled`` and NEVER aborts
  startup — the app still serves and MangaDex resolves no-clearance
  (D-33/Pitfall 3 / BOT-01);
* the D-37 recovery watchdog re-probes a tripped breaker at +1h then +6h then
  stays down — asserted with a fake clock (no real waiting).
"""

from __future__ import annotations

import asyncio

import pytest

from manga_gateway.app import _recovery_watchdog, create_app
from manga_gateway.config import Settings
from manga_gateway.framework.android_solver import AndroidSolver
from manga_gateway.framework.antibot import CloudflareSolver
from manga_gateway.framework.health import SourceHealth
from manga_gateway.framework.solver_router import SolverRouter

TEST_API_KEY = "test-key-deterministic-0123456789"


def _settings(**over: object) -> Settings:
    return Settings(api_key=TEST_API_KEY, **over)  # type: ignore[arg-type]


# ───────────────────────────── lifespan swap (BOT-01) ─────────────────────────────


async def test_lifespan_swaps_in_solver_router() -> None:
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        # Phase 10 / Phase 14: the ONE shared solver is a SolverRouter composing the
        # Patchright (mangafire) and Android (comix/mangadot/kagane/mangaball) backends.
        assert isinstance(solver, SolverRouter)
        assert isinstance(solver._patchright, CloudflareSolver)
        # Single-lane collapse: the sole "default" lane is the android backend.
        assert isinstance(solver._android_by_lane["default"], AndroidSolver)
        # The per-source breaker map still covers EVERY cloudflare source,
        # regardless of which engine solves it (both engines).
        assert isinstance(app.state.source_health, dict)
        for key in ("comix", "mangadot", "kagane"):
            assert key in app.state.source_health
            assert isinstance(app.state.source_health[key], SourceHealth)


async def test_engine_partition_splits_patchright_and_android_challenge_urls() -> None:
    """Phase 10 (T-10-13) / Phase 14: the lifespan partitions the per-domain
    challenge-URL map by ``Source.solver_engine``. The Patchright leg now owns ONLY
    mangafire (``https://mangafire.to/``, Phase 12) — comix moved to the Android leg
    in Phase 14 (comix.to 403s the Patchright-Linux fingerprint even WITH a
    cf_clearance; the redroid WebView is the only fingerprint that clears it). The
    Android leg covers comix (``https://comix.to/``) plus the other android sources
    (mangadot ``https://mangadot.net/``, kagane ``https://kagane.to/``, mangaball
    ``https://mangaball.net/``), all routed to the redroid-WebView sidecar. Guards
    against drift that would let a source leak into the wrong engine's challenge map.
    """
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        # Patchright leg: mangafire only (comix moved to android in Phase 14).
        assert solver._patchright._challenge_urls == {
            "mangafire": "https://mangafire.to/",
        }
        # Android leg (single-lane collapse — the "default" lane carries all android
        # sources): comix + mangadot + kagane + mangaball (NOT mangafire).
        assert solver._android_by_lane["default"]._challenge_urls == {
            "comix": "https://comix.to/",
            "mangadot": "https://mangadot.net/",
            "kagane": "https://kagane.to/",
            "mangaball": "https://mangaball.net/",
        }


async def test_on_demand_source_wired_into_warm_skip_and_bootstrap_peek() -> None:
    """On-demand clearance wiring (debug pooltimeout-recurrence): mangaball
    (``cloudflare_challenge_optional=True``) must be (a) in the Android leg's
    ``on_demand_keys`` so ``warm()`` skips it, and (b) routed through the csrf-bootstrap
    clearance provider with ``solve_if_missing=False`` so the bootstrap GET never
    eagerly solves an absent challenge. An eager source (comix) keeps the eager path.
    """
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        # (a) warm-skip wiring: mangaball is on-demand on the Android leg; mangafire
        # is on-demand on the Patchright leg (260623-m5h: search computes the vrf
        # in-process and answers cold over httpx, so it defer-solves Cloudflare).
        assert "mangaball" in solver._android_by_lane["default"]._on_demand_keys
        assert solver._patchright._on_demand_keys == frozenset({"mangafire"})

        # (b) bootstrap provider peeks for an on-demand source, stays eager otherwise.
        calls: list[tuple[str, dict[str, object]]] = []

        async def _spy(source_key: str, **kwargs: object) -> None:
            calls.append((source_key, kwargs))
            return None

        solver.get_clearance = _spy  # type: ignore[method-assign]
        provider = app.state.session_prep._get_clearance
        await provider("mangaball")
        await provider("comix")

    by_key = dict(calls)
    assert by_key["mangaball"] == {"solve_if_missing": False}  # peek, no eager solve
    assert by_key["comix"] == {}  # eager (no solve_if_missing forwarded)


# ───────────────── per-lane construction + single-lane collapse (LANE-02) ─────────


async def test_single_lane_collapse_builds_exactly_one_android_solver() -> None:
    """CRITICAL regression guard (LANE-02): with NO lane config the app builds EXACTLY
    ONE AndroidSolver (one device lock), adb_target=None, lane="default"; the router's
    android_by_lane has length 1 and eval_backend IS that one instance — single-lane
    collapse, byte-for-byte today."""
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        # exactly one lane instance
        assert list(solver._android_by_lane) == ["default"]
        only = solver._android_by_lane["default"]
        assert isinstance(only, AndroidSolver)
        # no target sent (single-lane collapse), default lane label
        assert only._adb_target is None
        assert only._lane == "default"
        # eval_backend is that one instance (the off-Protocol eval passthrough target)
        assert solver._eval_backend is only
        # source_lane_map empty (no per-source lane routing)
        assert solver._source_lane_map == {}


async def test_two_lanes_slice_kagane_onto_its_own_solver() -> None:
    """With a dedicated kagane lane: TWO AndroidSolver instances; the kagane instance
    carries adb_target + lane + ONLY kagane's challenge_url; the default instance
    carries the rest and the page-holder (comix) is the eval_backend."""
    app = create_app(
        _settings(
            android_lanes={
                "default": "redroid:5555",
                "kagane": "redroid-kagane:5555",
            },
            android_source_lane_map={"kagane": "kagane"},
        )
    )
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        assert set(solver._android_by_lane) == {"default", "kagane"}
        kagane = solver._android_by_lane["kagane"]
        default = solver._android_by_lane["default"]
        # kagane lane: its own device target + lane label + ONLY its challenge_url
        assert kagane._adb_target == "redroid-kagane:5555"
        assert kagane._lane == "kagane"
        assert kagane._challenge_urls == {"kagane": "https://kagane.to/"}
        # default lane: the other android sources, its own device target
        assert default._adb_target == "redroid:5555"
        assert default._lane == "default"
        assert "kagane" not in default._challenge_urls
        assert "comix" in default._challenge_urls  # page-holder rides the default lane
        # the page-holder (comix) lane's instance is the eval_backend
        assert solver._eval_backend is default
        # the source_lane_map is threaded through for per-source dispatch
        assert solver._source_lane_map == {"kagane": "kagane"}


async def test_unknown_lane_map_source_key_fails_loud() -> None:
    """LANE-01 fail-loud (PR #324 review): a typo'd SOURCE key in
    android_source_lane_map is rejected at startup, NOT silently dropped (which would
    leave the real source on the shared default lane and defeat the lane config)."""
    app = create_app(
        _settings(
            android_lanes={
                "default": "redroid:5555",
                "kagane": "redroid-kagane:5555",
            },
            android_source_lane_map={"kgane": "kagane"},  # typo: should be "kagane"
        )
    )
    with pytest.raises(ValueError, match="unknown source key"):
        async with app.router.lifespan_context(app):
            pass


async def test_disabled_source_in_lane_map_does_not_block_startup() -> None:
    """A source intentionally disabled via GATEWAY_DISABLED_SOURCES but left in the
    lane map must NOT fail startup — only genuine typos fail loud (the disabled source
    is a known android source, just dropped from the registry for this deploy)."""
    app = create_app(
        _settings(
            android_lanes={
                "default": "redroid:5555",
                "kagane": "redroid-kagane:5555",
            },
            android_source_lane_map={"kagane": "kagane"},
            disabled_sources="kagane",
        )
    )
    # Reaching the body (no raise) proves the disabled source was exempted.
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.solver, SolverRouter)


# ─────────── Phase 16 Task 1: pinned-proxy singleton threaded into solvers ───────────


async def test_android_solver_receives_pinned_proxy_singleton() -> None:
    """PROXY-04: every per-lane AndroidSolver is handed the ONE
    ``app.state.source_pinned_proxies`` singleton (same identity) so the /solve body
    can read the SAME pin the search/image legs use (one egress IP)."""
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        singleton = app.state.source_pinned_proxies
        for android in solver._android_by_lane.values():
            assert android._source_pins is singleton


async def test_android_solver_opted_in_keys_sliced_per_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PROXY-04: each AndroidSolver receives only its OWN lane's slice of the opted-in
    ``solve_search_via_proxy_pool`` source keys, derived via a registry comprehension
    (never a key literal). Force-opt-in mangadot + kagane, then split kagane onto its
    own lane and assert each solver carries only its slice."""
    from manga_gateway.sources.kagane import KaganeSource
    from manga_gateway.sources.mangadot import MangadotSource

    monkeypatch.setattr(MangadotSource, "solve_search_via_proxy_pool", True)
    monkeypatch.setattr(KaganeSource, "solve_search_via_proxy_pool", True)

    app = create_app(
        _settings(
            android_lanes={
                "default": "redroid:5555",
                "kagane": "redroid-kagane:5555",
            },
            android_source_lane_map={"kagane": "kagane"},
        )
    )
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        # kagane lane carries ONLY kagane; the default lane has mangadot (NOT kagane).
        assert solver._android_by_lane["kagane"]._solve_search_keys == frozenset(
            {"kagane"}
        )
        default_keys = solver._android_by_lane["default"]._solve_search_keys
        assert "mangadot" in default_keys
        assert "kagane" not in default_keys


async def test_android_solver_non_opted_source_keeps_global_proxy() -> None:
    """Regression-safe default: a source that does NOT opt in
    (``solve_search_via_proxy_pool`` left False) never enters any AndroidSolver's
    opted-in slice ⇒ its /solve body keeps the global proxy (byte-for-byte today).

    Pre-Plan-03 this asserted EVERY slice was empty; Plan 03 (PROXY-08) opts mangadot
    in, so the invariant is now the per-source one the slice actually protects: kagane
    (the no-regression control) is never in any slice, while only opted-in sources are.
    """
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        all_keys: frozenset[str] = frozenset()
        for android in solver._android_by_lane.values():
            all_keys |= android._solve_search_keys
        # kagane (control) must NOT be opted in — it stays on the global /solve proxy.
        assert "kagane" not in all_keys
        # The only opted-in source in the default registry is mangadot (PROXY-08).
        assert all_keys == frozenset({"mangadot"})


async def test_mangadex_resolves_no_clearance_with_real_solver() -> None:
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        # BOT-01: a non-cloudflare key resolves to None even with the CF solver wired.
        assert await solver.get_clearance("mangadex") is None


async def test_jobmanager_receives_solver_and_health() -> None:
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        jm = app.state.job_manager
        # Threaded through to the engine (params Plan 02 added).
        assert jm._engine._solver is app.state.solver
        assert jm._engine._source_health is app.state.source_health


# ──────────────────── non-blocking launch (D-33/Pitfall 3) ────────────────────


async def test_patchright_warm_failure_leaves_comix_disabled_and_app_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Patchright warm failure surfaces as comix in the warm() failed-keys list
    # (CloudflareSolver._warm_one returns False on a launch/solve failure → warm()
    # reports the key). The router unions that with the android leg's failures and
    # the lifespan force-disables exactly those — but startup must still complete and
    # the app must still serve, MangaDex unaffected (D-33/Pitfall 3).
    async def _comix_failed(self: CloudflareSolver) -> list[str]:
        return ["comix"]

    monkeypatch.setattr(CloudflareSolver, "warm", _comix_failed)

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        # Startup completed despite the warm() failure; comix booted disabled.
        assert app.state.source_health["comix"].force_disabled is True
        assert app.state.source_health["comix"].is_enabled is False
        # MangaDex path is untouched (router dispatches a non-cf key to None).
        assert await app.state.solver.get_clearance("mangadex") is None


# ──────────────────── D-37 recovery watchdog (fake clock) ────────────────────


async def test_watchdog_reprobes_at_plus_1h_then_6h_then_stays_down() -> None:
    health = SourceHealth(threshold=1)
    health.record_failure()  # trip the breaker
    assert health.is_enabled is False

    slept_hours: list[float] = []
    probe_calls: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        slept_hours.append(seconds / 3600.0)

    async def probe() -> bool:
        probe_calls.append(1)
        return False  # re-probe keeps failing → stays down

    await _recovery_watchdog(
        health, backoff_hours=(1, 6), sleep=fake_sleep, probe=probe
    )

    # Re-probed exactly twice, at +1h then +6h, then stops (no busy retry).
    assert slept_hours == [1.0, 6.0]
    assert len(probe_calls) == 2
    assert health.is_enabled is False


async def test_watchdog_reenables_when_probe_recovers() -> None:
    health = SourceHealth(threshold=1)
    health.record_failure()
    assert health.is_enabled is False

    async def fake_sleep(seconds: float) -> None:
        return None

    async def probe() -> bool:
        return True  # recovered on the first re-probe

    await _recovery_watchdog(
        health, backoff_hours=(1, 6), sleep=fake_sleep, probe=probe
    )
    assert health.is_enabled is True  # breaker reset on recovery


async def test_watchdog_cancellable() -> None:
    health = SourceHealth(threshold=1)
    health.record_failure()

    async def slow_sleep(seconds: float) -> None:
        await asyncio.sleep(10)

    async def probe() -> bool:
        return False

    task = asyncio.create_task(
        _recovery_watchdog(health, backoff_hours=(1, 6), sleep=slow_sleep, probe=probe)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watchdog_reenables_force_disabled_source_on_probe_recovery() -> None:
    # #153: an eager-launch-flake (force_disabled) source now recovers OFF the
    # request path — a successful watchdog re-probe clears the D-33 latch
    # (record_success resets force_disabled), instead of staying down until restart.
    health = SourceHealth(threshold=5)
    health.force_disabled = True
    assert health.is_enabled is False

    async def fake_sleep(seconds: float) -> None:
        return None

    async def probe() -> bool:
        return True

    await _recovery_watchdog(
        health, backoff_hours=(1, 6), sleep=fake_sleep, probe=probe
    )
    assert health.force_disabled is False
    assert health.is_enabled is True


# ──────────────────── eager-warm bounded retry (#153) ────────────────────
#
# These exercise ``_warm_one`` directly — the unit holding all the new retry /
# backoff / give-up logic. ``warm()`` itself is a trivial loop that appends the
# keys ``_warm_one`` reports failed, and the conftest autouse fixture stubs
# ``warm`` to a no-op for every deterministic test, so hitting ``_warm_one`` is
# both the honest unit boundary and stub-proof.


async def test_warm_one_retry_absorbs_cold_start_flake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #153: a transient first-attempt warm failure is absorbed by retry, so the
    # source is NOT reported failed and the lifespan never force_disables it.
    solver = CloudflareSolver(cloudflare_keys=("comix",), warm_attempts=3)

    attempts: list[int] = []

    async def flaky_clearance(key: str) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("cold persistent-context launch race")
        return None  # success on the second attempt

    monkeypatch.setattr(solver, "get_clearance", flaky_clearance)

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    ok = await solver._warm_one("comix", sleep=fake_sleep)

    assert ok is True  # absorbed → comix stays enabled
    assert len(attempts) == 2  # one retry
    assert slept == [2.0]  # one backoff between attempts (warm_retry_seconds * 1)


async def test_warm_one_reports_failed_after_exhausting_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #153: when every attempt fails the source is reported failed (warm() then
    # surfaces it so the lifespan force_disables it) — but only AFTER the bounded
    # retry budget is spent, and with backoff ONLY between attempts.
    solver = CloudflareSolver(
        cloudflare_keys=("comix",), warm_attempts=2, warm_retry_seconds=1.0
    )

    attempts: list[int] = []

    async def always_boom(key: str) -> None:
        attempts.append(1)
        raise RuntimeError("patchright failed to launch")

    monkeypatch.setattr(solver, "get_clearance", always_boom)

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    ok = await solver._warm_one("comix", sleep=fake_sleep)

    assert ok is False  # exhausted → warm() reports it failed
    assert len(attempts) == 2  # both attempts spent
    assert slept == [1.0]  # backoff only BETWEEN attempts (no trailing sleep)
