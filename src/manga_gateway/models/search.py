"""``POST /search`` + ``GET /recent`` wire DTOs (SRCH-01, REL-01/02/03).

All models extend the shared :class:`~manga_gateway.models.common.ApiModel` base
(single owner of ``populate_by_name=True``) and carry camelCase ``Field(alias=...)``
matching ``manga-gateway.openapi.yaml`` exactly (Pitfall 3 — never re-declare
``model_config``; serialize routes with ``response_model_by_alias=True``).

``Release.chapter_number`` is typed ``Decimal`` so a decimal chapter like ``"1.005"``
survives to >=3 places (SRCH-06 / Pitfall 1) — never round-tripped through a lossy
float intermediate. Structured fields are populated whenever the source supplies them
and are the forward primary path for a future Mangarr (D-19/D-20).

``ExternalLinks`` carries the D-07 exclude-none behavior on its own
``@model_serializer`` (not a route-level ``exclude_none``) because the ``/search``
route serializes with no ``exclude_none`` — so unset tracker keys are dropped on the
wire by the sub-model itself, never emitted as an object of 10 nulls.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_serializer, model_serializer

from .common import ApiModel


class ExternalLinks(ApiModel):
    """Series-level cross-reference tracker IDs (D-04/D-05/D-06/D-07).

    Ten frozen canonical fields, each an optional bare ID/slug string (never a URL).
    The ``@model_serializer`` emits ONLY populated keys by their camelCase alias, so a
    series with two trackers serializes to exactly two keys (D-07, Pitfall 1).
    """

    anilist: str | None = Field(default=None, alias="anilist")
    my_anime_list: str | None = Field(default=None, alias="myAnimeList")
    manga_updates: str | None = Field(default=None, alias="mangaUpdates")
    manga_baka: str | None = Field(default=None, alias="mangaBaka")
    kitsu: str | None = Field(default=None, alias="kitsu")
    anime_planet: str | None = Field(default=None, alias="animePlanet")
    bookwalker: str | None = Field(default=None, alias="bookwalker")
    ann: str | None = Field(default=None, alias="ann")
    kenmei: str | None = Field(default=None, alias="kenmei")
    manga_dex: str | None = Field(default=None, alias="mangaDex")

    @model_serializer
    def _serialize(self) -> dict[str, str]:
        """Emit only populated keys, by alias (D-07 — drop unset keys on the wire)."""
        return {
            field.alias or name: value
            for name, field in type(self).model_fields.items()
            if (value := getattr(self, name)) is not None
        }


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
    # Display-only advisory field (REL-03/D-19): per-release popularity count
    # populated when the source exposes a like/vote/view signal (e.g. Comix
    # chapter likes, MangaBall views), null otherwise. Never part of the minted
    # ResolutionRecord / download handle — it has no resolution/download role.
    votes: int | None = Field(default=None, alias="votes")
    size_bytes: int = Field(default=0, alias="sizeBytes")
    ids: dict[str, Any] | None = None
    external_links: ExternalLinks | None = Field(default=None, alias="externalLinks")

    @field_serializer("chapter_number", when_used="json")
    def _serialize_chapter_number(self, value: Decimal | None) -> float | None:
        """Emit a JSON ``number`` on the wire (contract ``type: number``) while the
        in-memory model keeps the lossless ``Decimal`` (SRCH-06). ``float`` of a
        <=3-place decimal round-trips exactly to the required precision.
        """
        return float(value) if value is not None else None


class SourceWarning(ApiModel):
    """A per-source soft failure surfaced alongside results (SRCH-03/04, D-14)."""

    source_key: str = Field(alias="sourceKey")
    code: str
    message: str


class ReleaseListResponse(ApiModel):
    """``/search`` + ``/recent`` response: releases + optional warnings (SRCH-01)."""

    releases: list[Release] = Field(default_factory=list)
    warnings: list[SourceWarning] = Field(default_factory=list)
