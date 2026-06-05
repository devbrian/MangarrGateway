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
import json
from collections.abc import Iterator
from contextlib import contextmanager

from .redact import redact_blob

# Max serialized length of the captured request body (bytes of the JSON-safe
# form). A hostile/huge body is truncated here at stash time and flagged so it
# cannot bloat the ring or smuggle a large secret payload past truncation review
# (T-e9a-02). 8192 mirrors the constraint's ~8KB cap.
REQUEST_BLOB_BODY_CAP = 8192

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


def _json_safe(value: object) -> object:
    """Coerce ``value`` to a JSON-serializable form (defensive, never raises).

    The body handed in is already a Pydantic ``model_dump(by_alias=True)`` (plain
    dicts/lists/scalars), but a source that stashes something exotic must not break
    the request path — fall back to ``str(value)`` on any non-serializable input.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def stash_request_blob(
    *,
    method: str,
    path: str,
    query_string: str,
    body: object | None,
) -> None:
    """Stash the request blob into the request-scoped ``current_request`` dict.

    Mutates the SAME dict the middleware set for the whole request scope IN PLACE
    (no new contextvar, no token churn) — the middleware finally-block ``_ingest``
    reads it back. No-op when no request scope is active (defensive).

    Secrets are redacted via :func:`redact_blob` AT STASH TIME (defense in depth,
    T-e9a-01) so they never enter the contextvar — and redaction runs BEFORE the
    size cap, because truncation serializes a dict body to a string that
    ``redact_blob`` can no longer key-redact (CodeRabbit #129). The already-redacted
    body is then SIZE-CAPPED at :data:`REQUEST_BLOB_BODY_CAP`: an oversized body is
    truncated to a string and ``body_truncated`` is set ``True``. ``body_truncated``
    is ALWAYS present in the stored blob — defaulted ``False`` and flipped ``True``
    only on truncation — so the stored dict keyset matches
    :class:`~manga_gateway.models.metrics.RequestBlob`.
    """
    req = current_request.get()
    if req is None:
        return
    # Redact BEFORE size-capping (CodeRabbit #129): truncation serializes a dict
    # body to a string, after which redact_blob can no longer mask denylisted keys
    # — so an oversized dict body would leak raw auth/token data. Redact the
    # dict-shaped blob first, THEN truncate the already-redacted body.
    blob: dict[str, object] = redact_blob(
        {
            "method": method,
            "path": path,
            "query_string": query_string,
            "body": _json_safe(body),
        }
    )
    # ALWAYS set body_truncated (False default, True on truncation) AFTER
    # redact_blob — redact_blob keys on the original method/path/query_string/body
    # shape and must not see body_truncated. One O(1) dict assignment on the hot
    # path so the stored keyset always matches RequestBlob.model_fields.
    blob["body_truncated"] = False
    body_val = blob.get("body")
    if body_val is not None:
        encoded = json.dumps(body_val).encode("utf-8")
        if len(encoded) > REQUEST_BLOB_BODY_CAP:
            blob["body"] = encoded[:REQUEST_BLOB_BODY_CAP].decode(
                "utf-8", errors="ignore"
            )
            blob["body_truncated"] = True
    req["request_blob"] = blob


def stash_request_result(
    *,
    result_count: int,
    warnings_summary: list[dict[str, object]],
) -> None:
    """Stash the final merged ``result_count`` + ``warnings_summary`` into the
    request-scoped dict IN PLACE (read by the middleware emit). No-op without a
    request scope. These are inherently request-level (one value per request) so
    they ride the umbrella ``request`` event, not a per-source row."""
    req = current_request.get()
    if req is None:
        return
    req["result_count"] = result_count
    req["warnings_summary"] = warnings_summary


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
