"""SourceHealth breaker + CloudflareSolver shell + dynamic source_cap() tests.

04-01 Task 3 (D-36/D-38, BOT-01). Deterministic — no clock, no browser, no network.

* ``SourceHealth`` consecutive-failure breaker (D-36): ``is_enabled`` True until N
  failures flip it False; a success resets the counter and re-enables.
* ``force_disabled`` (D-33 eager-launch-failed path) disables regardless of count.
* ``source_cap(health=None)`` keeps ``enabled=True`` (MangaDex regression); an
  open breaker yields ``enabled=False`` (D-38).
* ``CloudflareSolver`` satisfies the runtime_checkable ``AntiBotSolver`` Protocol
  (BOT-01); the Protocol signature is unchanged (D-41).
"""

from __future__ import annotations

import pytest

from manga_gateway.framework.antibot import (
    AntiBotSolver,
    Clearance,
    CloudflareSolver,
    NoopSolver,
)
from manga_gateway.framework.base import Source
from manga_gateway.framework.health import SourceHealth

# ──────────────────────────────── SourceHealth ──────────────────────────────────


def test_health_enabled_initially() -> None:
    assert SourceHealth(threshold=3).is_enabled is True


def test_breaker_opens_at_threshold() -> None:
    h = SourceHealth(threshold=3)
    h.record_failure()
    h.record_failure()
    assert h.is_enabled is True  # below threshold
    h.record_failure()
    assert h.is_enabled is False  # N reached → breaker OPEN


def test_success_resets_breaker() -> None:
    h = SourceHealth(threshold=2)
    h.record_failure()
    h.record_failure()
    assert h.is_enabled is False
    h.record_success()
    assert h.consecutive_failures == 0
    assert h.is_enabled is True


def test_force_disabled_overrides_healthy_counter() -> None:
    h = SourceHealth(threshold=5)
    assert h.is_enabled is True
    h.force_disabled = True
    assert h.is_enabled is False  # D-33: eager Patchright launch failed


def test_success_clears_force_disabled_latch() -> None:
    # #153: a genuine success (on-demand warm / watchdog re-probe) must clear the
    # D-33 eager-launch latch so /caps self-heals — previously force_disabled was
    # sticky until a manual restart.
    h = SourceHealth(threshold=5)
    h.force_disabled = True
    assert h.is_enabled is False
    h.record_success()
    assert h.force_disabled is False
    assert h.is_enabled is True


# ─────────────────────────────── source_cap(health) ─────────────────────────────


class _Dummy(Source):
    key = "dummy"
    name = "Dummy"
    base_url = "https://example.test"
    id_types = ["x"]
    languages = ["en"]
    rate_limit_per_minute = 10

    async def search(self, req: object, ctx: object) -> list:  # type: ignore[override]
        return []

    async def recent(self, **kwargs: object) -> list:  # type: ignore[override]
        return []

    async def fetch_manifest(self, chapter_id: str, ctx: object) -> list[str]:  # type: ignore[override]
        return []

    async def fetch_image(self, url: str, ctx: object) -> bytes:  # type: ignore[override]
        return b""


def test_source_cap_no_health_stays_enabled() -> None:
    # MangaDex regression: no health → enabled True (unchanged behavior).
    assert _Dummy.source_cap().enabled is True
    assert _Dummy.source_cap(health=None).enabled is True


def test_source_cap_open_breaker_disables() -> None:
    h = SourceHealth(threshold=1)
    h.record_failure()  # breaker OPEN
    assert _Dummy.source_cap(health=h).enabled is False


def test_source_declares_decrypt_scheme_default_none() -> None:
    assert _Dummy.decrypt_scheme is None


# ──────────────────────────────── CloudflareSolver ──────────────────────────────


def test_cloudflare_solver_satisfies_protocol() -> None:
    solver = CloudflareSolver()
    assert isinstance(solver, AntiBotSolver)
    # NoopSolver still satisfies it too (Protocol unchanged, D-41).
    assert isinstance(NoopSolver(), AntiBotSolver)


@pytest.mark.asyncio
async def test_cloudflare_solver_non_cloudflare_key_returns_none() -> None:
    # A key absent from ``cloudflare_keys`` resolves to None WITHOUT launching
    # a browser (BOT-01 regression for non-Cloudflare sources). Construct with
    # one explicit cf key and probe an unrelated key — the exclusion is the
    # behavior under test. The real solve flow is exercised in
    # test_solver_lifecycle.py with a mocked browser.
    solver = CloudflareSolver(cloudflare_keys=("cf-source",))
    assert await solver.get_clearance("plain-source") is None


# ──────────────────────────────── conftest fixture ──────────────────────────────


def test_fake_solver_fixture_returns_clearance(fake_solver: object) -> None:
    assert isinstance(fake_solver, AntiBotSolver)


@pytest.mark.asyncio
async def test_fake_solver_clearance_shape(fake_solver: object) -> None:
    clr = await fake_solver.get_clearance("comix")  # type: ignore[attr-defined]
    assert isinstance(clr, Clearance)
    assert clr.cookies == {"cf_clearance": "X"}
    assert clr.user_agent == "UA/1"
