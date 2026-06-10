"""SidecarConfig.from_env parsing — env-driven, stdlib-only, offline.

Covers the Phase 11 per-solve proxy knobs (``proxy_solve_timeout_s`` / ``hop_port``
/ ``hop_host``) alongside the locked invariant that the BASE ``solve_timeout_s``
default + parsing are untouched (D-08).
"""

from __future__ import annotations

import pytest
from android_solver.config import (
    ConfigError,
    SidecarConfig,
)

_MINIMAL = {"SOLVER_API_KEY": "k"}


def test_proxy_knob_defaults_present_when_env_unset() -> None:
    cfg = SidecarConfig.from_env(_MINIMAL)
    # hop_host MUST default to the docker-reachable sidecar service name (Pitfall 1).
    assert cfg.hop_host == "android-solver"
    assert cfg.hop_port == 18081
    # The proxied-solve budget is higher than the base solve timeout (D-07).
    assert cfg.proxy_solve_timeout_s == 240.0
    assert cfg.proxy_solve_timeout_s > cfg.solve_timeout_s


def test_proxy_knobs_override_from_env() -> None:
    cfg = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_HOP_HOST": "sidecar-x",
            "SOLVER_HOP_PORT": "19000",
            "SOLVER_PROXY_SOLVE_TIMEOUT_S": "300",
        }
    )
    assert cfg.hop_host == "sidecar-x"
    assert cfg.hop_port == 19000
    assert cfg.proxy_solve_timeout_s == 300.0


def test_non_numeric_proxy_timeout_raises() -> None:
    with pytest.raises(ConfigError):
        SidecarConfig.from_env(
            {"SOLVER_API_KEY": "k", "SOLVER_PROXY_SOLVE_TIMEOUT_S": "soon"}
        )


def test_non_positive_proxy_timeout_raises() -> None:
    with pytest.raises(ConfigError):
        SidecarConfig.from_env(
            {"SOLVER_API_KEY": "k", "SOLVER_PROXY_SOLVE_TIMEOUT_S": "0"}
        )


def test_out_of_range_hop_port_raises() -> None:
    with pytest.raises(ConfigError):
        SidecarConfig.from_env({"SOLVER_API_KEY": "k", "SOLVER_HOP_PORT": "70000"})


def test_non_numeric_hop_port_raises() -> None:
    with pytest.raises(ConfigError):
        SidecarConfig.from_env({"SOLVER_API_KEY": "k", "SOLVER_HOP_PORT": "abc"})


def test_base_solve_timeout_default_unchanged() -> None:
    # D-08: the base no-proxy solve timeout default + parsing must NOT drift.
    assert SidecarConfig.from_env(_MINIMAL).solve_timeout_s == 120.0
    assert (
        SidecarConfig.from_env(
            {"SOLVER_API_KEY": "k", "SOLVER_SOLVE_TIMEOUT_S": "45"}
        ).solve_timeout_s
        == 45.0
    )


def test_service_port_still_parsed_and_guarded() -> None:
    # Generalizing _parse_port must not regress the existing SOLVER_PORT path.
    cfg = SidecarConfig.from_env({"SOLVER_API_KEY": "k", "SOLVER_PORT": "9090"})
    assert cfg.port == 9090
    with pytest.raises(ConfigError):
        SidecarConfig.from_env({"SOLVER_API_KEY": "k", "SOLVER_PORT": "0"})
