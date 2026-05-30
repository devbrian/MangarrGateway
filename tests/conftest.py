"""Shared test fixtures.

The app is always built with an explicitly-injected fixed API key via
``create_app(Settings(api_key=...))`` (D-03) — never by reading the generated
config file and never from an environment variable. This keeps the in-process
ASGI tests deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.framework.antibot import Clearance, CloudflareSolver


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

    async def _noop_warm(self: CloudflareSolver) -> None:
        return None

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
def app() -> FastAPI:
    """A fresh app built with the fixed test key (D-03)."""
    return create_app(Settings(api_key=TEST_API_KEY))


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
