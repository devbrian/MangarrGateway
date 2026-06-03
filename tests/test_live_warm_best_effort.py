"""Gate test: the live session warm is BEST-EFFORT (debug/live-warm-fatal-session-gate).

The live harness shares ONE ``CloudflareSolver`` warmed once for the whole
session (``tests/live/conftest.py::_session_solver``), and every live test —
Cloudflare-gated or not — depends on it. A hard ``asyncio.wait_for(warm(), 60)``
made that warm a fatal, all-or-nothing session gate: when one CF-gated source
(comix) was slow to clear on the datacenter runner, the ``TimeoutError`` errored
EVERY source's tests at fixture setup, including pure-httpx mangadex (nightly
runs 26912244601 / 26912471852 — ``31 errors``).

``_warm_best_effort`` swallows the timeout/failure so non-CF sources proceed and
only the CF-gated source fails on its own clearance need. These gate-run tests
lock that contract in (no browser, no network).

This module also covers ``_session_warm_budget_s`` (debug
datacenter-cf-warm-regression): the warm ceiling must SCALE with the number of
cloudflare-gated domains, since ``warm()`` solves them sequentially and each has
its own 60s inner deadline — a flat 60s ceiling false-fails an N>1 warm.

Importing ``tests.live.conftest`` from the gate is the established pattern
(``tests/test_live_collection.py``).
"""

from __future__ import annotations

import asyncio

import pytest

from tests.live.conftest import (
    _PER_DOMAIN_WARM_BUDGET_S,
    _session_warm_budget_s,
    _warm_best_effort,
)


@pytest.mark.asyncio
async def test_returns_true_when_warm_succeeds() -> None:
    """A clean warm returns True. (Production ``warm()`` returns the list of
    FAILED cloudflare keys; ``[]`` means every domain cleared.)"""

    async def ok() -> list[str]:
        return []

    assert await _warm_best_effort(ok, timeout=5.0) is True


@pytest.mark.asyncio
async def test_swallows_timeout_without_raising() -> None:
    """The actual incident: warm exceeds the ceiling. It MUST return False, not
    raise — otherwise the shared session fixture errors and takes every source
    (incl. non-CF mangadex) down with it."""

    async def slow() -> None:
        await asyncio.sleep(10)

    assert await _warm_best_effort(slow, timeout=0.05) is False


@pytest.mark.asyncio
async def test_swallows_exception_without_raising() -> None:
    """A hard warm failure (e.g. the browser never launches) is also best-effort:
    swallowed and reported False, never propagated to session setup."""

    async def boom() -> None:
        raise RuntimeError("browser never launched")

    assert await _warm_best_effort(boom, timeout=5.0) is False


def test_warm_budget_scales_with_cf_domain_count() -> None:
    """The ceiling is per-domain, not flat: N domains -> N * per-domain budget.

    A flat 60s could not cover a 2-domain warm (each domain owns a 60s inner
    _solve_real deadline), so one slow domain false-failed the whole warm."""
    assert _session_warm_budget_s(2) == _PER_DOMAIN_WARM_BUDGET_S * 2
    assert _session_warm_budget_s(3) == _PER_DOMAIN_WARM_BUDGET_S * 3
    # Strictly increasing in the domain count.
    assert _session_warm_budget_s(2) > _session_warm_budget_s(1)


def test_warm_budget_floored_at_one_domain() -> None:
    """A zero/one-CF session still gets a sane (>= one-domain) budget — never 0."""
    assert _session_warm_budget_s(1) == _PER_DOMAIN_WARM_BUDGET_S
    assert _session_warm_budget_s(0) == _PER_DOMAIN_WARM_BUDGET_S
    assert _session_warm_budget_s(-5) == _PER_DOMAIN_WARM_BUDGET_S


def test_two_domain_budget_exceeds_combined_inner_deadlines() -> None:
    """With two cf domains each owning a 60s inner _solve_real deadline, the outer
    ceiling must exceed their combined 120s so a legitimately-slow-but-clearing
    second domain isn't cancelled — the exact flat-60s failure mode this fixes."""
    assert _session_warm_budget_s(2) > 60.0 * 2
