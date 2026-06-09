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
        # Phase 10: the ONE shared solver is now a SolverRouter composing the
        # Patchright (comix) and Android (mangadot/kagane) backends.
        assert isinstance(solver, SolverRouter)
        assert isinstance(solver._patchright, CloudflareSolver)
        assert isinstance(solver._android, AndroidSolver)
        # The per-source breaker map still covers EVERY cloudflare source,
        # regardless of which engine solves it (both engines).
        assert isinstance(app.state.source_health, dict)
        for key in ("comix", "mangadot", "kagane"):
            assert key in app.state.source_health
            assert isinstance(app.state.source_health[key], SourceHealth)


async def test_engine_partition_splits_patchright_and_android_challenge_urls() -> None:
    """Phase 10 (T-10-13): the lifespan partitions the per-domain challenge-URL map
    by ``Source.solver_engine``. The Patchright leg owns ONLY the patchright sources
    — comix (``https://comix.to/``) — so its browser warm/solve is byte-for-byte
    unchanged and the unclearable-from-Linux mangadot/kagane NEVER enter the
    Patchright warm set. The Android leg covers the android sources (mangadot
    ``https://mangadot.net/`` + kagane ``https://kagane.to/``), routed to the
    redroid-WebView sidecar. Guards against drift that would let either source leak
    into the wrong engine's challenge map.
    """
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, SolverRouter)
        # Patchright leg: comix ONLY (NOT mangadot/kagane).
        assert solver._patchright._challenge_urls == {"comix": "https://comix.to/"}
        # Android leg: mangadot + kagane (NOT comix).
        assert solver._android._challenge_urls == {
            "mangadot": "https://mangadot.net/",
            "kagane": "https://kagane.to/",
        }


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
