"""Shared DTO base for every contract model.

``ApiModel`` is the SINGLE owner of the camelCase alias configuration that every
wire model inherits (Pitfall 3). This plan's ``VersionResponse`` and Plan 02's
``Capabilities`` / ``StatusResponse`` / ``SourceCap`` all extend this base via
import; they must never re-declare ``model_config``.

The base enables ``populate_by_name=True`` so models can be constructed with the
snake_case Python field names internally while serializing by their camelCase
``Field(alias=...)`` on the wire (FastAPI serializes ``response_model`` by alias
by default).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    """Base for all contract DTOs (single owner of the alias config)."""

    model_config = ConfigDict(populate_by_name=True)
