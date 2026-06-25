"""Per-lane observability (OBS-01) — the additive ``lane`` label end-to-end.

Proves the additive ``lane: str | None = None`` field lands on the flat
:class:`MetricEvent` AND its lockstep doc model :class:`MetricEventOut`, defaults to
``None`` so every existing emit / ``asdict`` round-trip is byte-for-byte unchanged,
and round-trips through ``asdict() -> MetricEventOut`` carrying a non-secret lane
identifier (``"kagane"`` / ``"default"``). Plan 15-04 supplies ``lane=self._lane`` at
the AndroidSolver emit sites; THIS plan is the plumbing + the proof.

``lane`` is payload-only — it rides ``json.dumps(asdict(MetricEvent))`` into
``ring_events.payload`` with NO new ring_events column and NO metrics-DB migration,
exactly like the 260605 enrichment fields (T-15-09: it is a non-secret label, so the
T-10-04 url=None redaction posture on solve/eval is untouched).
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, cast

import pytest

from manga_gateway.metrics.collector import Collector, set_collector
from manga_gateway.metrics.context import current_source
from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.store import InMemoryStore
from manga_gateway.models.metrics import MetricEventOut

from ._metrics_helpers import CapturingRingWriter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from manga_gateway.metrics.snapshot import MetricSnapshotStore


def _minimal_event(lane: str | None = None) -> MetricEvent:
    """A minimal event — only the required dataclass fields, lane optional."""
    return MetricEvent(
        ts=1780675400.0,
        kind="solve",
        request_id=None,
        surface=None,
        endpoint=None,
        source_key="kagane",
        op="solve",
        method=None,
        url=None,
        status=None,
        outcome="ok",
        duration_ms=11000.0,
        attempt=1,
        lane=lane,
    )


# ───────────────────────── Task 1: the additive field ─────────────────────────


def test_minimal_event_lane_defaults_none() -> None:
    """Constructing a MetricEvent WITHOUT lane leaves ``.lane is None`` (OBS-01).

    The field is additive + optional so every existing emit / positional-free
    construction is byte-for-byte unchanged.
    """
    ev = MetricEvent(
        ts=1780675400.0,
        kind="solve",
        request_id=None,
        surface=None,
        endpoint=None,
        source_key="kagane",
        op="solve",
        method=None,
        url=None,
        status=None,
        outcome="ok",
        duration_ms=11000.0,
        attempt=1,
    )
    assert ev.lane is None


def test_lane_round_trips_through_metric_event_out() -> None:
    """A lane-stamped event round-trips asdict() -> MetricEventOut with lane intact.

    This is the OBS-01 proof that the label rides ``json.dumps(asdict(MetricEvent))``
    into the ring payload and surfaces through the read endpoints' doc model.
    """
    ev = _minimal_event(lane="kagane")
    wire = json.loads(json.dumps(dataclasses.asdict(ev)))
    assert wire["lane"] == "kagane"
    out = MetricEventOut.model_validate(wire)
    assert out.lane == "kagane"
    # byte-identity: the validated model dumps to exactly the asdict wire shape.
    assert out.model_dump(mode="json") == wire


def test_minimal_event_lane_null_round_trips() -> None:
    """lane=None serializes as JSON null (present, not dropped) and round-trips."""
    ev = _minimal_event(lane=None)
    wire = json.loads(json.dumps(dataclasses.asdict(ev)))
    assert wire["lane"] is None
    out = MetricEventOut.model_validate(wire)
    assert out.lane is None
    assert out.model_dump(mode="json") == wire


# ──────────────── Task 2: emit_solve / emit_eval lane threading ────────────────


@pytest.fixture
def collector() -> Iterator[tuple[Collector, CapturingRingWriter]]:
    """Install a real Collector + capturing ring writer for one test, then clear."""
    ring = CapturingRingWriter()
    c = Collector(
        InMemoryStore(slow_factor=3.0),
        ring_writer=cast("MetricSnapshotStore", ring),
    )
    set_collector(c)
    try:
        yield c, ring
    finally:
        set_collector(None)
        current_source.set(None)


def test_emit_solve_stamps_lane(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """emit_solve(lane=...) emits a kind="solve" event carrying that lane (OBS-01).

    Exercises the in-source_scope fallback branch (source_key passed, contextvar
    unbound), which is the AndroidSolver emit posture plan 15-04 will use.
    """
    c, store = collector
    c.emit_solve(source_key="kagane", outcome="ok", duration_ms=11000.0, lane="kagane")
    solves = [e for e in store.iter_recent() if e.kind == "solve"]
    assert len(solves) == 1
    assert solves[0].lane == "kagane"
    assert solves[0].source_key == "kagane"


def test_emit_eval_stamps_lane(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """emit_eval(lane=...) emits a kind="eval" event carrying that lane (OBS-01)."""
    c, store = collector
    c.emit_eval(source_key="comix", outcome="ok", duration_ms=8000.0, lane="default")
    evals = [e for e in store.iter_recent() if e.kind == "eval"]
    assert len(evals) == 1
    assert evals[0].lane == "default"
    assert evals[0].url is None  # T-15-09: lane is a label, redaction untouched


def test_emit_solve_lane_threads_through_bare_branch(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """lane threads through the BARE branch too (source already bound)."""
    from manga_gateway.metrics.context import source_scope

    c, store = collector
    with source_scope("kagane"):
        c.emit_solve(outcome="ok", duration_ms=11000.0, lane="kagane")
    solves = [e for e in store.iter_recent() if e.kind == "solve"]
    assert len(solves) == 1
    assert solves[0].lane == "kagane"


def test_emit_eval_lane_threads_through_bare_branch(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """lane threads through emit_eval's BARE branch too (source already bound)."""
    from manga_gateway.metrics.context import source_scope

    c, store = collector
    with source_scope("comix"):
        c.emit_eval(outcome="ok", duration_ms=8000.0, lane="default")
    evals = [e for e in store.iter_recent() if e.kind == "eval"]
    assert len(evals) == 1
    assert evals[0].lane == "default"


def test_emit_solve_without_lane_is_none(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """Omitting lane (existing call sites) emits ``.lane is None`` — byte-for-byte."""
    c, store = collector
    c.emit_solve(source_key="kagane", outcome="ok", duration_ms=11000.0)
    solves = [e for e in store.iter_recent() if e.kind == "solve"]
    assert len(solves) == 1
    assert solves[0].lane is None


def test_emit_eval_without_lane_is_none(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """Omitting lane on emit_eval emits ``.lane is None`` — byte-for-byte today."""
    c, store = collector
    c.emit_eval(source_key="comix", outcome="ok", duration_ms=8000.0)
    evals = [e for e in store.iter_recent() if e.kind == "eval"]
    assert len(evals) == 1
    assert evals[0].lane is None
