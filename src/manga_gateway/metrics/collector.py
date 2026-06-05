"""The collector emit catalog (D-01) — the one ingest seam for all metric kinds.

A :class:`Collector` holds an :class:`InMemoryStore` ref and exposes the additive
``emit_*`` catalog (``emit_http``, ``emit_solve``, ``emit_package``,
``emit_limiter_wait``, ``emit_job``, ``emit_request``). Each reads BOTH attribution
contextvars (``current_request`` for request_id/surface/endpoint, ``current_source``
for source_key), applies :func:`redact_url` to the URL at THIS boundary (Pitfall 5
— the URL only lives in rings, redact before it enters one), builds a
:class:`MetricEvent`, and calls ``store.ingest(ev)``. Callers pass ZERO request/
source attribution by hand.

A module-level ``set_collector`` / ``get_collector`` accessor lets the framework
seam (Plan 04) emit WITHOUT a hard import of ``app.state``. A ``None`` collector
makes every framework-side emit a no-op, so non-app unit tests and the pre-wiring
app stay byte-for-byte unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .context import current_request, current_source
from .event import MetricEvent
from .redact import redact_url
from .store import InMemoryStore

if TYPE_CHECKING:
    from .snapshot import MetricSnapshotStore

_collector: Collector | None = None


def set_collector(collector: Collector | None) -> None:
    """Install (or clear) the process-wide collector the framework seam reads."""
    global _collector
    _collector = collector


def get_collector() -> Collector | None:
    """Return the installed collector, or ``None`` (framework emits then no-op)."""
    return _collector


class Collector:
    """Builds fully-attributed ``MetricEvent``s from the contextvars + ingests.

    ``store`` (in-memory) holds the rollups and classifies each event's ring
    membership; ``ring_writer`` (the disk ``MetricSnapshotStore``, 260604-wm2) is
    the append-only ring system of record. ``_ingest`` updates the rollup +
    classifies, then enqueues ONE O(1) append to the ring writer (no await, no
    disk write on the hot path). A ``None`` ring_writer makes ingest rollup-only
    (degraded in-memory mode — same posture as the historic deque-only path).
    """

    def __init__(
        self,
        store: InMemoryStore,
        *,
        ring_writer: MetricSnapshotStore | None = None,
    ) -> None:
        self.store = store
        self.ring_writer = ring_writer

    def _ingest(
        self,
        *,
        kind: str,
        op: str | None,
        method: str | None,
        url: str | None,
        status: int | None,
        outcome: str,
        duration_ms: float,
        attempt: int,
        error: str | None,
    ) -> None:
        req = current_request.get()
        ev = MetricEvent(
            ts=time.time(),
            kind=kind,
            request_id=_req_int(req, "request_id"),
            surface=_req_str(req, "surface"),
            endpoint=_req_str(req, "endpoint"),
            source_key=current_source.get(),
            op=op,
            method=method,
            url=redact_url(url),  # Pitfall 5: redact at the ingest boundary
            status=status,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
            error=error,
        )
        # O(1) hot path: update the rollup + get the ring-membership set, then a
        # plain queue append to the disk ring writer (no await, no disk I/O here —
        # the batched flush in app.py does the writes).
        rings = self.store.classify(ev)
        if self.ring_writer is not None:
            self.ring_writer.enqueue(ev, rings)

    # ── D-01 emit catalog ────────────────────────────────────────────────────
    def emit_http(
        self,
        *,
        op: str,
        method: str,
        url: str,
        status: int | None,
        outcome: str,
        duration_ms: float,
        attempt: int,
        error: str | None = None,
    ) -> None:
        self._ingest(
            kind="http",
            op=op,
            method=method,
            url=url,
            status=status,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
            error=error,
        )

    def emit_solve(
        self,
        *,
        source_key: str | None = None,
        outcome: str,
        duration_ms: float,
        attempt: int = 1,
        error: str | None = None,
    ) -> None:
        # source_key normally rides the contextvar; the explicit arg is a fallback
        # for the solver path which may run outside a source_scope.
        if source_key is not None and current_source.get() is None:
            from .context import source_scope

            with source_scope(source_key):
                self._ingest(
                    kind="solve",
                    op="solve",
                    method=None,
                    url=None,
                    status=None,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    attempt=attempt,
                    error=error,
                )
            return
        self._ingest(
            kind="solve",
            op="solve",
            method=None,
            url=None,
            status=None,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
            error=error,
        )

    def emit_package(
        self,
        *,
        op: str,
        outcome: str,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        self._ingest(
            kind="package",
            op=op,
            method=None,
            url=None,
            status=None,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=1,
            error=error,
        )

    def emit_limiter_wait(
        self,
        *,
        outcome: str = "ok",
        duration_ms: float,
    ) -> None:
        self._ingest(
            kind="limiter-wait",
            op="acquire",
            method=None,
            url=None,
            status=None,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=1,
            error=None,
        )

    def emit_job(
        self,
        *,
        op: str,
        outcome: str,
        duration_ms: float = 0.0,
        error: str | None = None,
    ) -> None:
        self._ingest(
            kind="job",
            op=op,
            method=None,
            url=None,
            status=None,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=1,
            error=error,
        )

    def emit_request(
        self,
        *,
        status: int | None = None,
        outcome: str,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        # NOTE: endpoint/surface/request_id are read from current_request by
        # _ingest (the middleware set them for the whole request scope), so this
        # helper takes NO endpoint arg — a passed value would have been silently
        # dropped (WR-03).
        self._ingest(
            kind="request",
            op=None,
            method=None,
            url=None,
            status=status,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=1,
            error=error,
        )


def _req_int(req: dict[str, object] | None, key: str) -> int | None:
    if req is None:
        return None
    val = req.get(key)
    return val if isinstance(val, int) else None


def _req_str(req: dict[str, object] | None, key: str) -> str | None:
    if req is None:
        return None
    val = req.get(key)
    return val if isinstance(val, str) else None
