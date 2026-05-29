"""``GET /version`` response model (VER-01).

Contract: ``{version: string, status: enum[ok, degraded]}`` — both required.
"""

from __future__ import annotations

from typing import Literal

from .common import ApiModel


class VersionResponse(ApiModel):
    """Version / health probe response (operationId ``getVersion``)."""

    version: str
    status: Literal["ok", "degraded"]
