"""``GET /version`` — version / health probe (VER-01, operationId getVersion)."""

from __future__ import annotations

from fastapi import APIRouter

from ... import __version__
from ...models.version import VersionResponse

router = APIRouter()


@router.get("/version", response_model=VersionResponse)
async def get_version() -> VersionResponse:
    """Return the gateway version and health status."""
    return VersionResponse(version=__version__, status="ok")
