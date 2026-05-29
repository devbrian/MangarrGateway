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

# Deterministic key injected into every test app (D-03). NOT read from a file,
# NOT taken from the environment.
TEST_API_KEY = "test-key-deterministic-0123456789"

BASE_URL = "http://testserver/api/v1"


@pytest.fixture
def app() -> FastAPI:
    """A fresh app built with the fixed test key (D-03)."""
    return create_app(Settings(api_key=TEST_API_KEY))


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process httpx client over ASGITransport (no bound port)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
        headers={"X-Api-Key": TEST_API_KEY},
    ) as ac:
        yield ac
