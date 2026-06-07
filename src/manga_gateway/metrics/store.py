"""In-process metric store: HDR-backed rollups + ring-membership classification.

Rollups stay IN MEMORY (O(1) update at ingest, cheap reads — the
poll-friendliness constraint, same world as ``GET /downloads``); the recent /
failures / slow RING EVENTS moved to the on-disk ``ring_events`` table
(``snapshot.py``, system of record, 260604-wm2). This store therefore no longer
holds ring deques — it holds:

1. **ROLLUPS** keyed by ``(surface, source_key, endpoint, op)`` ONLY: count,
   error_count, sum_ms (→ avg), min/max, and a per-rollup ``HdrHistogram`` for a
   constant-relative-error p95 (OBS-08). NEVER keyed on ``url`` or ``request_id``
   (cardinality explosion).
2. **Ring classification** — :meth:`classify` updates the rollup (so the slow
   threshold reflects this series' own live baseline) and returns the SET of
   rings this event belongs to: always ``"recent"``; ``"failures"`` for a genuine
   failure (:func:`is_failure` — ``error``/``timeout`` on any event, plus a 4xx
   ``client_error`` only on the umbrella ``request`` event; NOT the cache
   hit/miss/refetch state or the re-solved per-source CF-403, which a blanket
   ``outcome != "ok"`` over-counted); ``"slow"`` when ``duration_ms >
   max(slow_factor*avg, p95)`` (a PER-SOURCE-relative baseline, NEVER a global ms
   constant — comix ~220ms vs mangadex ~35ms). The collector enqueues one on-disk
   row per (event, ring) membership; reads filter on the persisted ``ring`` tag.

The dashboard reads ready-made JSON: rollups from here, ring events from disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

# The three ring names. ``recent`` admits every event; ``failures`` admits a
# genuine failure (see :func:`is_failure`); ``slow`` admits a per-baseline
# outlier (see classify).
RING_RECENT = "recent"
RING_FAILURES = "failures"
RING_SLOW = "slow"

# Outcomes that are a genuine failure on ANY event. Everything NOT listed here is
# treated as a non-failure: "ok", the enumeration-cache STATE labels
# "hit"/"miss"/"refetch" (kind="cache"), and the reserved transient labels
# "retry"/"cf_resolve". ``client_error`` is CONDITIONAL — see :func:`is_failure`.
_FAILURE_OUTCOMES = frozenset({"error", "timeout"})


def is_failure(ev: MetricEvent) -> bool:
    """True iff ``ev`` counts toward ``error_count`` and the ``failures`` ring.

    Replaces the historic blanket ``outcome != "ok"`` test, which over-counted
    every non-ok STATE label as an error (debug ``search-error-rate-inflated``):
    the enumeration cache's ``hit``/``miss``/``refetch`` (``kind="cache"``,
    ``enum_cache.py``) and Comix's expected pre-solve Cloudflare 403 — emitted as
    ``client_error`` at the per-source http seam (``framework/context.py``) BEFORE
    ``_send_with_clearance`` re-solves + retries it to ``ok`` — both inherit
    ``endpoint="POST /search"`` + a ``source_key`` and so inflated the per-source
    ``POST /search`` ``error_rate`` (and contaminated the ``failures`` ring, which
    shared the test).

    A genuine failure is ``error`` (5xx / transport exception / unrecovered solve,
    browser, package, or job failure) or ``timeout`` for ANY event. A 4xx
    (``client_error``) counts ONLY on the umbrella ``request`` event — the
    gateway's OWN inbound API, ``kind="request"``, ``source_key=None``, emitted by
    the request middleware. That preserves WR-04: a flood of 401/404/422 on the
    gateway's own API is the prime observability signal and must stay visible in
    ``error_rate`` + the ``failures`` ring. A per-SOURCE seam 4xx (``kind="http"``
    and friends) is NOT counted — it is the expected, re-solved CF-403 noise.
    """
    if ev.outcome in _FAILURE_OUTCOMES:
        return True
    # WR-04: keep counting the gateway's own inbound-API 4xx; drop the per-source
    # seam 4xx (the re-solved Cloudflare challenge 403).
    return ev.outcome == "client_error" and ev.kind == "request"


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
        if is_failure(ev):
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
    """Live rollup source of truth (R1 single-process). Reads are O(rollup count),
    not O(events); the slow factor comes from ``Settings`` (RESEARCH §Q4).

    Ring events are NOT held here anymore (260604-wm2) — they live in the on-disk
    ring_events table. :meth:`classify` returns the ring-membership set the
    collector persists; the ring READ path is the disk store.
    """

    def __init__(self, *, slow_factor: float) -> None:
        self._rollups: dict[RollupKey, Rollup] = {}
        self._slow_factor = slow_factor

    # ── ingest / classify (hot path, O(1)) ───────────────────────────────────
    def classify(self, ev: MetricEvent) -> set[str]:
        """Update the rollup and return this event's ring-membership set.

        Always includes ``"recent"``; adds ``"failures"`` for a genuine failure
        (:func:`is_failure` — NOT a blanket ``outcome != "ok"``, so cache
        hit/miss/refetch and the re-solved per-source CF-403 ``client_error`` are
        excluded); adds ``"slow"`` for a per-baseline outlier (``duration_ms >
        max(slow_factor*avg, p95)``), evaluated AFTER the rollup update so the
        threshold reflects this series' own live avg/p95 — never a global ms
        constant. The collector enqueues one on-disk row per returned ring.
        """
        key: RollupKey = (ev.surface, ev.source_key, ev.endpoint, ev.op)
        rollup = self._rollups.setdefault(key, Rollup())
        rollup.observe(ev)
        rings = {RING_RECENT}
        if is_failure(ev):
            rings.add(RING_FAILURES)
        threshold = max(self._slow_factor * rollup.avg_ms, rollup.p95_ms())
        if ev.duration_ms > threshold:
            rings.add(RING_SLOW)
        return rings

    def ingest(self, ev: MetricEvent) -> set[str]:
        """Rollup-only ingest (still callable for pure rollup tests).

        Delegates to :meth:`classify` — same rollup update — and returns the
        membership set so a caller that wants it (the collector path) gets it.
        """
        return self.classify(ev)

    # ── read side (rollups; all O(rollup count)) ─────────────────────────────
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

    # ── snapshot accessors (used by snapshot.py — ROLLUPS ONLY) ───────────────
    def iter_rollups(self) -> list[tuple[RollupKey, Rollup]]:
        return list(self._rollups.items())

    def restore_rollup(self, key: RollupKey, rollup: Rollup) -> None:
        self._rollups[key] = rollup


def _sort_key(item: tuple[RollupKey, Rollup]) -> tuple[str, str, str, str]:
    surface, source, endpoint, op = item[0]
    return (surface or "", source or "", endpoint or "", op or "")
