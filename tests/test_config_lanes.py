"""Tests for the lane Settings fields + fail-loud topology validator (LANE-01).

Plan 15-01 Task 2. These pin: the additive empty defaults (no behavior change for
existing deployers), the valid topology round-tripping into ``resolve_lane_plans``,
and every invalid topology failing LOUD at ``Settings`` construction (T-15-01 /
T-15-02 — a mis-mapped source can never route to a non-existent or blank-target
lane).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from manga_gateway.config import Settings
from manga_gateway.framework.lanes import resolve_lane_plans

_DUMMY_KEY = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_lane_fields_default_empty() -> None:
    """Additive defaults: nothing changes for a deployer with no lane config."""
    settings = Settings(api_key=_DUMMY_KEY)
    assert settings.android_lanes == {}
    assert settings.android_source_lane_map == {}


def test_valid_two_lane_topology_constructs() -> None:
    settings = Settings(
        api_key=_DUMMY_KEY,
        android_lanes={"default": "redroid:5555", "kagane": "redroid-kagane:5555"},
        android_source_lane_map={"kagane": "kagane"},
    )
    assert settings.android_lanes["kagane"] == "redroid-kagane:5555"
    assert settings.android_source_lane_map == {"kagane": "kagane"}


def test_valid_topology_round_trips_into_resolve_lane_plans() -> None:
    settings = Settings(
        api_key=_DUMMY_KEY,
        android_lanes={"default": "redroid:5555", "kagane": "redroid-kagane:5555"},
        android_source_lane_map={"kagane": "kagane"},
    )
    plans = resolve_lane_plans(
        android_lanes=settings.android_lanes,
        source_lane_map=settings.android_source_lane_map,
        android_keys=frozenset({"kagane", "mangadot", "comix"}),
    )
    assert set(plans) == {"default", "kagane"}
    assert plans["kagane"].source_keys == frozenset({"kagane"})
    assert plans["default"].source_keys == frozenset({"mangadot", "comix"})


def test_missing_default_lane_raises() -> None:
    with pytest.raises(ValidationError, match="default"):
        Settings(
            api_key=_DUMMY_KEY,
            android_lanes={"kagane": "redroid-kagane:5555"},
            android_source_lane_map={"kagane": "kagane"},
        )


def test_map_referencing_undeclared_lane_raises() -> None:
    with pytest.raises(ValidationError, match="kagane"):
        Settings(
            api_key=_DUMMY_KEY,
            android_lanes={"default": "redroid:5555"},
            android_source_lane_map={"kagane": "kagane"},
        )


def test_empty_adb_target_raises() -> None:
    with pytest.raises(ValidationError, match="default"):
        Settings(
            api_key=_DUMMY_KEY,
            android_lanes={"default": ""},
            android_source_lane_map={},
        )


def test_blank_adb_target_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(
            api_key=_DUMMY_KEY,
            android_lanes={"default": "   "},
            android_source_lane_map={},
        )


def test_map_without_any_lanes_raises() -> None:
    with pytest.raises(ValidationError, match="kagane"):
        Settings(
            api_key=_DUMMY_KEY,
            android_lanes={},
            android_source_lane_map={"kagane": "kagane"},
        )
