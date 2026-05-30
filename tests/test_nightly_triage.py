"""Unit tests for ``scripts/nightly_triage.py`` (Phase 5 D-57 / D-58 / Pitfall 7).

The triage script lives under ``scripts/`` which is ruff-excluded
(``pyproject.toml: [tool.ruff] extend-exclude = ["scripts"]``); we make it
importable for the deterministic gate by inserting that directory onto
``sys.path`` here. The ``gh`` subprocess is monkeypatched in every test — no
real GitHub network call ever happens.
"""

from __future__ import annotations

import pathlib
import sys

# Make ``scripts/nightly_triage.py`` importable without packaging it.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import nightly_triage  # noqa: E402  (must come after sys.path mutation)

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "junit_samples"


# ---------------------------------------------------------------------------
# Task 1 — parse_junit + compute_exit
# ---------------------------------------------------------------------------


def test_parse_junit_all_pass() -> None:
    per_source = nightly_triage.parse_junit(_FIXTURES / "all_pass.xml")
    assert set(per_source.keys()) == {"mangadex", "comix"}
    assert per_source["mangadex"]["fail"] == 0
    assert per_source["mangadex"]["pass"] == 1
    assert per_source["comix"]["fail"] == 0
    assert per_source["comix"]["pass"] == 1


def test_parse_junit_all_fail() -> None:
    per_source = nightly_triage.parse_junit(_FIXTURES / "all_fail.xml")
    assert per_source["mangadex"]["fail"] > 0
    assert per_source["comix"]["fail"] > 0


def test_parse_junit_mixed() -> None:
    per_source = nightly_triage.parse_junit(_FIXTURES / "mixed_pass_fail.xml")
    assert per_source["mangadex"]["fail"] == 0
    assert per_source["mangadex"]["pass"] == 1
    assert per_source["comix"]["fail"] > 0
    assert per_source["comix"]["pass"] == 0


def test_parse_junit_perf_classname_bucketing() -> None:
    """Pitfall 7: non-parametrized perf test must bucket to ``comix`` via classname."""
    per_source = nightly_triage.parse_junit(_FIXTURES / "perf_only_fail.xml")
    assert "comix" in per_source, (
        f"Pitfall 7: expected 'comix' bucket from classname-only testcase, "
        f"got {per_source!r}"
    )
    assert per_source["comix"]["fail"] == 1
    # The single failing test must be recorded by name.
    assert any(
        "test_comix_warm_download_under_perf_budget" in name
        for name in per_source["comix"]["tests"]
    )


def test_compute_exit_all_pass() -> None:
    per_source = {
        "mangadex": {"pass": 1, "fail": 0, "tests": []},
        "comix": {"pass": 1, "fail": 0, "tests": []},
    }
    assert nightly_triage.compute_exit(per_source) == 0


def test_compute_exit_all_fail() -> None:
    per_source = {
        "mangadex": {"pass": 0, "fail": 1, "tests": []},
        "comix": {"pass": 0, "fail": 1, "tests": []},
    }
    assert nightly_triage.compute_exit(per_source) == 1


def test_compute_exit_mixed_returns_zero() -> None:
    """D-58: one source fails, one passes → exit 0 (don't flip the badge)."""
    per_source = {
        "mangadex": {"pass": 1, "fail": 0, "tests": []},
        "comix": {"pass": 0, "fail": 1, "tests": []},
    }
    assert nightly_triage.compute_exit(per_source) == 0


def test_compute_exit_empty_returns_one() -> None:
    """Empty buckets = parsing produced nothing = something is broken."""
    assert nightly_triage.compute_exit({}) == 1
