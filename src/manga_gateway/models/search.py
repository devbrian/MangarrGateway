"""``POST /search`` + ``GET /recent`` wire DTOs (SRCH-01, REL-01/02/03).

All models extend the shared :class:`~manga_gateway.models.common.ApiModel` base
(single owner of ``populate_by_name=True``) and carry camelCase ``Field(alias=...)``
matching ``manga-gateway.openapi.yaml`` exactly (Pitfall 3 — never re-declare
``model_config``; serialize routes with ``response_model_by_alias=True``).

``Release.chapter_number`` is typed ``Decimal`` so a decimal chapter like ``"1.005"``
survives to >=3 places (SRCH-06 / Pitfall 1) — never round-tripped through a lossy
float intermediate. Structured fields are populated whenever the source supplies them
and are the forward primary path for a future Mangarr (D-19/D-20).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from .common import ApiModel


class SearchRequest(ApiModel):
    """``POST /search`` request body (openapi.yaml SearchRequest; required type)."""

    type: Literal["manga", "chapter"]
    query: str | None = None
    ids: dict[str, Any] | None = None
    chapter: float | None = None
    volume: int | None = None
    languages: list[str] | None = None
    sources: list[str] | None = None
    interactive: bool = False
    limit: int = 50
    offset: int = 0


class Release(ApiModel):
    """A single normalized release — one grabbable chapter upload (REL-01/03, D-21).

    Required wire fields: ``guid``, ``title``, ``sourceKey``, ``downloadHandle``,
    ``publishDate``. The rest are advisory structured fields (D-19) — fully populated
    when known and the intended primary path for a future Mangarr (D-20).
    """

    guid: str
    title: str
    source_key: str = Field(alias="sourceKey")
    download_handle: str = Field(alias="downloadHandle")
    publish_date: str = Field(alias="publishDate")
    info_url: str | None = Field(default=None, alias="infoUrl")
    manga_title: str | None = Field(default=None, alias="mangaTitle")
    # Decimal preserves >=3 places (SRCH-06 / Pitfall 1) — never via float.
    chapter_number: Decimal | None = Field(default=None, alias="chapterNumber")
    volume: int | None = None
    language: str | None = None
    scanlation_group: str | None = Field(default=None, alias="scanlationGroup")
    page_count: int | None = Field(default=None, alias="pageCount")
    size_bytes: int = Field(default=0, alias="sizeBytes")
    ids: dict[str, Any] | None = None


class SourceWarning(ApiModel):
    """A per-source soft failure surfaced alongside results (SRCH-03/04, D-14)."""

    source_key: str = Field(alias="sourceKey")
    code: str
    message: str


class ReleaseListResponse(ApiModel):
    """``/search`` + ``/recent`` response: releases + optional warnings (SRCH-01)."""

    releases: list[Release] = Field(default_factory=list)
    warnings: list[SourceWarning] = Field(default_factory=list)
