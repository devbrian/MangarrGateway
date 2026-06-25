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
        result_count: int | None = None,
        candidates_enumerated: int | None = None,
        manga_title: str | None = None,
        chapter_number: float | None = None,
        lane: str | None = None,
    ) -> None:
        req = current_request.get()
        # 260605-e9a: the umbrella ``request`` event's request_blob / result_count /
        # warnings_summary are stashed into ``current_request`` by the route handler
        # (the middleware calls emit_request with no knowledge of them) — read them
        # back here, mirroring how request_id/surface/endpoint are read from the
        # contextvar. For non-request kinds these are None (the keys are absent).
        # A per-source ``source-result`` event passes result_count /
        # candidates_enumerated directly (they are NOT request-level), so the
        # explicit args win over the contextvar for those.
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
            request_blob=_req_dict(req, "request_blob") if kind == "request" else None,
            result_count=(
                result_count
                if result_count is not None
                else (_req_int(req, "result_count") if kind == "request" else None)
            ),
            candidates_enumerated=candidates_enumerated,
            warnings_summary=(
                _req_list(req, "warnings_summary") if kind == "request" else None
            ),
            # 260605-nqo: resolved manga_title/chapter_number stashed into
            # current_request by the download routes (POST from the record;
            # GET/DELETE-by-id from the persisted Job). Read back for the umbrella
            # ``request`` event from the request stash; for a ``kind="job"`` event
            # (260615-238) the engine passes them EXPLICITLY (it runs outside
            # request scope), so an explicit arg WINS — mirroring how result_count
            # lets a per-source event override the contextvar above.
            manga_title=(
                manga_title
                if manga_title is not None
                else (_req_str(req, "manga_title") if kind == "request" else None)
            ),
            chapter_number=(
                chapter_number
                if chapter_number is not None
                else (_req_float(req, "chapter_number") if kind == "request" else None)
            ),
            # 260605-wab: per-item GET /downloads queue contents stashed into
            # current_request by the route. Read back for the umbrella ``request``
            # event only — None for every other kind. Reuses the existing _req_list
            # accessor (already returns list[dict[str, object]] | None).
            queue_items=_req_list(req, "queue_items") if kind == "request" else None,
            # 15-03 OBS-01: the non-secret solver-lane label, passed explicitly by
            # emit_solve/emit_eval (AndroidSolver call sites in plan 15-04). Absent
            # on every other emitter -> None (additive default), byte-for-byte today.
            lane=lane,
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
        lane: str | None = None,
    ) -> None:
        # source_key normally rides the contextvar; the explicit arg is a fallback
        # for the solver path which may run outside a source_scope. ``lane`` (OBS-01)
        # is the non-secret solver-lane label threaded into the emitted event in BOTH
        # branches; omitting it keeps lane=None (byte-for-byte today).
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
                    lane=lane,
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
            lane=lane,
        )

    def emit_eval(
        self,
        *,
        source_key: str | None = None,
        op: str = "eval",
        outcome: str,
        duration_ms: float,
        attempt: int = 1,
        error: str | None = None,
        lane: str | None = None,
    ) -> None:
        """Emit an in-WebView eval event (kind=``eval``) — the eval analog of
        ``emit_solve``, with a REAL (non-zero) ``duration_ms``.

        For comix's android-WebView ``/eval`` path (search / recent / chapter-list /
        chapter-pages all hit comix.to through the sidecar ``/eval``). Mirrors
        ``emit_solve`` for source attribution: ``source_key`` normally rides
        ``current_source``, but the eval call site may run outside a source_scope, so
        the explicit arg is wrapped in ``source_scope`` as a fallback.

        D-06 / T-10-04 / T-14-04 redaction: ``url=None`` — same posture as
        ``emit_solve`` / ``emit_cache`` — so the eval js, the eval result, and any
        cf_clearance/token are STRUCTURALLY impossible to record. Source attribution
        (comix) rides ``source_key``; the host (comix.to) is implied by the source.
        """
        if source_key is not None and current_source.get() is None:
            from .context import source_scope

            with source_scope(source_key):
                self._ingest(
                    kind="eval",
                    op=op,
                    method=None,
                    url=None,
                    status=None,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    attempt=attempt,
                    error=error,
                    lane=lane,
                )
            return
        self._ingest(
            kind="eval",
            op=op,
            method=None,
            url=None,
            status=None,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
            error=error,
            lane=lane,
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
        manga_title: str | None = None,
        chapter_number: float | None = None,
    ) -> None:
        # 260615-238: the engine runs OUTSIDE request scope, so the request-stash
        # seam that populates manga_title/chapter_number for ``request`` events is
        # unavailable here. The engine threads them EXPLICITLY from the ``Job`` so a
        # ``kind="job"`` event self-attributes to a series/chapter instead of null.
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
            manga_title=manga_title,
            chapter_number=chapter_number,
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

    def emit_source_result(
        self,
        *,
        result_count: int,
        candidates_enumerated: int | None = None,
    ) -> None:
        """Emit a per-source summary event (260605-e9a deliverables 3+5).

        kind=``source-result``, op=``result``. ``source_key`` self-attributes via
        ``current_source`` (already bound by ``fanout._guarded``) — ZERO new
        attribution plumbing. Carries the source's PRE-MERGE ``result_count`` (the
        172/154/50/5 numbers) and ``candidates_enumerated`` (how many ≤5 title
        candidates were deep-enumerated). A dedicated per-source event (NOT a nested
        map on the umbrella request event) so each ring read stays a single indexed
        query + the breakdown shows it as one flat row per source.
        """
        self._ingest(
            kind="source-result",
            op="result",
            method=None,
            url=None,
            status=None,
            outcome="ok",
            duration_ms=0.0,
            attempt=1,
            error=None,
            result_count=result_count,
            candidates_enumerated=candidates_enumerated,
        )

    def emit_cache(
        self,
        *,
        op: str,
        outcome: str,
        source_key: str | None = None,
    ) -> None:
        """Emit an enumeration-cache event (Phase 09, kind=``cache``).

        ``op`` is the cache layer (``resolve`` = Layer 1 title→id, ``enumerate`` =
        Layer 2 chapter feed); ``outcome`` ∈ ``{hit, miss, refetch}``. Mirrors
        ``emit_solve`` for source attribution: ``source_key`` normally rides
        ``current_source``, but the cache call site may run outside a source_scope,
        so the explicit arg is wrapped in ``source_scope`` as a fallback.

        D-06 redaction: ``url=None`` so the raw query / series-id is NEVER recorded
        — only the ``source_key`` + layer ``op`` cross into the metrics store.
        """
        if source_key is not None and current_source.get() is None:
            from .context import source_scope

            with source_scope(source_key):
                self._ingest(
                    kind="cache",
                    op=op,
                    method=None,
                    url=None,
                    status=None,
                    outcome=outcome,
                    duration_ms=0.0,
                    attempt=1,
                    error=None,
                )
            return
        self._ingest(
            kind="cache",
            op=op,
            method=None,
            url=None,
            status=None,
            outcome=outcome,
            duration_ms=0.0,
            attempt=1,
            error=None,
        )

    def emit_browser(
        self,
        *,
        op: str,
        url: str,
        status: int | None = None,
        outcome: str,
        duration_ms: float,
        attempt: int = 1,
        error: str | None = None,
    ) -> None:
        """Emit a browser-read event (#125, kind=``browser``).

        For comix per-candidate ``fetch_via_browser`` chapter reads. ``url`` is
        redacted at the ingest boundary (same path as ``emit_http``);
        ``source_key`` + ``request_id`` self-attribute via the contextvars already
        bound by ``fanout._guarded`` (the browser read runs inside ``_run_one``
        inside the fan-out's ``source_scope``).
        """
        self._ingest(
            kind="browser",
            op=op,
            method=None,
            url=url,
            status=status,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
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


def _req_float(req: dict[str, object] | None, key: str) -> float | None:
    if req is None:
        return None
    val = req.get(key)
    # bool is an int subclass — exclude it explicitly so a stashed True/False never
    # coerces to 1.0/0.0. Accept int OR float and coerce to float; else None.
    if isinstance(val, bool):
        return None
    if isinstance(val, int | float):
        return float(val)
    return None


def _req_dict(req: dict[str, object] | None, key: str) -> dict[str, object] | None:
    if req is None:
        return None
    val = req.get(key)
    return val if isinstance(val, dict) else None


def _req_list(
    req: dict[str, object] | None, key: str
) -> list[dict[str, object]] | None:
    if req is None:
        return None
    val = req.get(key)
    return val if isinstance(val, list) else None
