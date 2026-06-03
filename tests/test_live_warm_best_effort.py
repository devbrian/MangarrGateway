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

Importing ``tests.live.conftest`` from the gate is the established pattern
(``tests/test_live_collection.py``).
"""

from __future__ import annotations

import asyncio

import pytest

from tests.live.conftest import _warm_best_effort


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
