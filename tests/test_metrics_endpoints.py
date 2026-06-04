"""``/admin/metrics/v1/*`` integration tests (OBS-05/06, SEC-01).

In-process ASGITransport against the real app (lifespan-built store + collector +
middleware). Proves every admin endpoint returns shaped JSON WITH the key and 401
WITHOUT it (T-08-16), the per-request breakdown works, ``?limit=0`` is rejected
(T-08-18), and the served JSON is redacted (no secret leaks in urls — SEC-01).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.metrics.collector import get_collector
from manga_gateway.metrics.context import current_request

from .conftest import TEST_API_KEY

_ADMIN = "http://testserver/admin/metrics/v1"

_ENDPOINTS = [
    "/summary",
    "/per-source-endpoint",
    "/failures",
    "/slow",
    "/recent",
]


@pytest.fixture
def metrics_app() -> FastAPI:
    return create_app(Settings(api_key=TEST_API_KEY))


@pytest_asyncio.fixture
async def metrics_client(
    metrics_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated in-process client rooted at the admin base path."""
    transport = httpx.ASGITransport(app=metrics_app)
    async with metrics_app.router.lifespan_context(metrics_app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_ADMIN,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            yield ac


async def _seed_request(app: FastAPI) -> int:
    """Drive one inbound request so the store has events; returns its request_id.

    The request_id is minted from a process-global counter (``_request_ids``), so it
    is NOT deterministically ``1`` in a full suite run — read it back from the most
    recent ``request`` event the middleware emitted.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": TEST_API_KEY},
    ) as ac:
        # A real /api/v1 call flows through the middleware (emits a `request` event).
        await ac.get("/api/v1/version")
        store = app.state.metric_store
        request_event = next(
            e for e in store.recent_calls(50) if e["kind"] == "request"
        )
        request_id = int(request_event["request_id"])
        # Also emit a synthetic http event UNDER that request_id carrying a
        # secret-bearing url to prove redaction reaches the served JSON, and to give
        # the breakdown a child call alongside the request event.
        collector = get_collector()
        assert collector is not None
        current_request.set(
            {"request_id": request_id, "surface": "search", "endpoint": "GET /version"}
        )
        collector.emit_http(
            op="get_json",
            method="GET",
            url="https://comix.example/x?cf_clearance=SECRET&apikey=NOPE",
            status=200,
            outcome="ok",
            duration_ms=10.0,
            attempt=1,
        )
        return request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [*_ENDPOINTS, "/requests/1"])
async def test_requires_api_key(metrics_app: FastAPI, path: str) -> None:
    """Every admin route is 401 without X-Api-Key (T-08-16/SEC-01)."""
    transport = httpx.ASGITransport(app=metrics_app)
    async with metrics_app.router.lifespan_context(metrics_app):
        async with httpx.AsyncClient(
            transport=transport, base_url=_ADMIN
        ) as ac:  # no key
            resp = await ac.get(path)
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _ENDPOINTS)
async def test_endpoints_return_shaped_json_with_key(
    metrics_client: httpx.AsyncClient, path: str
) -> None:
    resp = await metrics_client.get(path)
    assert resp.status_code == 200
    body = resp.json()
    if path == "/summary":
        assert set(body) == {
            "total_calls",
            "total_errors",
            "error_rate",
            "tracked_series",
        }
    else:
        assert isinstance(body, list)


@pytest.mark.asyncio
async def test_summary_counts_seeded_events(metrics_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=metrics_app)
    async with metrics_app.router.lifespan_context(metrics_app):
        await _seed_request(metrics_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_ADMIN,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            resp = await ac.get("/summary")
    body = resp.json()
    assert body["total_calls"] >= 1


@pytest.mark.asyncio
async def test_request_breakdown_returns_calls(metrics_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=metrics_app)
    async with metrics_app.router.lifespan_context(metrics_app):
        request_id = await _seed_request(metrics_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_ADMIN,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            resp = await ac.get(f"/requests/{request_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] == request_id
    assert isinstance(body["calls"], list)
    assert len(body["calls"]) >= 1


@pytest.mark.asyncio
async def test_request_breakdown_unknown_id_is_404(
    metrics_client: httpx.AsyncClient,
) -> None:
    resp = await metrics_client.get("/requests/999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_limit_zero_is_rejected(metrics_client: httpx.AsyncClient) -> None:
    """``?limit=0`` violates Query(ge=1) → rejected (T-08-18).

    The gateway maps request-validation errors to the contract 400 error model
    (ERR-01), not FastAPI's default 422.
    """
    resp = await metrics_client.get("/failures?limit=0")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_served_json_is_redacted(metrics_app: FastAPI) -> None:
    """The url in a served ring event has secrets masked (SEC-01)."""
    transport = httpx.ASGITransport(app=metrics_app)
    async with metrics_app.router.lifespan_context(metrics_app):
        await _seed_request(metrics_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_ADMIN,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            resp = await ac.get("/recent?limit=50")
    raw = resp.text
    assert "SECRET" not in raw
    assert "NOPE" not in raw
    # The secret query values were masked at the ingest boundary; the masked token
    # (urlencoded "***" → "%2A%2A%2A") is present in their place.
    assert "%2A%2A%2A" in raw
