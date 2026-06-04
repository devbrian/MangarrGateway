"""Central structured JSON-lines logging configuration (OBS-09).

This is the gateway's single ``dictConfig``. It supersedes the original text-line
config (#21): the same ``"manga_gateway"`` logger every module already binds via
``logging.getLogger("manga_gateway")`` / ``.*`` now emits structured JSON lines to BOTH
stdout (12-factor) AND a size-rotating file under the operator-configured log dir
(``<log_dir>/gateway.jsonl``, D-07/D-08), with zero per-module edits.

What this configures:

* :class:`JsonFormatter` — renders each record as a single JSON object
  ``{ts, level, logger, msg, request_id}`` (+ ``exc`` when ``record.exc_info``), then
  passes the rendered line through :func:`~manga_gateway.metrics.redact.redact_text`
  BEFORE returning it (D-05 — redact the final line, not the dict), so the
  ``<log_dir>/gateway.jsonl`` file the dashboard reads off the shared volume is
  redaction-safe (T-08-08).
* :class:`RequestIdFilter` — reads the ``current_request`` contextvar the metrics seam
  sets and stamps ``record.request_id`` (``None`` outside a request scope). A log line
  and its metric event therefore share one ``request_id`` (the D-08 join key, T-08-11);
  startup / snapshot-timer lines carry ``request_id: null`` — correct.
* :func:`configure_logging` — ``mkdir``-s the log dir then ``dictConfig``-attaches the
  formatter, the filter, a stdout ``StreamHandler``, and a size-rotating
  ``RotatingFileHandler`` to the ``"manga_gateway"`` logger at ``settings.log_level``.
  It also pins noisy third-party loggers (``httpx`` et al.) to WARNING — carried over
  from #21 so library access spam never drowns out gateway lines.

Rotation is SIZE-based (``RotatingFileHandler``), NOT time-based: log volume tracks
request traffic, so a byte cap (``log_max_bytes`` × ``log_backup_count`` ≈ 60 MB) bounds
disk use on the ``/state`` volume predictably regardless of how busy the gateway
is (D-07, T-08-09). The path is the fixed operator-config ``<log_dir>/gateway.jsonl`` —
NEVER a client-supplied value (T-08-10).

stdlib only — ``python-json-logger`` was deliberately rejected (RESEARCH §Q3): a
``logging.Formatter`` subclass is a few lines and avoids a dependency.

**Deliberate CLAUDE.md trade-off (accepted exception to "offload blocking work via
``asyncio.to_thread``").** The stdlib ``RotatingFileHandler.emit()`` writes
synchronously on whatever thread logs (including the asyncio event-loop thread) because
Python's ``logging`` is synchronous by design. This is the conventional async-logging
pattern and is left as-is: log lines are small, OS-buffered, sub-millisecond writes,
and wrapping ``logger.*`` calls in ``asyncio.to_thread`` would add overhead and REORDER
lines (losing the temporal ordering that makes a log useful). Revisit only if profiling
shows the file handler stalling the loop under load.
"""

from __future__ import annotations

import json
import logging
import logging.config
from pathlib import Path
from typing import TYPE_CHECKING

from .metrics.context import current_request
from .metrics.redact import redact_text

if TYPE_CHECKING:
    from .config import Settings

_LOG_FILENAME = "gateway.jsonl"

# Third-party loggers that would otherwise drown out gateway lines (#21).
_QUIET_LIBS = (
    "httpx",
    "httpcore",
    "asyncio",
    "patchright",
    "playwright",
    "uvicorn.access",
)


class JsonFormatter(logging.Formatter):
    """Render a record as one redacted JSON object (the JSON-lines wire shape).

    Emits ``{ts, level, logger, msg, request_id}`` (+ ``exc`` when the record carries
    ``exc_info``), then runs the rendered line through :func:`redact_text` so secrets
    (cf_clearance / Cookie / Authorization / proxy creds) never reach the log file the
    dashboard reads (D-05).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact_text(json.dumps(payload))


class RequestIdFilter(logging.Filter):
    """Stamp ``record.request_id`` from the ``current_request`` contextvar (D-08).

    The same contextvar the metrics middleware sets is read here, so a log line and its
    metric event share the identical ``request_id``. Outside any request scope (startup,
    snapshot timer) ``current_request`` is ``None`` and ``request_id`` is ``None``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        req = current_request.get()
        record.request_id = req["request_id"] if req else None
        return True


def configure_logging(settings: Settings) -> None:
    """Attach the central JSON-lines logging config to the ``"manga_gateway"`` logger.

    Creates ``settings.log_dir`` (the ``/state`` volume exists but ``logs/`` may not),
    then ``dictConfig``-attaches the :class:`JsonFormatter`, the
    :class:`RequestIdFilter`, a stdout ``StreamHandler``, and a size-rotating
    ``RotatingFileHandler`` writing
    ``<log_dir>/gateway.jsonl`` (``maxBytes=settings.log_max_bytes``,
    ``backupCount=settings.log_backup_count``) — all at ``settings.log_level``. Noisy
    third-party loggers are pinned to WARNING (#21).

    ``disable_existing_loggers=False`` so module loggers created at import time before
    this runs are NOT silenced. Reapplying it REPLACES the ``"manga_gateway"`` handler
    list rather than stacking handlers (``dictConfig`` semantics) — it stays idempotent.

    **Graceful degradation when ``log_dir`` is unwritable.** The ``mkdir`` +
    ``RotatingFileHandler`` provisioning is wrapped in ``try/except OSError``. On a
    host where the dir cannot be created (Linux CI: ``/state`` read-only), the file
    handler is SKIPPED and the gateway runs stdout-only (still JSON-lines, still
    redacted), emitting a single WARNING recording that file logging was disabled
    and why. ``configure_logging`` therefore NEVER raises on an unwritable dir —
    app construction (and the test gate) always succeeds.
    """
    # Attempt to provision the size-rotating file handler under ``log_dir``. The
    # default ``/state/logs`` is the docker-volume path and is correct in prod, but
    # on a host where that dir is not writable (Linux CI: ``/state`` is read-only;
    # the app is constructed at test-collection time) the ``mkdir`` raises
    # ``PermissionError`` and previously crashed app construction — and with it the
    # whole gate (CodeRabbit). Graceful degradation: if the dir cannot be created,
    # SKIP the file handler and run stdout-only (still JSON-lines, still redacted),
    # so the gateway always starts. This is NOT a default change — the path stays.
    handlers: dict[str, dict[str, object]] = {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["request_id"],
            "stream": "ext://sys.stdout",
        },
    }
    handler_names = ["stdout"]
    file_disabled_reason: str | None = None
    log_dir = Path(settings.log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / _LOG_FILENAME
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "json",
            "filters": ["request_id"],
            "filename": str(log_path),
            "maxBytes": settings.log_max_bytes,
            "backupCount": settings.log_backup_count,
            "encoding": "utf-8",
        }
        handler_names.append("file")
    except OSError as exc:
        # PermissionError (read-only /state on CI) or any other OSError: keep
        # stdout-only and record WHY after dictConfig wires up the logger.
        file_disabled_reason = f"{type(exc).__name__}: {exc}"

    config: dict[str, object] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": f"{__name__}.JsonFormatter"},
        },
        "filters": {
            "request_id": {"()": f"{__name__}.RequestIdFilter"},
        },
        "handlers": handlers,
        "loggers": {
            "manga_gateway": {
                "level": settings.log_level,
                "handlers": handler_names,
                "propagate": False,
            },
            **{lib: {"level": "WARNING"} for lib in _QUIET_LIBS},
        },
    }
    try:
        logging.config.dictConfig(config)
    except Exception as exc:  # noqa: BLE001 — handler instantiation must not abort startup
        # mkdir succeeding does NOT guarantee the file handler can be built:
        # dictConfig is what actually instantiates the RotatingFileHandler (opens
        # the file), which can still fail when the dir exists but the file open is
        # denied (read-only mount, the path is a directory, etc.). Catch it here too
        # and fall back to a stdout-only config so app construction never aborts
        # (CodeRabbit). Mutating handlers/handler_names also updates ``config`` (same
        # objects), so the retry uses the stdout-only handler set.
        handlers.pop("file", None)
        if "file" in handler_names:
            handler_names.remove("file")
        file_disabled_reason = f"{type(exc).__name__}: {exc}"
        logging.config.dictConfig(config)  # stdout-only — no file handler to fail on

    if file_disabled_reason is not None:
        # Emit ONCE through the now-configured logger so the operator sees on
        # stdout that file logging is off and why (the gateway still runs).
        logging.getLogger("manga_gateway").warning(
            "file logging disabled (log_dir=%s): %s; "
            "continuing with stdout-only JSON logging",
            settings.log_dir,
            file_disabled_reason,
        )
