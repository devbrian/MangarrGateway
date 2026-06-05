"""Shared test helper: an in-memory capturing ring writer (260604-wm2).

Under the disk-backed ring model the ``Collector`` enqueues each event +
membership set to a ``MetricSnapshotStore`` (the on-disk system of record) on the
O(1) hot path, and the ring READ path is that store's indexed async queries.

Unit tests that just want to read back the events a collector emitted (seam /
p95 / middleware attribution) don't need the aiosqlite ceremony — they need the
same ``enqueue(ev, rings)`` contract plus synchronous read accessors. This
:class:`CapturingRingWriter` is that: a structural stand-in that records each
enqueue in memory and exposes ``iter_recent`` / ``recent_calls`` /
``latest_failures`` / ``latest_slow`` / ``calls_for_request`` so the tests assert
attribution without a DB. It is duck-typed against the collector's
``ring_writer`` (the collector only ever calls ``enqueue``), so it is passed via
``cast`` at the call sites.
"""

from __future__ import annotations

from dataclasses import asdict

from manga_gateway.metrics.event import MetricEvent


class CapturingRingWriter:
    """In-memory capture of ``Collector.enqueue(ev, rings)`` for unit tests."""

    def __init__(self) -> None:
        self._rows: list[tuple[MetricEvent, set[str]]] = []

    # The one method the collector hot path calls (O(1), no I/O).
    def enqueue(self, ev: MetricEvent, rings: set[str]) -> None:
        self._rows.append((ev, rings))

    # ── read accessors mirroring the disk store (newest-first by insertion) ────
    def iter_recent(self) -> list[MetricEvent]:
        """Every captured event (each event is in the ``recent`` ring)."""
        return [ev for ev, rings in self._rows if "recent" in rings]

    def recent_calls(self, limit: int) -> list[dict[str, object]]:
        return _as_dicts(self.iter_recent()[-limit:][::-1])

    def latest_failures(self, limit: int) -> list[dict[str, object]]:
        evs = [ev for ev, rings in self._rows if "failures" in rings]
        return _as_dicts(evs[-limit:][::-1])

    def latest_slow(self, limit: int) -> list[dict[str, object]]:
        evs = [ev for ev, rings in self._rows if "slow" in rings]
        return _as_dicts(evs[-limit:][::-1])

    def calls_for_request(self, request_id: int) -> list[dict[str, object]]:
        return _as_dicts(
            [ev for ev in self.iter_recent() if ev.request_id == request_id]
        )


def _as_dicts(events: list[MetricEvent]) -> list[dict[str, object]]:
    return [asdict(e) for e in events]
