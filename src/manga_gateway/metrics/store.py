"""In-process metric store: HDR-backed rollups + recent/failures/slow rings.

Two structures, both updated in O(1) per event at *ingest* so *reads* stay cheap
(the poll-friendliness constraint — same world as ``GET /downloads``):

1. **ROLLUPS** keyed by ``(surface, source_key, endpoint, op)`` ONLY: count,
   error_count, sum_ms (→ avg), min/max, and a per-rollup ``HdrHistogram`` for a
   constant-relative-error p95 (OBS-08 — replaces the spike's coarse fixed-bucket
   placeholder). NEVER keyed on ``url`` or ``request_id`` (cardinality explosion).
2. Three bounded ``deque(maxlen=N)`` **RINGS** carrying full event detail:
   * ``recent`` — every event (answers "recent calls" + ``/requests/{id}``);
   * ``failures`` — admits ``outcome != "ok"``;
   * ``slow`` — admits ``duration_ms > max(slow_factor*avg, p95)`` evaluated AFTER
     the rollup is updated (a PER-SOURCE-relative baseline, NEVER a global ms
     constant — comix ~220ms vs mangadex ~35ms).

The dashboard reads ready-made JSON from here; it never parses a log line.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field

from hdrh.histogram import HdrHistogram

from .event import MetricEvent

# HdrHistogram(lowest, highest_trackable_ms, significant_figures): 1ms..600_000ms
# (10 min) at 2 sig figs → ~1% relative error, a few KB/series (RESEARCH §Q1).
_HDR_LOW = 1
_HDR_HIGH = 600_000
_HDR_SIGFIG = 2


def new_histogram() -> HdrHistogram:
    """One canonical histogram config, shared by the store and the snapshot
    rehydrate path so p95 round-trips exactly."""
    return HdrHistogram(_HDR_LOW, _HDR_HIGH, _HDR_SIGFIG)


def histogram_pairs(hist: HdrHistogram) -> list[tuple[int, int]]:
    """Extract the populated ``(value, count)`` pairs of a histogram.

    This is the CROSS-PLATFORM serialization/merge primitive (RESEARCH §Q1 +
    08-01 known-issue): ``HdrHistogram.encode()`` AND ``HdrHistogram.add()`` both
    raise ``OverflowError`` on the Windows dev host (LLP64 32-bit ``c_long``), so
    the snapshot store and the per-source-per-endpoint merge BOTH go through this
    iterator-pair path — replaying ``record_value(value, count)`` into a fresh
    histogram round-trips p50/p95/total_count exactly on Windows and Linux.
    """
    return [
        (item.value_iterated_to, item.count_added_in_this_iter_step)
        for item in hist.get_recorded_iterator()
        if item.count_added_in_this_iter_step > 0
    ]


def histogram_from_pairs(pairs: list[tuple[int, int]]) -> HdrHistogram:
    """Rebuild a histogram by replaying persisted ``(value, count)`` pairs."""
    hist = new_histogram()
    for value, count in pairs:
        hist.record_value(value, count)
    return hist


RollupKey = tuple[str | None, str | None, str | None, str | None]


@dataclass
class Rollup:
    """O(1)-updated aggregate for one ``(surface, source, endpoint, op)`` series."""

    count: int = 0
    error_count: int = 0
    sum_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    hist: HdrHistogram = field(default_factory=new_histogram)

    def observe(self, ev: MetricEvent) -> None:
        self.count += 1
        if ev.outcome != "ok":
            self.error_count += 1
        d = ev.duration_ms
        self.sum_ms += d
        self.min_ms = min(self.min_ms, d)
        self.max_ms = max(self.max_ms, d)
        # Pitfall 3: record_value requires an int → round. 1ms quantization is far
        # below the ~1% bucket error at 2 sig figs, so no p95 precision loss.
        self.hist.record_value(max(1, round(d)))

    @property
    def avg_ms(self) -> float:
        return self.sum_ms / self.count if self.count else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.count if self.count else 0.0

    def p95_ms(self) -> float:
        if self.count == 0:
            return 0.0
        return float(self.hist.get_value_at_percentile(95.0))

    def to_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "errors": self.error_count,
            "error_rate": round(self.error_rate, 4),
            "avg_ms": round(self.avg_ms, 2),
            "p95_ms": round(self.p95_ms(), 2),
            "min_ms": round(self.min_ms, 2) if self.count else 0.0,
            "max_ms": round(self.max_ms, 2),
        }


class InMemoryStore:
    """Live source of truth (R1 single-process). Ring sizes + slow factor come
    from ``Settings`` (RESEARCH §Q4); reads are O(rollup count), not O(events)."""

    def __init__(
        self,
        *,
        recent_max: int,
        failures_max: int,
        slow_max: int,
        slow_factor: float,
    ) -> None:
        self._rollups: dict[RollupKey, Rollup] = {}
        self._recent: deque[MetricEvent] = deque(maxlen=recent_max)
        self._failures: deque[MetricEvent] = deque(maxlen=failures_max)
        self._slow: deque[MetricEvent] = deque(maxlen=slow_max)
        self._slow_factor = slow_factor

    # ── ingest (hot path, O(1)) ──────────────────────────────────────────────
    def ingest(self, ev: MetricEvent) -> None:
        key: RollupKey = (ev.surface, ev.source_key, ev.endpoint, ev.op)
        rollup = self._rollups.setdefault(key, Rollup())
        rollup.observe(ev)
        self._recent.append(ev)
        if ev.outcome != "ok":
            self._failures.append(ev)
        # Slow admission is PER-BASELINE, evaluated AFTER the rollup update so the
        # threshold reflects this series' own avg/p95 — never a global ms constant.
        threshold = max(self._slow_factor * rollup.avg_ms, rollup.p95_ms())
        if ev.duration_ms > threshold:
            self._slow.append(ev)

    # ── read side (all O(rollup count) or O(ring)) ───────────────────────────
    def summary(self) -> dict[str, object]:
        total_calls = sum(r.count for r in self._rollups.values())
        total_errors = sum(r.error_count for r in self._rollups.values())
        return {
            "total_calls": total_calls,
            "total_errors": total_errors,
            "error_rate": round(total_errors / total_calls, 4) if total_calls else 0.0,
            "tracked_series": len(self._rollups),
        }

    def rollups(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for key, r in sorted(self._rollups.items(), key=_sort_key):
            surface, source, endpoint, op = key
            row: dict[str, object] = {
                "surface": surface,
                "source": source,
                "endpoint": endpoint,
                "op": op,
            }
            row.update(r.to_dict())
            out.append(row)
        return out

    def per_source_per_endpoint(self) -> list[dict[str, object]]:
        """Avg per source per endpoint — merges all ops under a (source, endpoint)."""
        merged: dict[tuple[str | None, str | None], Rollup] = {}
        for (_surface, source, endpoint, _op), r in self._rollups.items():
            agg = merged.setdefault((source, endpoint), Rollup())
            agg.count += r.count
            agg.error_count += r.error_count
            agg.sum_ms += r.sum_ms
            agg.min_ms = min(agg.min_ms, r.min_ms)
            agg.max_ms = max(agg.max_ms, r.max_ms)
            # Merge via the cross-platform pair replay — HdrHistogram.add() raises
            # OverflowError on the Windows dev host (LLP64 c_long).
            for value, count in histogram_pairs(r.hist):
                agg.hist.record_value(value, count)
        out: list[dict[str, object]] = []
        for (source, endpoint), r in sorted(
            merged.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
        ):
            row: dict[str, object] = {"source": source, "endpoint": endpoint}
            row.update(r.to_dict())
            out.append(row)
        return out

    def latest_failures(self, limit: int) -> list[dict[str, object]]:
        return _as_dicts(list(self._failures)[-limit:][::-1])

    def latest_slow(self, limit: int) -> list[dict[str, object]]:
        return _as_dicts(list(self._slow)[-limit:][::-1])

    def recent_calls(self, limit: int) -> list[dict[str, object]]:
        return _as_dicts(list(self._recent)[-limit:][::-1])

    def calls_for_request(self, request_id: int) -> list[dict[str, object]]:
        return _as_dicts([e for e in self._recent if e.request_id == request_id])

    # ── snapshot accessors (used by snapshot.py) ─────────────────────────────
    def iter_rollups(self) -> list[tuple[RollupKey, Rollup]]:
        return list(self._rollups.items())

    def iter_recent(self) -> list[MetricEvent]:
        return list(self._recent)

    def iter_failures(self) -> list[MetricEvent]:
        return list(self._failures)

    def iter_slow(self) -> list[MetricEvent]:
        return list(self._slow)

    def restore_rollup(self, key: RollupKey, rollup: Rollup) -> None:
        self._rollups[key] = rollup

    def restore_recent(self, ev: MetricEvent) -> None:
        self._recent.append(ev)

    def restore_failure(self, ev: MetricEvent) -> None:
        self._failures.append(ev)

    def restore_slow(self, ev: MetricEvent) -> None:
        self._slow.append(ev)


def _sort_key(item: tuple[RollupKey, Rollup]) -> tuple[str, str, str, str]:
    surface, source, endpoint, op = item[0]
    return (surface or "", source or "", endpoint or "", op or "")


def _as_dicts(events: list[MetricEvent]) -> list[dict[str, object]]:
    return [asdict(e) for e in events]
