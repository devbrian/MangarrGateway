"""Contract JSON error model + exception handlers (ERR-01).

The wire shape is ``{"error": {"code": <enum>, "message": <str>}}`` where code is
one of [auth, rate_limited, source_unavailable, bad_request, not_found, internal].
A 429 carries a ``Retry-After`` header.

Pitfall 5: the catch-all ``Exception`` handler must NOT swallow Starlette's
``HTTPException`` / FastAPI validation. We register specific handlers for our
contract-shaped paths so a genuine 404 stays a 404 (now wrapped in the contract
Error envelope, code ``not_found`` — issue #2), never ``code: "internal"``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

ErrorCode = Literal[
    "auth",
    "rate_limited",
    "source_unavailable",
    "bad_request",
    "not_found",
    "internal",
]


class ErrorBody(BaseModel):
    """The nested ``error`` object."""

    code: ErrorCode
    message: str


class Error(BaseModel):
    """The contract ``Error`` envelope: ``{"error": {...}}``."""

    error: ErrorBody


class AuthError(Exception):
    """Missing or invalid API key -> 401 code ``auth`` (AUTH-01, D-02)."""


class RateLimited(Exception):
    """Too many requests -> 429 code ``rate_limited`` with ``Retry-After``."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("rate_limited")


def _error(code: ErrorCode, message: str) -> dict[str, dict[str, str]]:
    return Error(error=ErrorBody(code=code, message=message)).model_dump()


def register_error_handlers(app: FastAPI) -> None:
    """Map our exceptions onto the contract ``Error`` schema."""

    @app.exception_handler(AuthError)
    async def _auth(_: Request, __: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=_error("auth", "Missing or invalid API key"),
        )

    @app.exception_handler(RequestValidationError)
    async def _bad(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error("bad_request", "Malformed request"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # A 400 from a body-parse failure (FastAPI raises a 400 HTTPException, not a
        # RequestValidationError, when the body is undecodable) must still serialize
        # the contract Error envelope (CTRT-01 / T-02-05) — never Starlette's bare
        # {"detail": ...}. 404s are wrapped in the same Error envelope so the
        # contract's /downloads/{jobId} 404 NotFound response (issue #2) matches the
        # wire shape; other statuses keep the default JSON detail shape (Pitfall 5).
        if exc.status_code == 400:
            return JSONResponse(
                status_code=400,
                content=_error("bad_request", "Malformed request"),
                headers=getattr(exc, "headers", None),
            )
        if exc.status_code == 404:
            message = str(exc.detail) if exc.detail else "Not found"
            return JSONResponse(
                status_code=404,
                content=_error("not_found", message),
                headers=getattr(exc, "headers", None),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RateLimited)
    async def _rate_limited(_: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content=_error("rate_limited", "Too many requests"),
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(Exception)
    async def _internal(_: Request, __: Exception) -> JSONResponse:
        # Generic message only — never echo stack/exception detail (V7/T-01-07).
        return JSONResponse(
            status_code=500,
            content=_error("internal", "Internal error"),
        )
