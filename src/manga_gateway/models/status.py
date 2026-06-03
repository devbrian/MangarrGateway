"""``GET /status`` response model (STAT-01, operationId ``getStatus``).

Extends the shared :class:`~manga_gateway.models.common.ApiModel` base and carries
camelCase ``Field(alias=...)`` matching ``manga-gateway.openapi.yaml``.

Phase-1 stance: the body is contract-shaped with non-sensitive placeholders. Real
``isLocalhost`` is computed from the bind host; ``outputRootFolders`` reports the
configured output root; ``maxConcurrentChapters`` is a placeholder per RESEARCH A6.
"""

from __future__ import annotations

from pydantic import Field

from .common import ApiModel


class StatusCapabilities(ApiModel):
    """``StatusResponse.capabilities`` sub-object (pause/formats/concurrency)."""

    pause: bool = False  # PAUSE-01 is a v2 requirement — not supported in v1.
    output_formats: list[str] = Field(
        default_factory=lambda: ["cbz", "cbt", "folder"],
        alias="outputFormats",
    )
    # Schema default mirrors the config default (D-30, raised 3->8); the /status
    # route always overrides it with the live settings value, so this is only the
    # advertised OpenAPI default.
    max_concurrent_chapters: int = Field(default=8, alias="maxConcurrentChapters")


class StatusResponse(ApiModel):
    """``GET /status`` response (STAT-01)."""

    is_localhost: bool = Field(alias="isLocalhost")
    removes_completed_downloads: bool = Field(
        default=False, alias="removesCompletedDownloads"
    )
    output_root_folders: list[str] = Field(alias="outputRootFolders")
    version: str
    capabilities: StatusCapabilities = Field(default_factory=StatusCapabilities)
