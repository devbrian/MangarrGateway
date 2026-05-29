"""API layer: the ``/api/v1`` router aggregating all route modules."""

from __future__ import annotations

from fastapi import APIRouter

from .routes import caps, downloads, recent, search, status, version

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(version.router)
api_router.include_router(status.router)
api_router.include_router(caps.router)
api_router.include_router(search.router)
api_router.include_router(recent.router)
api_router.include_router(downloads.router)
