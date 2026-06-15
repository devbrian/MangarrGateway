"""Gate-deterministic proof of the android-solver reachability gate (#215 Model A).

This test runs UNDER THE GATE (no ``pytest.mark.live``) — it imports the
``_android_solver_reachable`` probe from ``tests.live.conftest`` (importing the
live conftest in the gate is safe and network-free, proven by
``tests/test_live_collection.py``) and exercises the four reachability outcomes
with ``respx`` + ``monkeypatch``, never touching the real network.

The probe converts the OLD unconditional ``ci_skip_reason`` for the three
android sources into a RUNTIME gate: when the home android-solver answers
``GET /healthz`` with 200 the sources RUN; when the URL is unset, the solver
returns a non-200, or the connection fails, the live tests SKIP (not fail) so a
down home-stack triages clean. ``_android_solver_reachable`` is
``functools.cache``-wrapped (a once-per-session probe), so every case clears the
cache first.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.live.conftest import _android_solver_reachable

_SOLVER_URL = "http://REDACTED-INTERNAL-IP:18080"


def test_unset_url_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL unset/empty -> (False, reason mentioning it is not configured)."""
    _android_solver_reachable.cache_clear()
    monkeypatch.delenv("GATEWAY_ANDROID_SOLVER_URL", raising=False)
    reachable, reason = _android_solver_reachable()
    assert reachable is False
    assert "GATEWAY_ANDROID_SOLVER_URL" in reason
    assert "unset" in reason or "not configured" in reason


@respx.mock
def test_healthz_200_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL set + /healthz 200 -> (True, "") (sources RUN)."""
    _android_solver_reachable.cache_clear()
    monkeypatch.setenv("GATEWAY_ANDROID_SOLVER_URL", _SOLVER_URL)
    respx.get(f"{_SOLVER_URL}/healthz").mock(return_value=httpx.Response(200))
    reachable, reason = _android_solver_reachable()
    assert reachable is True
    assert reason == ""


@respx.mock
def test_healthz_503_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL set + /healthz 503 -> (False, reason naming the status)."""
    _android_solver_reachable.cache_clear()
    monkeypatch.setenv("GATEWAY_ANDROID_SOLVER_URL", _SOLVER_URL)
    respx.get(f"{_SOLVER_URL}/healthz").mock(return_value=httpx.Response(503))
    reachable, reason = _android_solver_reachable()
    assert reachable is False
    assert "503" in reason


@respx.mock
def test_connection_error_skips_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL set + connection error -> (False, reason names error type); never raises."""
    _android_solver_reachable.cache_clear()
    monkeypatch.setenv("GATEWAY_ANDROID_SOLVER_URL", _SOLVER_URL)
    respx.get(f"{_SOLVER_URL}/healthz").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    # Must NOT raise — the gate swallows transport errors into a clean skip.
    reachable, reason = _android_solver_reachable()
    assert reachable is False
    assert "ConnectError" in reason
