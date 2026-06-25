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


# ── multi-target registry + per-target allowlist (LANE-03 / SEC-01) ───────────


def test_adb_targets_defaults_to_single_target() -> None:
    # Default collapse: no SOLVER_ADB_TARGETS -> the registry is exactly the one
    # existing SOLVER_ADB_TARGET (byte-for-byte today).
    cfg = SidecarConfig.from_env(_MINIMAL)
    assert cfg.adb_targets == ("redroid:5555",)
    assert cfg.adb_target == "redroid:5555"


def test_adb_targets_parses_comma_list_default_first() -> None:
    cfg = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_ADB_TARGET": "redroid:5555",
            "SOLVER_ADB_TARGETS": "redroid:5555,redroid-kagane:5555",
        }
    )
    assert cfg.adb_targets == ("redroid:5555", "redroid-kagane:5555")
    # The DEFAULT target stays the existing SOLVER_ADB_TARGET.
    assert cfg.adb_target == "redroid:5555"


def test_adb_targets_always_contains_default() -> None:
    # SEC-01 invariant: the default target is always a member of the registry,
    # even when SOLVER_ADB_TARGETS omits it.
    cfg = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_ADB_TARGET": "redroid:5555",
            "SOLVER_ADB_TARGETS": "redroid-kagane:5555",
        }
    )
    assert cfg.adb_target in cfg.adb_targets
    assert "redroid-kagane:5555" in cfg.adb_targets


def test_malformed_adb_targets_falls_back_to_single() -> None:
    # All-blank entries -> fall back to the single SOLVER_ADB_TARGET (default).
    cfg = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_ADB_TARGET": "redroid:5555",
            "SOLVER_ADB_TARGETS": " , , ",
        }
    )
    assert cfg.adb_targets == ("redroid:5555",)


def test_allowed_hosts_for_defaults_to_global() -> None:
    cfg = SidecarConfig.from_env(_MINIMAL)
    # No per-target map -> every target uses the global allowlist.
    assert cfg.allowed_hosts_for("redroid:5555") == cfg.allowed_hosts


def test_allowed_hosts_for_scopes_per_target() -> None:
    cfg = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_ADB_TARGETS": "redroid:5555,redroid-kagane:5555",
            "SOLVER_ALLOWED_HOSTS_BY_TARGET": '{"redroid-kagane:5555":"kagane.to"}',
        }
    )
    assert cfg.allowed_hosts_for("redroid-kagane:5555") == frozenset({"kagane.to"})
    # An unscoped target still falls back to the global allowlist.
    assert cfg.allowed_hosts_for("redroid:5555") == cfg.allowed_hosts


def test_allowed_hosts_by_target_accepts_list_value() -> None:
    cfg = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_ADB_TARGETS": "redroid:5555,redroid-kagane:5555",
            "SOLVER_ALLOWED_HOSTS_BY_TARGET": (
                '{"redroid-kagane:5555":["kagane.to","Cdn.Kagane.To"]}'
            ),
        }
    )
    assert cfg.allowed_hosts_for("redroid-kagane:5555") == frozenset(
        {"kagane.to", "cdn.kagane.to"}
    )


def test_allowed_hosts_by_target_unknown_target_raises() -> None:
    # Fail loud: a per-target allowlist naming a target not in adb_targets.
    with pytest.raises(ConfigError):
        SidecarConfig.from_env(
            {
                "SOLVER_API_KEY": "k",
                "SOLVER_ADB_TARGETS": "redroid:5555",
                "SOLVER_ALLOWED_HOSTS_BY_TARGET": '{"redroid-nope:5555":"x.to"}',
            }
        )


def test_allowed_hosts_by_target_malformed_json_raises() -> None:
    with pytest.raises(ConfigError):
        SidecarConfig.from_env(
            {
                "SOLVER_API_KEY": "k",
                "SOLVER_ALLOWED_HOSTS_BY_TARGET": "not-json",
            }
        )
