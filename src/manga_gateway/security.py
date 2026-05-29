"""Global API-key authentication (AUTH-01, D-02).

The key is accepted via the ``X-Api-Key`` header OR the ``?apikey=`` query
param. Comparison is constant-time (``secrets.compare_digest``, never ``==``;
T-01-02). Applied on the app via ``FastAPI(dependencies=[Depends(require_api_key)])``
so EVERY route is covered — schemathesis then proves 401 across all ops.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

from .errors import AuthError

# auto_error=False: we raise the contract AuthError ourselves (not FastAPI's 403).
_header_scheme = APIKeyHeader(name="X-Api-Key", auto_error=False)
_query_scheme = APIKeyQuery(name="apikey", auto_error=False)


async def require_api_key(
    request: Request,
    header_key: str | None = Security(_header_scheme),
    query_key: str | None = Security(_query_scheme),
) -> None:
    """Reject the request unless a valid key is supplied (header or query)."""
    supplied = header_key or query_key
    expected: str = request.app.state.settings.api_key
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise AuthError
