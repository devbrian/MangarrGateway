"""``LiveSmokeProfile`` frozen dataclass (D-48).

A per-source, declarative bundle of constants the parametrized live-smoke
tests need at collection + run time. One ``LIVE_SMOKE: LiveSmokeProfile``
instance lives per ``tests/live/profiles/{source_key}.py`` (D-49), keeping
test-only data out of production ``src/manga_gateway/sources/*.py``.

The dataclass is ``frozen=True`` so a parametrized test cannot mutate the
shared profile mid-run (one source can't poison another's expectations).

Field set is locked by Phase 5 RESEARCH (Pattern 2) + PATTERNS.md:
``tests/live/profiles/_base.py`` section.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from manga_gateway.models.caps import AntibotLevel


@dataclass(frozen=True)
class LiveSmokeProfile:
    """Declarative per-source live-smoke configuration (D-48).

    Required fields capture the four traits that vary per source: the source
    key + a stable query, the antibot level the live ``/caps`` response is
    expected to advertise, whether the smoke harness must ``await
    solver.warm()`` before issuing requests, and the per-test timeout.

    Defaults cover the common case so future sources only override what differs.
    """

    source_key: str
    default_query: str
    expected_caps_antibot: AntibotLevel
    needs_solver_warm: bool
    download_timeout_s: float
    submit_idempotency: bool = True
    max_releases_to_try: int = 3
    min_releases_returned: int = 1
    expected_release_pattern: dict[str, str] = field(default_factory=dict)
    fixture_drift_paths: list[Path] = field(default_factory=list)
    perf_budget_s: float | None = None
