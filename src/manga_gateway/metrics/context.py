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
_request_ids = itertools.count(1)


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
