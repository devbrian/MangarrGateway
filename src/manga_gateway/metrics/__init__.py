"""In-process metrics data layer (Phase 8 — OBS-03/04/07/08, SEC-01).

The hot-path core every downstream Phase 8 plan builds on: the flat event schema
(:mod:`event`), the request/source attribution contextvars (:mod:`context`), the
shared secret-redaction scrub (:mod:`redact`), the HDR-backed rollups + bounded
rings store (:mod:`store`), the collector emit catalog (:mod:`collector`), and the
aiosqlite snapshot/rehydrate store (:mod:`snapshot`).

Built standalone so it is exhaustively unit-testable without the app, the network,
or the framework wired up.
"""

from __future__ import annotations

from .collector import Collector, get_collector, set_collector
from .context import current_request, current_source, source_scope
from .event import MetricEvent
from .redact import redact_text, redact_url
from .snapshot import MetricSnapshotStore, open_metric_store
from .store import InMemoryStore, Rollup

__all__ = [
    "Collector",
    "InMemoryStore",
    "MetricEvent",
    "MetricSnapshotStore",
    "Rollup",
    "current_request",
    "current_source",
    "get_collector",
    "open_metric_store",
    "redact_text",
    "redact_url",
    "set_collector",
    "source_scope",
]
