"""Logging-config tests (#21).

``manga_gateway.logging_config.configure_logging`` is the explicit, idempotent
entry-point that ``__main__`` calls at startup. The tests verify:

* default ``manga_gateway*`` level is INFO so existing ``_log.info(...)`` lines
  reach the console;
* ``GATEWAY_LOG_LEVEL=DEBUG`` widens it to DEBUG;
* third-party loggers (``httpx`` et al.) stay at WARNING;
* reapplying the config does NOT stack handlers (idempotent).

These tests deliberately mutate the global logging state; they snapshot and
restore handlers/levels around each case so ``pytest``'s ``caplog`` (which
relies on root propagation) stays usable in the rest of the suite.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from manga_gateway.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Snapshot + restore the loggers configure_logging touches."""
    touched_names = (
        "manga_gateway",
        "httpx",
        "httpcore",
        "asyncio",
        "patchright",
        "playwright",
        "uvicorn.access",
    )
    snapshots: dict[str, tuple[int, bool, list[logging.Handler]]] = {}
    for name in touched_names:
        logger = logging.getLogger(name)
        snapshots[name] = (logger.level, logger.propagate, list(logger.handlers))
    root = logging.getLogger()
    root_snapshot = (root.level, list(root.handlers))
    yield
    for name, (level, propagate, handlers) in snapshots.items():
        logger = logging.getLogger(name)
        logger.handlers = handlers
        logger.level = level
        logger.propagate = propagate
    root.handlers = root_snapshot[1]
    root.level = root_snapshot[0]


def test_default_level_is_info_for_gateway_logger() -> None:
    configure_logging()
    assert logging.getLogger("manga_gateway").getEffectiveLevel() == logging.INFO


def test_third_party_loggers_pinned_to_warning() -> None:
    configure_logging()
    # WARNING keeps httpx access lines + uvicorn access spam out of the console.
    for noisy in ("httpx", "httpcore", "patchright", "uvicorn.access"):
        assert logging.getLogger(noisy).getEffectiveLevel() == logging.WARNING


def test_env_override_widens_to_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_LOG_LEVEL", "DEBUG")
    configure_logging()
    assert logging.getLogger("manga_gateway").getEffectiveLevel() == logging.DEBUG


def test_explicit_level_argument_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_LOG_LEVEL", "DEBUG")
    configure_logging(level="ERROR")
    assert logging.getLogger("manga_gateway").getEffectiveLevel() == logging.ERROR


def test_invalid_env_value_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_LOG_LEVEL", "not-a-level")
    configure_logging()
    assert logging.getLogger("manga_gateway").getEffectiveLevel() == logging.INFO


def test_idempotent_does_not_stack_handlers() -> None:
    configure_logging()
    first = list(logging.getLogger("manga_gateway").handlers)
    configure_logging()
    second = list(logging.getLogger("manga_gateway").handlers)
    # dictConfig replaces the handler list when reapplied — not stacks it.
    assert len(second) == len(first)


def test_handler_format_string_includes_level_logger_message() -> None:
    """The gateway handler's format string emits level + logger name + message.

    We inspect the formatter directly rather than asserting through ``caplog``:
    ``configure_logging`` installs a dedicated handler on ``manga_gateway`` with
    ``propagate=False`` (so a record never reaches ``caplog``'s root hook), and
    that is intentional — operator-facing output must be format-stable and not
    depend on the root configuration.
    """
    configure_logging()
    handlers = logging.getLogger("manga_gateway").handlers
    assert handlers, "configure_logging must attach a handler"
    formatter = handlers[0].formatter
    assert formatter is not None
    fmt = formatter._fmt or ""
    # Order-independent: any layout containing these three placeholders works.
    assert "%(levelname)s" in fmt
    assert "%(name)s" in fmt
    assert "%(message)s" in fmt
