"""``/admin/metrics/v1/*`` integration tests (OBS-05/06, SEC-01).

In-process ASGITransport against the real app (lifespan-built store + collector +
middleware). Proves every admin endpoint returns shaped JSON WITH the key and 401
WITHOUT it (T-08-16), the per-request breakdown works, ``?limit=0`` is rejected
(T-08-18), and the served JSON is redacted (no secret leaks in urls — SEC-01).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

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
def metrics_app(tmp_path: Path) -> FastAPI:
    # Point the metrics DB + logs/output at a writable tmp dir so the disk ring
    # store opens (the default /state path is unwritable outside docker, which
    # would force degraded in-memory-only mode — the ring reads need the disk
    # store live, 260604-wm2).
    return create_app(
        Settings(
            api_key=TEST_API_KEY,
            metrics_db_path=str(tmp_path / "metrics.db"),
            log_dir=str(tmp_path / "logs"),
            output_root=str(tmp_path / "out"),
            db_path=str(tmp_path / "jobs.db"),
        )
    )


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

    The request_id is minted from a process-global counter, so it is NOT
    deterministically ``1`` in a full suite run — read it back from the most recent
    ``request`` event the middleware emitted. Ring events are now on disk and only
    queryable after a flush, so this flushes the lifespan's disk ring store before
    reading and again after the synthetic emit (260604-wm2).
    """
    ring = app.state.metric_snapshot  # the disk ring store (system of record)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": TEST_API_KEY},
    ) as ac:
        # A real /api/v1 call flows through the middleware (emits a `request`
        # event). Must NOT be an endpoint excluded from dashboard metrics
        # (/version, /admin/metrics) — those deliberately emit no request event.
        await ac.get("/api/v1/status")
        # Flush the batched ring queue so the request event is queryable on disk.
        await ring.flush()
        request_event = next(
            e for e in await ring.recent_calls(50) if e["kind"] == "request"
        )
        request_id = int(request_event["request_id"])
        # Also emit a synthetic http event UNDER that request_id carrying a
        # secret-bearing url to prove redaction reaches the served JSON, and to give
        # the breakdown a child call alongside the request event.
        collector = get_collector()
        assert collector is not None
        token = current_request.set(
            {"request_id": request_id, "surface": "search", "endpoint": "GET /status"}
        )
        try:
            collector.emit_http(
                op="get_json",
                method="GET",
                url="https://comix.example/x?cf_clearance=SECRET&apikey=NOPE",
                status=200,
                outcome="ok",
                duration_ms=10.0,
                attempt=1,
            )
        finally:
            # Reset so this synthetic request attribution doesn't leak into later
            # assertions / tests (CodeRabbit).
            current_request.reset(token)
        # Flush again so the synthetic child call is queryable for the breakdown +
        # redaction assertions.
        await ring.flush()
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
async def test_read_endpoints_echo_enriched_fields(metrics_app: FastAPI) -> None:
    """The read endpoints echo the new payload-only fields (request_blob,
    result_count, candidates_enumerated, warnings_summary) VERBATIM — they read
    payload as-is, so an enriched event in the ring surfaces with no code change
    (260605-e9a)."""
    transport = httpx.ASGITransport(app=metrics_app)
    async with metrics_app.router.lifespan_context(metrics_app):
        ring = metrics_app.state.metric_snapshot
        collector = get_collector()
        assert collector is not None
        # Emit an enriched umbrella request event under a known request_id by
        # stashing the blob/result into current_request (the route's path) then
        # emitting (the middleware's path).
        token = current_request.set(
            {
                "request_id": 4242,
                "surface": "search",
                "endpoint": "POST /search",
                "request_blob": {
                    "method": "POST",
                    "path": "/api/v1/search",
                    "query_string": "x=1",
                    "body": {"type": "chapter", "query": "naruto"},
                },
                "result_count": 13,
                "warnings_summary": [{"source_key": "comix", "code": "timeout"}],
            }
        )
        try:
            collector.emit_request(status=200, outcome="ok", duration_ms=5.0)
            # A per-source summary event for the same request.
            from manga_gateway.metrics.context import source_scope

            with source_scope("comix"):
                collector.emit_source_result(result_count=172, candidates_enumerated=5)
        finally:
            current_request.reset(token)
        await ring.flush()

        async with httpx.AsyncClient(
            transport=transport,
            base_url=_ADMIN,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            recent = await ac.get("/recent?limit=200")
            breakdown = await ac.get("/requests/4242")

    rec_body = recent.json()
    req_ev = next(
        e for e in rec_body if e["kind"] == "request" and e["request_id"] == 4242
    )
    assert req_ev["result_count"] == 13
    assert req_ev["warnings_summary"] == [{"source_key": "comix", "code": "timeout"}]
    assert req_ev["request_blob"]["body"] == {"type": "chapter", "query": "naruto"}

    assert breakdown.status_code == 200
    calls = breakdown.json()["calls"]
    src_ev = next(c for c in calls if c["kind"] == "source-result")
    assert src_ev["result_count"] == 172
    assert src_ev["candidates_enumerated"] == 5


@pytest.mark.asyncio
async def test_response_model_preserves_served_keys(metrics_app: FastAPI) -> None:
    """No byte regression after attaching response_model to the read endpoints.

    Every served event dict's keyset must be a SUPERSET-or-equal of the 18
    MetricEvent field names — i.e. the response_model dropped no key (extra="allow"
    keeps undeclared keys; the declared 18 are always present). Proven through the
    LIVE app against /recent, /failures, /slow, and the /requests/{id} breakdown
    calls[]. If response_model had dropped/renamed a key, this (and the existing
    verbatim-echo assertions in test_read_endpoints_echo_enriched_fields) break.
    """
    import dataclasses

    from manga_gateway.metrics.event import MetricEvent

    event_fields = {f.name for f in dataclasses.fields(MetricEvent)}

    transport = httpx.ASGITransport(app=metrics_app)
    async with metrics_app.router.lifespan_context(metrics_app):
        request_id = await _seed_request(metrics_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_ADMIN,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            recent = (await ac.get("/recent?limit=200")).json()
            failures = (await ac.get("/failures?limit=200")).json()
            slow = (await ac.get("/slow?limit=200")).json()
            breakdown = (await ac.get(f"/requests/{request_id}")).json()

    served_event_lists = [recent, failures, slow, breakdown["calls"]]
    saw_event = False
    for events in served_event_lists:
        for ev in events:
            saw_event = True
            missing = event_fields - set(ev)
            assert not missing, f"served event dropped MetricEvent fields: {missing}"
    assert saw_event, "expected at least one served event to validate keysets"

    # The breakdown envelope keeps its exact documented shape (RequestBreakdownOut).
    assert set(breakdown) == {
        "request_id",
        "surface",
        "endpoint",
        "ts",
        "total_duration_ms",
        "outcome",
        "calls",
    }


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
