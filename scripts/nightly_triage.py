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

import re
import subprocess  # noqa: F401  (used by upsert/close logic added in Task 2)
import xml.etree.ElementTree as ET  # safe: no external entity resolution by default
from collections import defaultdict
from pathlib import Path
from typing import Any

# Parametrize IDs at the tail of a pytest testcase name: ``test_foo[mangadex]``.
PARAM_RE = re.compile(r"\[([^\]]+)\]$")


def parse_junit(path: Path) -> dict[str, dict[str, Any]]:
    """Parse a pytest JUnit XML report and bucket testcases by source_key.

    Returns a ``{source_key: {"pass": int, "fail": int, "tests": [name, ...]}}``
    mapping. Bucketing rule (matches Phase 5 RESEARCH Code Examples §4 +
    Pitfall 7):

    * If the testcase name ends in ``[paramid]``, take the first dash-separated
      segment of ``paramid`` as the source_key (so ``[mangadex-extra]`` still
      buckets to ``mangadex`` — defensive against double-params).
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
            source_key = m.group(1).split("-")[0]
        elif "comix" in classname:
            # Pitfall 7: non-parametrized perf test → bucket via classname.
            source_key = "comix"
        else:
            continue
        failed = tc.find("failure") is not None or tc.find("error") is not None
        bucket = per_source[source_key]
        bucket["tests"].append(name)
        if failed:
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


# upsert_sticky_issue, close_if_open, _ensure_label, _format_body, main():
# implemented in Task 2.
