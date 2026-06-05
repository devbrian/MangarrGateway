"""Request- + source-scoped attribution carriers (the 3-edit metrics seam, OBS-02).

Two :class:`contextvars.ContextVar` carriers and one ``source_scope`` helper:

* ``current_request`` (name ``"mg_request"``) — set ONCE at the pure-ASGI layer
  (Plan 05 middleware) to ``{"request_id", "surface", "endpoint"}`` and read
  inward by the collector emit fns (Plan 04 framework seam) and the logging filter
  (Plan 03). Critical (Pitfall 2): set it in the pure-ASGI ``__call__`` BEFORE
  ``await self.app(...)``, never inside ``BaseHTTPMiddleware.dispatch`` — a value
  set there lives in a separate anyio-task context copy and won't propagate inward.
* ``current_source`` (name ``"mg_source"``) — bound per fan-out child via
  ``source_scope`` (Plan 04, ``fanout._guarded``). asyncio copies the current
  context into each Task at creation, so sibling sources never cross-attribute.

The names ``mg_request`` / ``mg_source`` are imported by the middleware, the
framework seam, and the logging filter — do NOT rename.
"""

from __future__ import annotations

import contextvars
import itertools
from collections.abc import Iterator
from contextlib import contextmanager

current_request: contextvars.ContextVar[dict[str, object] | None] = (
    contextvars.ContextVar("mg_request", default=None)
)
current_source: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mg_source", default=None
)

# Monotonic request-id source for the ASGI middleware (Plan 05).
#
# Restart-monotonic (260604-wm2): the counter is RESEEDED on startup from
# MAX(request_id)+1 in the on-disk ring_events table (1 on an empty/missing/
# degraded DB) so ids climb across restarts instead of resetting to 1 and
# colliding with persisted history. ``seed_request_ids`` rebinds this module
# global IN PLACE; every reader goes through the ``next_request_id()`` accessor
# (NOT a by-value import of ``_request_ids``) so the reseed is visible.
_request_ids = itertools.count(1)


def seed_request_ids(start: int) -> None:
    """Reseed the request-id counter (restart-monotonic, 260604-wm2).

    Rebinds the module-global ``_request_ids`` to ``itertools.count(max(1,
    start))``. Called once from the lifespan with ``MAX(request_id)+1`` from the
    persisted ring_events (or 1 on an empty/missing/degraded DB). ``max(1, ...)``
    keeps the first id ≥ 1 even if a caller passes 0 (empty-DB MAX → 0, +1 → 1;
    the guard is belt-and-suspenders against a bare 0/negative seed).
    """
    global _request_ids
    _request_ids = itertools.count(max(1, start))


def next_request_id() -> int:
    """Return the next monotonic request id (the middleware's mint accessor).

    Read THROUGH this function (never import ``_request_ids`` by value) so a
    lifespan ``seed_request_ids`` rebind is observed by the caller.
    """
    return next(_request_ids)


@contextmanager
def source_scope(source_key: str) -> Iterator[None]:
    """Bind ``current_source`` for the duration of one fan-out child (Pattern 1).

    Because the Task got its own context copy, siblings can't clobber each other;
    the ``finally`` resets to the prior value (``None`` at the top level).
    """
    token = current_source.set(source_key)
    try:
        yield
    finally:
        current_source.reset(token)
