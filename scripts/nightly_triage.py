#!/usr/bin/env python3
"""Parse pytest JUnit XML, bucket testcases by source_key, upsert sticky issues.

Usage:
    uv run python scripts/nightly_triage.py --junit junit.xml --run-url <url>

This script is invoked by ``.github/workflows/nightly-live-smoke.yml`` after
``nox -s live`` runs. It reads the JUnit XML pytest emitted, buckets test
results by ``source_key`` extracted from each parametrize ID (with a classname
fallback for the non-parametrized Comix perf test — see Pitfall 7), and:

  * upserts a sticky GitHub issue per failing source (D-57)
  * auto-closes the sticky issue when a source's bucket is all-pass
  * returns exit code 1 iff EVERY source bucket failed in the same run (D-58
    "hold the line" — single-source failures route to issues, not the badge)

The script is stdlib + ``subprocess`` only. ``scripts/`` is ruff-excluded via
``pyproject.toml: [tool.ruff] extend-exclude = ["scripts"]``; the
unit tests under ``tests/test_nightly_triage.py`` import it via ``sys.path``
and exercise every branch with synthetic JUnit fixtures + a mocked
``subprocess`` (no real ``gh`` invocation in tests).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET  # safe: no external entity resolution by default
from collections import defaultdict
from pathlib import Path
from typing import Any

# Parametrize IDs at the tail of a pytest testcase name: ``test_foo[mangadex]``.
PARAM_RE = re.compile(r"\[([^\]]+)\]$")

# Defensive guard for source_key spliced into gh argv (label flags + issue
# titles). gh is invoked via argv (never shell) so this is belt-and-braces,
# but a strict character class rules out any surprises in URLs/labels.
_SOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Bound every ``gh`` subprocess call so a stalled CLI (network/API/runner
# hiccup) can't hang the entire nightly job (CodeRabbit PR #33 review).
# 30s is well above gh's typical sub-second latency, while keeping the
# nightly's total triage budget below 5 min even if every call timed out.
GH_TIMEOUT_SECONDS = 30


def parse_junit(path: Path) -> dict[str, dict[str, Any]]:
    """Parse a pytest JUnit XML report and bucket testcases by source_key.

    Returns a ``{source_key: {"pass": int, "fail": int, "tests": [name, ...]}}``
    mapping. Bucketing rule (matches Phase 5 RESEARCH Code Examples §4 +
    Pitfall 7):

    * If the testcase name ends in ``[paramid]``, the FULL ``paramid`` is the
      source_key. Hyphens are preserved (``[manga-plus]`` → ``manga-plus``):
      ``_SOURCE_KEY_RE`` permits ``-`` in source keys, so splitting on ``-``
      would silently misbucket any hyphenated source (CR-01, issue #28).
    * Else, if the testcase ``classname`` contains ``"comix"``, bucket to
      ``"comix"`` (Pitfall 7 — the non-parametrized perf test still feeds
      the comix sticky-issue signal per D-61 + D-62).
    * Else drop the testcase (e.g. unrelated stragglers).

    A testcase is a failure iff it has a ``<failure>`` OR ``<error>`` child
    element. ``<skipped>`` is NOT a failure.
    """
    tree = ET.parse(path)
    per_source: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "tests": []}
    )
    for tc in tree.iter("testcase"):
        name = tc.get("name", "")
        classname = tc.get("classname", "")
        m = PARAM_RE.search(name)
        if m is not None:
            source_key = m.group(1)
        elif "comix" in classname:
            # Pitfall 7: non-parametrized perf test → bucket via classname.
            source_key = "comix"
        else:
            continue
        failed = tc.find("failure") is not None or tc.find("error") is not None
        bucket = per_source[source_key]
        if failed:
            # ``bucket["tests"]`` is consumed by ``_format_body`` under the
            # "Failing tests in this bucket" heading — only failing testcase
            # names belong here (CodeRabbit PR #33 review).
            bucket["tests"].append(name)
            bucket["fail"] += 1
        else:
            bucket["pass"] += 1
    return dict(per_source)


def compute_exit(per_source: dict[str, dict[str, Any]]) -> int:
    """D-58 all-sources-fail exit code.

    Returns 1 iff ``per_source`` is empty (parsing produced nothing → something
    broke) OR every bucket has at least one failure. Returns 0 otherwise — a
    single-source failure still routes to a sticky issue but does NOT flip the
    nightly badge red ("hold the line").
    """
    if not per_source:
        return 1
    return 1 if all(bucket["fail"] > 0 for bucket in per_source.values()) else 0


def _validate_source_key(source_key: str) -> str:
    """Reject source keys that would be unsafe to splice into label flags."""
    if not _SOURCE_KEY_RE.match(source_key):
        raise ValueError(
            f"refusing to invoke gh with unsafe source_key: {source_key!r}"
        )
    return source_key


def _ensure_label(source_key: str) -> None:
    """Idempotently create both labels used by the sticky-issue flow (Pitfall 5).

    Creates the per-source ``source:{key}`` label AND the cross-cutting
    ``nightly-failure`` umbrella label. Both must exist before
    ``gh issue create --label`` will accept them; the first failing nightly
    HAS to bootstrap both, otherwise ``upsert_sticky_issue`` fails with
    ``could not add label: 'nightly-failure' not found`` (first dispatch on
    main 2026-05-31 surfaced this).

    ``check=False`` so a pre-existing label silently no-ops — ``gh label
    create`` returns non-zero when the label already exists, which is the
    overwhelmingly common case after the first nightly run.
    """
    _validate_source_key(source_key)
    subprocess.run(
        [
            "gh",
            "label",
            "create",
            "nightly-failure",
            "--color",
            "B60205",
            "--description",
            "Umbrella label for all nightly-live-smoke sticky issues",
        ],
        check=False,
        timeout=GH_TIMEOUT_SECONDS,
    )
    subprocess.run(
        [
            "gh",
            "label",
            "create",
            f"source:{source_key}",
            "--color",
            "FF0000",
            "--description",
            "Per-source nightly bucket",
        ],
        check=False,
        timeout=GH_TIMEOUT_SECONDS,
    )


def upsert_sticky_issue(source_key: str, body: str, run_url: str) -> None:
    """Create-or-comment the sticky failure issue for ``source_key`` (D-57).

    Looks up an open issue via label-based filter (``nightly-failure`` +
    ``source:{key}``) — NOT title search (RESEARCH anti-pattern). If one
    exists, comments on it; otherwise creates a fresh issue with both labels.

    Labels are passed via repeated ``--label`` flags (cli.github.com manual).
    """
    _validate_source_key(source_key)
    _ensure_label(source_key)
    out = subprocess.check_output(
        [
            "gh",
            "issue",
            "list",
            "--label",
            "nightly-failure",
            "--label",
            f"source:{source_key}",
            "--state",
            "open",
            "--json",
            "number,title",
        ],
        timeout=GH_TIMEOUT_SECONDS,
    )
    existing = json.loads(out or b"[]")
    if existing:
        number = existing[0]["number"]
        subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "--body",
                f"{run_url}\n\n{body}",
            ],
            check=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    else:
        subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                f"nightly: {source_key} live smoke failing",
                "--body",
                f"{run_url}\n\n{body}",
                "--label",
                f"source:{source_key}",
                "--label",
                "nightly-failure",
            ],
            check=True,
            timeout=GH_TIMEOUT_SECONDS,
        )


def close_if_open(source_key: str, run_url: str) -> None:
    """Auto-close any open sticky issue for ``source_key`` (next-night pass)."""
    _validate_source_key(source_key)
    out = subprocess.check_output(
        [
            "gh",
            "issue",
            "list",
            "--label",
            "nightly-failure",
            "--label",
            f"source:{source_key}",
            "--state",
            "open",
            "--json",
            "number",
        ],
        timeout=GH_TIMEOUT_SECONDS,
    )
    for issue in json.loads(out or b"[]"):
        subprocess.run(
            [
                "gh",
                "issue",
                "close",
                str(issue["number"]),
                "--comment",
                f"Live smoke passing again — auto-closed.\n\n{run_url}",
            ],
            check=True,
            timeout=GH_TIMEOUT_SECONDS,
        )


def _format_body(source_bucket: dict[str, Any], junit_path: Path) -> str:
    """Build a Markdown-ish sticky-issue body — bounded-noise (D-57).

    Includes the recorded testcase names for the bucket plus the tail (~50
    lines) of an adjacent ``pytest-live.log`` when present.
    """
    failing = list(source_bucket.get("tests", []))
    lines: list[str] = ["**Failing tests in this bucket:**"]
    if failing:
        lines.extend(f"- `{name}`" for name in failing)
    else:
        lines.append("- (no test names recorded)")
    lines.append("")
    log_path = junit_path.parent / "pytest-live.log"
    if log_path.exists():
        try:
            log_tail = log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-50:]
            lines.append("**Log tail (last 50 lines):**")
            lines.append("```")
            lines.extend(log_tail)
            lines.append("```")
        except OSError:
            lines.append("(log read failed)")
    else:
        lines.append("(no pytest-live.log adjacent to junit.xml)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point — parse JUnit, upsert/close per source, return D-58 exit code.

    Does NOT call ``sys.exit`` itself so unit tests can call ``main`` directly
    and inspect the integer return code.
    """
    parser = argparse.ArgumentParser(
        description="Triage pytest JUnit XML and upsert sticky GitHub issues."
    )
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--run-url", required=True, type=str)
    ns = parser.parse_args(argv)

    per_source = parse_junit(ns.junit)
    for source_key, bucket in per_source.items():
        if bucket["fail"] > 0:
            body = _format_body(bucket, ns.junit)
            upsert_sticky_issue(source_key, body, ns.run_url)
        else:
            close_if_open(source_key, ns.run_url)
    return compute_exit(per_source)


if __name__ == "__main__":
    sys.exit(main())
