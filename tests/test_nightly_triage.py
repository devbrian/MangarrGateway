"""Unit tests for ``scripts/nightly_triage.py`` (Phase 5 D-57 / D-58 / Pitfall 7).

The triage script lives under ``scripts/`` which is ruff-excluded
(``pyproject.toml: [tool.ruff] extend-exclude = ["scripts"]``); we make it
importable for the deterministic gate by inserting that directory onto
``sys.path`` here. The ``gh`` subprocess is monkeypatched in every test — no
real GitHub network call ever happens.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Any

import pytest

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
    # Regression: bucket["tests"] feeds _format_body's "Failing tests in
    # this bucket" — passing testcases must NOT appear there
    # (CodeRabbit PR #33 review).
    assert per_source["mangadex"]["tests"] == [], (
        f"passing mangadex testcase leaked into tests[]: "
        f"{per_source['mangadex']['tests']!r}"
    )
    assert per_source["comix"]["tests"], "failing comix testcase missing from tests[]"


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


# ---------------------------------------------------------------------------
# Task 2 — subprocess-mocked gh CLI tests
# ---------------------------------------------------------------------------


class _SubprocessRecorder:
    """Capture every ``subprocess.run`` argv list across the test."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *args: Any,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        # Record a copy so later mutation in the script can't affect assertions.
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=0, stdout=b"", stderr=b"")


def test_upsert_creates_issue_when_none_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SubprocessRecorder()
    monkeypatch.setattr(
        nightly_triage.subprocess, "check_output", lambda *a, **kw: b"[]"
    )
    monkeypatch.setattr(nightly_triage.subprocess, "run", recorder)

    nightly_triage.upsert_sticky_issue("comix", "body text", "https://example/run/1")

    create_calls = [
        c for c in recorder.calls if len(c) >= 3 and c[:3] == ["gh", "issue", "create"]
    ]
    assert create_calls, f"expected gh issue create call, got: {recorder.calls!r}"
    argv = create_calls[0]
    assert "--title" in argv
    title_idx = argv.index("--title")
    assert argv[title_idx + 1] == "nightly: comix live smoke failing"
    # Both labels must appear via repeated --label flags.
    label_values = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--label"]
    assert "source:comix" in label_values
    assert "nightly-failure" in label_values


def test_upsert_comments_on_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _SubprocessRecorder()
    _existing_issue_json = (
        b'[{"number": 42, "title": "nightly: comix live smoke failing"}]'
    )
    monkeypatch.setattr(
        nightly_triage.subprocess,
        "check_output",
        lambda *a, **kw: _existing_issue_json,
    )
    monkeypatch.setattr(nightly_triage.subprocess, "run", recorder)

    nightly_triage.upsert_sticky_issue("comix", "body text", "https://example/run/1")

    comment_calls = [
        c for c in recorder.calls if len(c) >= 3 and c[:3] == ["gh", "issue", "comment"]
    ]
    assert comment_calls, f"expected gh issue comment call, got: {recorder.calls!r}"
    argv = comment_calls[0]
    assert "42" in argv
    body_idx = argv.index("--body")
    assert "https://example/run/1" in argv[body_idx + 1]
    # When commenting on an existing issue we must NOT also issue create.
    assert not any(c[:3] == ["gh", "issue", "create"] for c in recorder.calls), (
        f"unexpected gh issue create call: {recorder.calls!r}"
    )


def test_close_if_open_closes_each(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _SubprocessRecorder()
    monkeypatch.setattr(
        nightly_triage.subprocess,
        "check_output",
        lambda *a, **kw: b'[{"number": 11}, {"number": 22}]',
    )
    monkeypatch.setattr(nightly_triage.subprocess, "run", recorder)

    nightly_triage.close_if_open("comix", "https://example/run/1")

    close_calls = [
        c for c in recorder.calls if len(c) >= 3 and c[:3] == ["gh", "issue", "close"]
    ]
    closed_numbers = [c[3] for c in close_calls]
    assert "11" in closed_numbers
    assert "22" in closed_numbers


def test_label_create_is_idempotent_no_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pitfall 5: pre-existing label → non-zero gh exit → must NOT raise."""

    def _run_fail(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        # Simulate gh returning non-zero (label already exists).
        return subprocess.CompletedProcess(argv, returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(nightly_triage.subprocess, "run", _run_fail)
    # MUST NOT raise.
    nightly_triage._ensure_label("comix")


def test_ensure_label_creates_both_per_source_and_umbrella(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-nightly bootstrap: BOTH ``nightly-failure`` (umbrella) and
    ``source:{key}`` labels must be created or the subsequent
    ``gh issue create --label nightly-failure`` rejects the call.

    First dispatch on main 2026-05-31 surfaced this:
    ``gh issue create`` failed with
    ``could not add label: 'nightly-failure' not found``
    because the umbrella label had never been bootstrapped.
    """
    recorder = _SubprocessRecorder()
    monkeypatch.setattr(nightly_triage.subprocess, "run", recorder)
    nightly_triage._ensure_label("comix")

    label_create_calls = [
        c for c in recorder.calls if len(c) >= 3 and c[:3] == ["gh", "label", "create"]
    ]
    created_labels = [c[3] for c in label_create_calls]
    assert "nightly-failure" in created_labels, (
        f"umbrella label not bootstrapped — got: {created_labels!r}"
    )
    assert "source:comix" in created_labels, (
        f"per-source label not bootstrapped — got: {created_labels!r}"
    )


def test_main_all_pass_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    fixture = _FIXTURES / "all_pass.xml"
    target = tmp_path / "junit.xml"
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    upserts: list[str] = []
    closes: list[str] = []
    monkeypatch.setattr(
        nightly_triage,
        "upsert_sticky_issue",
        lambda src, body, run_url: upserts.append(src),
    )
    monkeypatch.setattr(
        nightly_triage,
        "close_if_open",
        lambda src, run_url: closes.append(src),
    )

    rc = nightly_triage.main(
        ["--junit", str(target), "--run-url", "https://example/run/1"]
    )
    assert rc == 0
    assert set(closes) == {"mangadex", "comix"}
    assert upserts == []


def test_main_all_fail_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    fixture = _FIXTURES / "all_fail.xml"
    target = tmp_path / "junit.xml"
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    upserts: list[str] = []
    closes: list[str] = []
    monkeypatch.setattr(
        nightly_triage,
        "upsert_sticky_issue",
        lambda src, body, run_url: upserts.append(src),
    )
    monkeypatch.setattr(
        nightly_triage,
        "close_if_open",
        lambda src, run_url: closes.append(src),
    )

    rc = nightly_triage.main(
        ["--junit", str(target), "--run-url", "https://example/run/1"]
    )
    assert rc == 1
    assert set(upserts) == {"mangadex", "comix"}
    assert closes == []


def test_main_mixed_exits_zero_but_upserts_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    fixture = _FIXTURES / "mixed_pass_fail.xml"
    target = tmp_path / "junit.xml"
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    upserts: list[str] = []
    closes: list[str] = []
    monkeypatch.setattr(
        nightly_triage,
        "upsert_sticky_issue",
        lambda src, body, run_url: upserts.append(src),
    )
    monkeypatch.setattr(
        nightly_triage,
        "close_if_open",
        lambda src, run_url: closes.append(src),
    )

    rc = nightly_triage.main(
        ["--junit", str(target), "--run-url", "https://example/run/1"]
    )
    assert rc == 0
    assert upserts == ["comix"]
    assert closes == ["mangadex"]
