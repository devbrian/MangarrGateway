"""Default logging configuration (#21).

Pins ``manga_gateway*`` loggers to INFO and noisy third-party libraries to
WARNING, with a single-line, machine-parseable format. Applied at process
startup from ``__main__``; never auto-applied at import time so tests retain
control of the root handler and ``pytest``'s ``caplog`` keeps working.

Operator override: set ``GATEWAY_LOG_LEVEL`` (e.g. ``DEBUG``) to widen the
``manga_gateway*`` level without touching code.
"""

from __future__ import annotations

import logging
import logging.config
import os

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that would otherwise drown out gateway lines.
_QUIET_LIBS = (
    "httpx",
    "httpcore",
    "asyncio",
    "patchright",
    "playwright",
    "uvicorn.access",
)


def _resolve_level(env_value: str | None) -> str:
    if env_value is None:
        return "INFO"
    candidate = env_value.strip().upper()
    if candidate not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return "INFO"
    return candidate


def configure_logging(level: str | None = None) -> None:
    """Install the gateway's default logging layout.

    Idempotent: applies a single named formatter + handler and pins the
    ``manga_gateway`` logger so reapplying it does not stack handlers.
    """
    env_value = level if level is not None else os.environ.get("GATEWAY_LOG_LEVEL")
    effective = _resolve_level(env_value)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "gateway": {
                    "format": _DEFAULT_FORMAT,
                    "datefmt": _DEFAULT_DATEFMT,
                },
            },
            "handlers": {
                "gateway_console": {
                    "class": "logging.StreamHandler",
                    "formatter": "gateway",
                },
            },
            "loggers": {
                "manga_gateway": {
                    "level": effective,
                    "handlers": ["gateway_console"],
                    "propagate": False,
                },
                **{lib: {"level": "WARNING"} for lib in _QUIET_LIBS},
            },
            "root": {"level": "WARNING", "handlers": ["gateway_console"]},
        }
    )
