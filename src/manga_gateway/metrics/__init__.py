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

from .context import current_request, current_source, source_scope
from .event import MetricEvent
from .redact import redact_text, redact_url

__all__ = [
    "MetricEvent",
    "current_request",
    "current_source",
    "redact_text",
    "redact_url",
    "source_scope",
]
