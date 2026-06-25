"""Offline tests for the pure lane-partition helper (LANE-01, Plan 15-01 Task 1).

These pin the single-lane collapse regression guard the whole phase rests on:
with NO lane config, ``resolve_lane_plans`` MUST return exactly ONE ``default``
lane carrying every android source with ``adb_target=None`` (byte-for-byte today).
They also pin the multi-lane partition (disjoint source_keys), the unmapped-key
fallback to ``default``, and the idle-dedicated-device empty-lane case.
"""

from __future__ import annotations

import dataclasses

import pytest

from manga_gateway.framework.lanes import DEFAULT_LANE, LanePlan, resolve_lane_plans


def test_default_lane_const() -> None:
    assert DEFAULT_LANE == "default"


def test_single_lane_collapse_no_config() -> None:
    """The critical regression guard: empty config -> ONE default lane, no target."""
    keys = frozenset({"kagane", "mangadot", "comix", "mangaball"})

    plans = resolve_lane_plans(android_lanes={}, source_lane_map={}, android_keys=keys)

    assert list(plans) == [DEFAULT_LANE]
    plan = plans[DEFAULT_LANE]
    assert plan.lane_id == DEFAULT_LANE
    assert plan.adb_target is None
    assert plan.source_keys == keys


def test_single_lane_collapse_empty_keys() -> None:
    plans = resolve_lane_plans(
        android_lanes={}, source_lane_map={}, android_keys=frozenset()
    )
    assert list(plans) == [DEFAULT_LANE]
    assert plans[DEFAULT_LANE].adb_target is None
    assert plans[DEFAULT_LANE].source_keys == frozenset()


def test_two_lane_partition_is_disjoint() -> None:
    keys = frozenset({"kagane", "mangadot", "comix"})

    plans = resolve_lane_plans(
        android_lanes={"default": "redroid:5555", "kagane": "redroid-kagane:5555"},
        source_lane_map={"kagane": "kagane"},
        android_keys=keys,
    )

    assert set(plans) == {"default", "kagane"}
    assert plans["default"].source_keys == frozenset({"mangadot", "comix"})
    assert plans["kagane"].source_keys == frozenset({"kagane"})
    assert plans["default"].adb_target == "redroid:5555"
    assert plans["kagane"].adb_target == "redroid-kagane:5555"

    # No source appears in more than one lane.
    seen: list[str] = []
    for plan in plans.values():
        seen.extend(plan.source_keys)
    assert sorted(seen) == sorted(keys)
    assert len(seen) == len(set(seen))


def test_unmapped_key_falls_to_default() -> None:
    plans = resolve_lane_plans(
        android_lanes={"default": "redroid:5555", "kagane": "redroid-kagane:5555"},
        source_lane_map={},  # nothing mapped -> everything to default
        android_keys=frozenset({"kagane", "mangadot"}),
    )
    assert plans["default"].source_keys == frozenset({"kagane", "mangadot"})
    assert plans["kagane"].source_keys == frozenset()


def test_declared_lane_with_no_sources_is_kept_empty() -> None:
    """A dedicated lane with nothing mapped to it is still constructed (idle device)."""
    plans = resolve_lane_plans(
        android_lanes={"default": "redroid:5555", "spare": "redroid-spare:5555"},
        source_lane_map={},
        android_keys=frozenset({"kagane"}),
    )
    assert "spare" in plans
    assert plans["spare"].source_keys == frozenset()
    assert plans["spare"].adb_target == "redroid-spare:5555"
    assert plans["default"].source_keys == frozenset({"kagane"})


def test_lane_plan_is_frozen() -> None:
    plan = LanePlan(lane_id="default", adb_target=None, source_keys=frozenset())
    assert dataclasses.is_dataclass(plan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.lane_id = "other"  # type: ignore[misc]
