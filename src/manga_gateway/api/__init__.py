"""API layer: the ``/api/v1`` router aggregating all route modules."""

from __future__ import annotations

from fastapi import APIRouter

from .routes import version

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(version.router)
