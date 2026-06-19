"""Shared test fixtures.

The app is always built with an explicitly-injected fixed API key via
``create_app(Settings(api_key=...))`` (D-03) — never by reading the generated
config file and never from an environment variable. This keeps the in-process
ASGI tests deterministic.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.framework.antibot import Clearance, CloudflareSolver
from manga_gateway.logging_config import _QUIET_LIBS


@pytest.fixture(autouse=True)
def _restore_logging_global() -> Iterator[None]:
    """Snapshot + restore the loggers ``configure_logging`` touches (session-wide).

    ``create_app`` now calls ``configure_logging`` (08-05), which ``dictConfig``-
    attaches handlers and sets ``propagate=False`` on the ``manga_gateway`` logger.
    Without restoration that global state leaks across tests and breaks ``caplog``-
    based assertions in OTHER modules depending on test ordering (caplog attaches to
    the root logger and relies on propagation). Snapshot before, restore after — and
    close any file handlers we attached so Windows releases the lock on the log file.
    """
    touched = ("manga_gateway", *_QUIET_LIBS)
    snapshots: dict[str, tuple[int, bool, list[logging.Handler]]] = {}
    for name in touched:
        logger = logging.getLogger(name)
        snapshots[name] = (logger.level, logger.propagate, list(logger.handlers))
    root = logging.getLogger()
    root_snapshot = (root.level, list(root.handlers))

    # ``test_contract.py`` builds the app at MODULE import time, so configure_logging
    # may have ALREADY run during collection — leaving manga_gateway with
    # propagate=False + dictConfig handlers before the first test even starts. Reset to
    # a clean baseline (propagate=True, no leftover handlers) so ``caplog`` (which
    # relies on propagation to the root logger) captures deterministically regardless
    # of import-time pollution. close() detaches any file handlers (Windows lock).
    for name in touched:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers = []
        logger.propagate = True
        logger.setLevel(logging.NOTSET)

    yield

    for name, (level, propagate, handlers) in snapshots.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers = handlers
        logger.level = level
        logger.propagate = propagate
    root.handlers = root_snapshot[1]
    root.level = root_snapshot[0]


@pytest.fixture(autouse=True)
def _no_real_cloudflare_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``CloudflareSolver.warm`` with a no-op for every deterministic test.

    The lifespan fires ``warm()`` as a non-blocking task (D-33) that tries to
    solve real Cloudflare against ``comix.to`` through a real Patchright
    browser. Locally that completes fast (cookies are cached in
    ``cloudflare-userdata/``); on a fresh CI runner it hangs the worker for
    minutes and eventually gets SIGTERMed by the runner (CI gate exit 143).
    Tests never depend on warm actually solving — they only assert the
    lifespan wired the solver type correctly (R1/BOT-01). Wiping warm to a
    no-op preserves every assertion the deterministic suite makes while
    deleting the only path that touches the network. Individual tests that
    need a specific warm behavior (e.g. forcing failure to exercise the D-33
    ``force_disabled`` path) override this with their own ``monkeypatch``,
    which wins because it runs after this autouse fixture.
    """

    # Return ``[]`` (no failed keys) — matching the real ``warm()`` ``list[str]``
    # contract. Since Phase 10 the lifespan's shared solver is a ``SolverRouter``
    # that calls ``list(await backend.warm())``; a ``None`` here would raise a
    # (swallowed) ``TypeError`` and log noise on every app build.
    async def _noop_warm(self: CloudflareSolver) -> list[str]:
        return []

    monkeypatch.setattr(CloudflareSolver, "warm", _noop_warm)


# Deterministic key injected into every test app (D-03). NOT read from a file,
# NOT taken from the environment.
TEST_API_KEY = "test-key-deterministic-0123456789"

BASE_URL = "http://testserver/api/v1"


class _FakeSolver:
    """Canned ``AntiBotSolver`` for Wave-2/3 tests (satisfies the runtime_checkable
    Protocol). Returns a fixed ``Clearance`` so solver-consuming code is testable
    without a real Patchright browser (RESEARCH Wave-0 gap)."""

    async def get_clearance(self, source_key: str) -> Clearance:
        return Clearance(cookies={"cf_clearance": "X"}, user_agent="UA/1")


@pytest.fixture
def fake_solver() -> _FakeSolver:
    """A canned solver returning a fixed ``Clearance`` (cf_clearance=X, UA/1)."""
    return _FakeSolver()


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """A fresh app built with the fixed test key (D-03).

    The two ``/state``-volume paths are redirected to a per-test tmp dir so tests
    never touch ``/state`` — that default is absent/unwritable outside the docker
    container (e.g. CI), where the metrics snapshot DB open would otherwise fail.
    ``output_root`` (``/data/manga``) and ``db_path`` are LEFT at their defaults:
    they don't break outside docker (the staging sweep tolerates a missing dir;
    ``db_path`` is a relative, writable path), and ``/status`` reports
    ``output_root`` verbatim — overriding it would break the STAT-01 contract test.
    """
    return create_app(
        Settings(
            api_key=TEST_API_KEY,
            metrics_db_path=str(tmp_path / "metrics.db"),
            handle_db_path=str(tmp_path / "handles.db"),
            log_dir=str(tmp_path / "logs"),
        )
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process httpx client over ASGITransport (no bound port).

    httpx ASGITransport does not emit the ASGI lifespan scope, so we drive the
    app's lifespan explicitly — this builds the R1 singleton seams on
    ``app.state`` that seam-reading routes (Plan 02's /caps, /status) rely on.
    """
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            yield ac
