"""Deterministic gate coverage for ``LiveSmokeProfile`` (D-48).

This test does NOT carry ``pytest.mark.live`` — it runs in the standard
``uv run nox -s gate`` invocation, closing the Wave-0 gap RESEARCH flagged
(no in-repo coverage of the dataclass shape that every Phase-5 smoke
parametrizes over).

The four behaviors asserted match the plan's `<behavior>` block:

1. ``LiveSmokeProfile`` is importable from ``tests.live.profiles._base``.
2. It is a frozen dataclass — post-construction field assignment raises.
3. Minimal-fields construction succeeds with the documented defaults.
4. ``expected_caps_antibot`` round-trips each documented Literal value.
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.live.profiles._base import LiveSmokeProfile


def _make_minimal(
    source_key: str = "demo",
    antibot: str = "none",
) -> LiveSmokeProfile:
    """Construct a profile with only the five required fields."""
    return LiveSmokeProfile(
        source_key=source_key,
        default_query="demo query",
        expected_caps_antibot=antibot,  # type: ignore[arg-type]
        needs_solver_warm=False,
        download_timeout_s=120.0,
    )


def test_profile_is_dataclass() -> None:
    """Behavior 1: importable + is a dataclass."""
    assert dataclasses.is_dataclass(LiveSmokeProfile)


def test_profile_is_frozen() -> None:
    """Behavior 2: frozen — assignment after construction raises.

    ``@dataclass(frozen=True)`` raises ``dataclasses.FrozenInstanceError``
    which subclasses ``AttributeError`` — accept either to keep the test
    robust to stdlib changes.
    """
    profile = _make_minimal()
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        profile.source_key = "other"  # type: ignore[misc]


def test_profile_defaults() -> None:
    """Behavior 3: minimal construction succeeds with documented defaults.

    Per PATTERNS.md ``tests/live/profiles/_base.py`` section:
    ``submit_idempotency=True``, ``max_releases_to_try=3``,
    ``min_releases_returned=1``, ``expected_release_pattern={}``,
    ``fixture_drift_paths=[]``, ``perf_budget_s=None``.
    """
    profile = _make_minimal()
    assert profile.source_key == "demo"
    assert profile.default_query == "demo query"
    assert profile.expected_caps_antibot == "none"
    assert profile.needs_solver_warm is False
    assert profile.download_timeout_s == 120.0
    assert profile.submit_idempotency is True
    assert profile.max_releases_to_try == 3
    assert profile.min_releases_returned == 1
    assert profile.expected_release_pattern == {}
    assert profile.fixture_drift_paths == []
    assert profile.perf_budget_s is None
    # CI gating (#197/#198) is opt-in — un-gated by default.
    assert profile.ci_skip_reason is None


def test_default_factories_isolated() -> None:
    """Default-factory fields must be fresh per instance (no shared mutable state).

    Catches a future refactor that swaps ``field(default_factory=...)`` for a
    bare default value — a classic Python footgun that would silently leak
    mutations across profiles.
    """
    a = _make_minimal(source_key="a")
    b = _make_minimal(source_key="b")
    assert a.expected_release_pattern is not b.expected_release_pattern
    assert a.fixture_drift_paths is not b.fixture_drift_paths


def test_antibot_literal_values_round_trip() -> None:
    """Behavior 4: ``expected_caps_antibot`` accepts the three documented literals.

    mypy enforces the Literal at static-analysis time; here we only assert
    construction succeeds and the field round-trips for each value.
    """
    for value in ("none", "cloudflare", "cloudflare+encrypted"):
        profile = _make_minimal(antibot=value)
        assert profile.expected_caps_antibot == value
