"""CORS default-deny + allowlist behavior for the metrics surface (OBS-07, T-08-17).

Proves: with ``metrics_cors_origins`` unset the gateway is default-deny (no
``access-control-allow-origin`` header, the CORSMiddleware is not even added); with an
explicit allowlist an ``OPTIONS`` preflight for the custom ``X-Api-Key`` header passes
and never sets ``allow_credentials=true`` (auth is header-based, not cookies).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from manga_gateway.app import create_app
from manga_gateway.config import Settings

from .conftest import TEST_API_KEY

_ALLOWED_ORIGIN = "http://localhost:5173"
_ADMIN = "http://testserver/admin/metrics/v1"


@pytest_asyncio.fixture
async def _client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=_ADMIN) as ac:
            yield ac


@pytest.mark.asyncio
async def test_default_deny_no_cors_header_when_origins_unset() -> None:
    """Unset origins → no CORS middleware → no allow-origin header on a GET."""
    app = create_app(Settings(api_key=TEST_API_KEY))  # metrics_cors_origins defaults []
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=_ADMIN) as ac:
            resp = await ac.get(
                "/summary",
                headers={"X-Api-Key": TEST_API_KEY, "Origin": _ALLOWED_ORIGIN},
            )
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.asyncio
async def test_default_deny_preflight_not_handled_when_origins_unset() -> None:
    """An OPTIONS preflight gets no allow-origin header when CORS is not configured."""
    app = create_app(Settings(api_key=TEST_API_KEY))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=_ADMIN) as ac:
            resp = await ac.options(
                "/summary",
                headers={
                    "Origin": _ALLOWED_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "X-Api-Key",
                },
            )
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.asyncio
async def test_allowlisted_origin_preflight_passes() -> None:
    """A configured origin: the X-Api-Key preflight returns the allow headers."""
    app = create_app(
        Settings(api_key=TEST_API_KEY, metrics_cors_origins=[_ALLOWED_ORIGIN])
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=_ADMIN) as ac:
            resp = await ac.options(
                "/summary",
                headers={
                    "Origin": _ALLOWED_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "X-Api-Key",
                },
            )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == _ALLOWED_ORIGIN
    allow_headers = resp.headers.get("access-control-allow-headers", "").lower()
    assert "x-api-key" in allow_headers
    # Header-based auth → credentials must NOT be allowed (no cookies, no "*"-creds).
    assert resp.headers.get("access-control-allow-credentials") != "true"


@pytest.mark.asyncio
async def test_disallowed_origin_not_reflected() -> None:
    """An origin NOT on the allowlist is not echoed back (no cross-origin access)."""
    app = create_app(
        Settings(api_key=TEST_API_KEY, metrics_cors_origins=[_ALLOWED_ORIGIN])
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=_ADMIN) as ac:
            resp = await ac.get(
                "/summary",
                headers={
                    "X-Api-Key": TEST_API_KEY,
                    "Origin": "http://evil.example",
                },
            )
    assert resp.headers.get("access-control-allow-origin") != "http://evil.example"
