"""Gate-deterministic tests proving the live-collection layer (D-47/D-49/D-50).

These tests intentionally run UNDER THE GATE (no ``pytest.mark.live``) so
the deterministic suite proves three invariants without ever touching the
network:

1. **Gate-isolation (RESEARCH A7).** Importing ``tests.live.conftest`` and
   resolving ``REGISTERED_KEYS`` inside the gate does NOT trigger the D-50
   collection error — the addopts ``-m 'not live'`` filter deselects the
   live modules' collection, but the conftest module itself stays loadable
   from any test that asks for it.
2. **Happy-path profile loads.** Both registered sources (MangaDex + Comix)
   resolve to a ``LiveSmokeProfile`` instance with the documented field
   values.
3. **D-50 negative path.** ``_load_profile("definitely_not_registered_xyz")``
   raises ``pytest.UsageError`` whose message names ``D-50``, proving the
   missing-profile failure mode reports the locked decision.

Also asserts ``parents[2]`` is the right climb for the Comix profile's
fixture path resolution (catches the parents[1] vs parents[2] regression
called out in the planner's read-first checklist).
"""

from __future__ import annotations

import pytest

from tests.live.conftest import REGISTERED_KEYS, _load_profile
from tests.live.profiles._base import LiveSmokeProfile


def test_registered_keys_resolves_in_gate() -> None:
    """RESEARCH A7: importing the live conftest in the gate must NOT raise."""
    assert isinstance(REGISTERED_KEYS, list)
    assert "mangadex" in REGISTERED_KEYS
    assert "comix" in REGISTERED_KEYS


def test_load_profile_mangadex() -> None:
    """MangaDex profile shape — antibot=none, no solver warm, English query."""
    profile = _load_profile("mangadex")
    assert isinstance(profile, LiveSmokeProfile)
    assert profile.source_key == "mangadex"
    assert profile.expected_caps_antibot == "none"
    assert profile.needs_solver_warm is False
    # default_query must be a non-empty string the planner pinned via the
    # human-verify checkpoint; the exact value is reviewed in HUMAN-UAT.md.
    assert isinstance(profile.default_query, str) and profile.default_query.strip()


def test_load_profile_comix() -> None:
    """Comix profile shape — antibot=cloudflare+encrypted, warm=True, default query."""
    profile = _load_profile("comix")
    assert isinstance(profile, LiveSmokeProfile)
    assert profile.source_key == "comix"
    assert profile.expected_caps_antibot == "cloudflare+encrypted"
    assert profile.needs_solver_warm is True
    assert profile.default_query == "Forgotten Field"
    # Migrated from tests/live/test_comix_e2e_live.py:54/57 (D-61 refactor map).
    assert profile.download_timeout_s == 480.0
    assert profile.max_releases_to_try == 3


def test_load_profile_missing_raises_usage_error_with_d50() -> None:
    """D-50 enforcement: an unregistered key MUST raise UsageError mentioning D-50."""
    with pytest.raises(pytest.UsageError) as excinfo:
        _load_profile("definitely_not_registered_xyz")
    assert "D-50" in str(excinfo.value)


def test_comix_fixture_paths_exist_on_disk() -> None:
    """Catches the parents[1] vs parents[2] climb regression (PATTERNS.md).

    The Comix profile lives one directory deeper than test_comix_live.py, so
    the relative climb to ``tests/fixtures/comix`` is parents[2], not
    parents[1]. If the climb is wrong the resolved Paths point inside the
    repo root and ``.exists()`` returns False — this test fails fast.
    """
    profile = _load_profile("comix")
    assert profile.fixture_drift_paths, (
        "Comix profile must declare fixture_drift_paths (D-44 anchors)"
    )
    for path in profile.fixture_drift_paths:
        assert path.exists(), (
            f"fixture path does not exist on disk: {path} "
            f"(likely parents[1] vs parents[2] climb regression)"
        )
