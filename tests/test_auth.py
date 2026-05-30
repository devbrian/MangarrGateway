"""Global API-key auth + error-model + singleton-seam tests (Task 2).

Covers AUTH-01/D-02 (header OR query, 401 contract body), the lifespan
singleton identity (R1/PLAT-02), the contract error model (ERR-01: 429 +
Retry-After, catch-all does not swallow 404), and the filename sanitizer (D-08).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.errors import RateLimited
from manga_gateway.util.sanitize import sanitize_filename

from .conftest import BASE_URL, TEST_API_KEY


def _client(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers=headers or {},
    )


async def test_no_key_returns_401_contract_body(app: FastAPI) -> None:
    async with _client(app) as ac:
        resp = await ac.get("/version")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "auth"
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


async def test_valid_header_key_passes(app: FastAPI) -> None:
    async with _client(app, {"X-Api-Key": TEST_API_KEY}) as ac:
        resp = await ac.get("/version")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_valid_query_key_passes(app: FastAPI) -> None:
    async with _client(app) as ac:
        resp = await ac.get("/version", params={"apikey": TEST_API_KEY})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_wrong_key_returns_401_auth(app: FastAPI) -> None:
    async with _client(app, {"X-Api-Key": "wrong-key"}) as ac:
        resp = await ac.get("/version")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "auth"


async def test_non_ascii_key_returns_401_not_500(app: FastAPI) -> None:
    """A non-ASCII key is rejected as 401, never crashed to 500 (CR-01).

    HTTP header values arrive latin-1-decoded, so a byte 0x80-0xFF yields a
    non-ASCII str. secrets.compare_digest raises TypeError on a non-ASCII str;
    encoding both operands to UTF-8 bytes first keeps the auth path returning
    the contract 401 instead of falling through to the catch-all 500.

    The key is sent as raw bytes: httpx encodes outgoing header values as ASCII
    and would reject a non-ASCII str, but the byte 0xFF on the wire is exactly
    what the ASGI layer decodes to the non-ASCII str that hits compare_digest.
    """
    async with _client(app) as ac:
        resp = await ac.get("/version", headers={"X-Api-Key": b"\xff-not-the-key"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "auth"


async def test_singleton_seams_built_once(app: FastAPI) -> None:
    """The same seam object is reused across two requests (R1/PLAT-02).

    httpx ASGITransport does not emit the ASGI lifespan scope, so we drive the
    lifespan explicitly to build the seams, then assert identity across requests.
    """
    async with app.router.lifespan_context(app):
        async with _client(app, {"X-Api-Key": TEST_API_KEY}) as ac:
            await ac.get("/version")
            first = app.state.session
            await ac.get("/version")
            second = app.state.session
        assert first is second
        # All six seams present on app.state after startup.
        for attr in (
            "transport",
            "session",
            "solver",
            "ratelimiter",
            "registry",
            "caps_cache",
        ):
            assert hasattr(app.state, attr)


async def test_catch_all_does_not_swallow_404(client: httpx.AsyncClient) -> None:
    """A genuine 404 stays a 404, wrapped in the contract Error envelope with
    code ``not_found`` (issue #2) — never ``code: 'internal'`` (Pitfall 5)."""
    resp = await client.get("/no-such-route")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_ratelimited_handler_sets_retry_after() -> None:
    """RateLimited -> 429 with a Retry-After header carrying the seconds."""
    app = create_app(Settings(api_key=TEST_API_KEY))

    @app.get("/api/v1/_boom_rl")
    async def _boom() -> None:
        raise RateLimited(retry_after=42)

    async with _client(app, {"X-Api-Key": TEST_API_KEY}) as ac:
        resp = await ac.get("/_boom_rl")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "42"
    assert resp.json()["error"]["code"] == "rate_limited"


async def test_internal_handler_maps_unhandled_to_500() -> None:
    app = create_app(Settings(api_key=TEST_API_KEY))

    @app.get("/api/v1/_boom")
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    # surface app exceptions as responses (our handler), not re-raises
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=BASE_URL,
        headers={"X-Api-Key": TEST_API_KEY},
    ) as ac:
        resp = await ac.get("/_boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal"
    # Generic message — never leaks the exception detail (T-01-07).
    assert "kaboom" not in resp.text


@pytest.mark.parametrize(
    ("raw", "expected_no_traversal"),
    [
        ("../../etc/passwd", True),
        ("..\\..\\windows\\system32", True),
        ("Chainsaw Man Ch. 152", True),
        ("....", True),
    ],
)
def test_sanitize_rejects_traversal(raw: str, expected_no_traversal: bool) -> None:
    out = sanitize_filename(raw)
    assert "/" not in out
    assert "\\" not in out
    assert ".." not in out
    assert out  # never empty
