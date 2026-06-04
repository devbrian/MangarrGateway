"""Central JSON-lines logging-config tests (OBS-09, Plan 08-03).

``manga_gateway.logging_config.configure_logging(settings)`` is the explicit,
idempotent entry-point ``__main__`` calls before ``create_app``. These tests verify the
four load-bearing OBS-09 truths against a ``log_dir`` pointed at ``tmp_path`` so the
suite NEVER writes to ``/state``:

* every emitted line is a single valid JSON object carrying ``ts/level/logger/msg/
  request_id`` (the five core keys);
* a line emitted inside a ``current_request.set({"request_id": ...})`` scope carries
  that same ``request_id`` (the D-08 join key); outside any scope it is ``null``;
* secrets (``cf_clearance`` value, proxy credentials) are redacted in the WRITTEN
  line while proxy ``host:port`` is preserved (D-04/D-05);
* the file handler is a size-rotating ``RotatingFileHandler`` configured with
  ``settings.log_max_bytes`` / ``settings.log_backup_count`` and writes
  ``<log_dir>/gateway.jsonl``.

These tests deliberately mutate global logging state; the autouse fixture snapshots and
restores the loggers ``configure_logging`` touches so ``pytest``'s ``caplog`` (which
relies on root propagation) stays usable in the rest of the suite.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest

from manga_gateway.config import Settings
from manga_gateway.logging_config import _QUIET_LIBS, configure_logging
from manga_gateway.metrics.context import current_request

TEST_API_KEY = "test-key-deadbeef"


def _settings(
    tmp_path: Path,
    *,
    log_level: str = "INFO",
    log_max_bytes: int = 10_000_000,
    log_backup_count: int = 5,
) -> Settings:
    """A ``Settings`` whose ``log_dir`` is ``tmp_path`` (never ``/state``)."""
    return Settings(
        api_key=TEST_API_KEY,
        log_dir=str(tmp_path),
        log_level=log_level,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Snapshot + restore the loggers configure_logging touches."""
    touched_names = ("manga_gateway", *_QUIET_LIBS)
    snapshots: dict[str, tuple[int, bool, list[logging.Handler]]] = {}
    for name in touched_names:
        logger = logging.getLogger(name)
        snapshots[name] = (logger.level, logger.propagate, list(logger.handlers))
    root = logging.getLogger()
    root_snapshot = (root.level, list(root.handlers))
    yield
    for name, (level, propagate, handlers) in snapshots.items():
        logger = logging.getLogger(name)
        # Close any rotating file handlers we attached so the tmp file is released
        # (Windows keeps an exclusive lock on the open file otherwise).
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers = handlers
        logger.level = level
        logger.propagate = propagate
    root.handlers = root_snapshot[1]
    root.level = root_snapshot[0]


def _read_file_lines(tmp_path: Path) -> list[str]:
    """Flush handlers, then return the non-empty lines written to gateway.jsonl."""
    logger = logging.getLogger("manga_gateway")
    for handler in logger.handlers:
        handler.flush()
    text = (tmp_path / "gateway.jsonl").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def test_emitted_line_is_valid_json_with_core_keys(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path))
    logging.getLogger("manga_gateway").info("hello world")
    lines = _read_file_lines(tmp_path)
    assert lines, "a line must be written to gateway.jsonl"
    payload = json.loads(lines[-1])  # must be a single valid JSON object
    assert set(payload) >= {"ts", "level", "logger", "msg", "request_id"}
    assert payload["level"] == "INFO"
    assert payload["logger"] == "manga_gateway"
    assert payload["msg"] == "hello world"


def test_request_id_correlates_with_contextvar(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path))
    log = logging.getLogger("manga_gateway")

    # Outside any request scope → request_id is null.
    log.info("outside")
    outside = json.loads(_read_file_lines(tmp_path)[-1])
    assert outside["request_id"] is None

    # Inside a request scope → the line carries the SAME request_id (D-08).
    token = current_request.set(
        {"request_id": 7, "surface": "search", "endpoint": "/caps"}
    )
    try:
        log.info("inside")
    finally:
        current_request.reset(token)
    inside = json.loads(_read_file_lines(tmp_path)[-1])
    assert inside["request_id"] == 7


def test_secrets_are_redacted_in_the_written_line(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path))
    log = logging.getLogger("manga_gateway")
    log.info(
        "fetch via cf_clearance=SUPERSECRET through http://user:pass@proxy.example:8080"
    )
    written = "\n".join(_read_file_lines(tmp_path))
    # D-05: the secret values are masked in the file the dashboard reads.
    assert "SUPERSECRET" not in written
    assert "user:pass" not in written
    assert "cf_clearance=***" in written
    # D-04: proxy host:port is forensic signal and is KEPT.
    assert "proxy.example:8080" in written


def test_rotating_file_handler_uses_settings_and_writes_jsonl(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path, log_max_bytes=12345, log_backup_count=7))
    handlers = logging.getLogger("manga_gateway").handlers
    rotating = [
        h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1, "exactly one size-rotating file handler is attached"
    handler = rotating[0]
    # Size-based rotation configured from settings (not TimedRotatingFileHandler).
    assert not isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    assert handler.maxBytes == 12345
    assert handler.backupCount == 7
    assert Path(handler.baseFilename).name == "gateway.jsonl"
    # The file lives under the tmp log dir, never /state.
    assert Path(handler.baseFilename).parent == tmp_path.resolve()


def test_stdout_and_file_handlers_both_attached(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path))
    handlers = logging.getLogger("manga_gateway").handlers
    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in handlers
    )
    has_file = any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers
    )
    assert has_stream, "a stdout StreamHandler is attached (12-factor)"
    assert has_file, "a rotating file handler is attached (/state volume)"


def test_third_party_loggers_pinned_to_warning(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path))
    for noisy in ("httpx", "httpcore", "patchright", "uvicorn.access"):
        assert logging.getLogger(noisy).getEffectiveLevel() == logging.WARNING


def test_log_level_setting_is_honored(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path, log_level="ERROR"))
    assert logging.getLogger("manga_gateway").getEffectiveLevel() == logging.ERROR


def test_idempotent_does_not_stack_handlers(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path))
    first = list(logging.getLogger("manga_gateway").handlers)
    configure_logging(_settings(tmp_path))
    second = list(logging.getLogger("manga_gateway").handlers)
    # dictConfig replaces the handler list when reapplied — it does not stack.
    assert len(second) == len(first)


def test_creates_missing_log_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "logs"
    assert not nested.exists()
    configure_logging(_settings(nested))
    logging.getLogger("manga_gateway").info("make the dir")
    assert (nested / "gateway.jsonl").exists()
