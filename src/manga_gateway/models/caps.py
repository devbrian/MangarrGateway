"""``GET /caps`` response models (CAPS-01/02/03, operationId ``getCaps``).

All models extend the shared :class:`~manga_gateway.models.common.ApiModel` base
(single owner of ``populate_by_name=True``) and carry camelCase ``Field(alias=...)``
matching ``manga-gateway.openapi.yaml`` exactly (Pitfall 3 — serialize by alias).

``Capabilities.sources`` defaults to ``[]`` but is populated LIVE per request by the
``GET /caps`` route from the registry + per-source ``SourceHealth`` map (D-05/D-38).
``SourceCap`` carries support for ``enabled=False`` (CAPS-03) and the constrained
``antibot`` enum (CAPS-02).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ApiModel

# Antibot levels per the contract SourceCap.antibot enum (CAPS-02).
AntibotLevel = Literal["none", "cloudflare", "cloudflare+encrypted"]

# Contract example list (Capabilities.supportedSearchParams.example).
_DEFAULT_SEARCH_PARAMS = [
    "q",
    "mangadexId",
    "anilistId",
    "malId",
    "chapter",
    "volume",
    "language",
    "sourceKey",
]

# downloadFormats enum [cbz, cbt, folder]; CBZ is the gateway default delivery.
_DEFAULT_DOWNLOAD_FORMATS = ["cbz", "cbt", "folder"]


class Limits(ApiModel):
    """Pagination limits advertised in ``Capabilities.limits``."""

    default_page_size: int = Field(default=50, alias="defaultPageSize")
    max_page_size: int = Field(default=100, alias="maxPageSize")


class SourceCap(ApiModel):
    """Per-source capability advertisement (CAPS-02/03).

    Supports ``enabled=False`` for the degraded-source signal (CAPS-03), and
    constrains ``antibot`` to the contract enum (CAPS-02).
    """

    key: str
    name: str
    enabled: bool
    supports_search: bool = Field(alias="supportsSearch")
    supports_recent: bool = Field(alias="supportsRecent")
    id_types: list[str] | None = Field(default=None, alias="idTypes")
    languages: list[str] | None = None
    rate_limit_per_minute: int | None = Field(default=None, alias="rateLimitPerMinute")
    antibot: AntibotLevel | None = None


class Capabilities(ApiModel):
    """``GET /caps`` capabilities document (CAPS-01).

    ``sources`` defaults to ``[]``; the route fills it LIVE from the registry +
    ``SourceHealth`` map on every call (D-05/D-38).
    """

    gateway_version: str = Field(alias="gatewayVersion")
    sources: list[SourceCap] = Field(default_factory=list)
    supported_search_params: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_SEARCH_PARAMS),
        alias="supportedSearchParams",
    )
    limits: Limits = Field(default_factory=Limits)
    download_formats: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_DOWNLOAD_FORMATS),
        alias="downloadFormats",
    )
