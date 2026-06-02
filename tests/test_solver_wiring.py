"""Lifespan solver-wiring tests (Plan 04-04 Task 2, BOT-01/BOT-03, criterion #4).

All deterministic — a MOCKED solver/browser, no real Chromium, no real sleeps:

* the lifespan swaps ``NoopSolver`` for a ``CloudflareSolver`` as the ONE shared
  solver (R1/BOT-01) and exposes a per-source ``SourceHealth`` map (D-38);
* a forced Patchright launch failure leaves Comix ``force_disabled`` and NEVER
  aborts startup — the app still serves and MangaDex resolves no-clearance
  (D-33/Pitfall 3 / BOT-01);
* the D-37 recovery watchdog re-probes a tripped breaker at +1h then +6h then
  stays down — asserted with a fake clock (no real waiting).
"""

from __future__ import annotations

import asyncio

import pytest

from manga_gateway.app import _recovery_watchdog, create_app
from manga_gateway.config import Settings
from manga_gateway.framework.antibot import CloudflareSolver
from manga_gateway.framework.health import SourceHealth

TEST_API_KEY = "test-key-deterministic-0123456789"


def _settings(**over: object) -> Settings:
    return Settings(api_key=TEST_API_KEY, **over)  # type: ignore[arg-type]


# ───────────────────────────── lifespan swap (BOT-01) ─────────────────────────────


async def test_lifespan_swaps_in_cloudflare_solver() -> None:
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.solver, CloudflareSolver)
        assert isinstance(app.state.source_health, dict)
        assert "comix" in app.state.source_health
        assert isinstance(app.state.source_health["comix"], SourceHealth)


async def test_lifespan_wires_per_domain_challenge_urls() -> None:
    """#88: the lifespan builds the per-domain ``challenge_urls`` map and passes
    it onto the solver. With only comix registered the map is
    ``{"comix": "https://comix.to/"}`` — guards the app mapping against silent
    drift back to the old single-``challenge_url`` single-pick.
    """
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        assert isinstance(solver, CloudflareSolver)
        assert solver._challenge_urls == {"comix": "https://comix.to/"}


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


async def test_launch_failure_leaves_comix_disabled_and_app_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the eager warm() to blow up — the lifespan must still complete and the
    # app must still serve (MangaDex unaffected). Comix is force_disabled (D-33).
    async def _boom(self: CloudflareSolver) -> None:
        raise RuntimeError("patchright failed to launch")

    monkeypatch.setattr(CloudflareSolver, "warm", _boom)

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        # Startup completed despite the warm() failure.
        assert app.state.source_health["comix"].force_disabled is True
        assert app.state.source_health["comix"].is_enabled is False
        # MangaDex path is untouched.
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
