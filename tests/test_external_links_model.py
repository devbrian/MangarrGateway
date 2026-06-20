"""Serialization unit tests for ``ExternalLinks`` + ``Release.external_links`` (D-07).

Proves the exclude-none wire shape: only populated tracker keys reach the wire
(never null-valued keys), a zero-link release emits ``externalLinks`` as null/absent
(never a 10-null object), and every canonical camelCase alias round-trips.
"""

from __future__ import annotations

import json

from manga_gateway.models.search import ExternalLinks, Release

_CANONICAL_ALIASES = [
    "anilist",
    "myAnimeList",
    "mangaUpdates",
    "mangaBaka",
    "kitsu",
    "animePlanet",
    "bookwalker",
    "ann",
    "kenmei",
    "mangaDex",
]


def _release(**kwargs: object) -> Release:
    return Release(
        guid="g1",
        title="Series Ch. 1",
        sourceKey="mangadex",  # type: ignore[call-arg]
        downloadHandle="h1",  # type: ignore[call-arg]
        publishDate="2026-06-19T00:00:00Z",  # type: ignore[call-arg]
        **kwargs,
    )


def test_release_emits_only_populated_keys() -> None:
    """Test 1: a 2-tracker release serializes exactly those 2 keys, no nulls."""
    rel = _release(
        external_links=ExternalLinks(anilist="105398", myAnimeList="121496")  # type: ignore[call-arg]
    )
    wire = json.loads(rel.model_dump_json(by_alias=True))
    assert wire["externalLinks"] == {"anilist": "105398", "myAnimeList": "121496"}


def test_release_without_links_emits_null() -> None:
    """Test 2: a None external_links emits null/absent externalLinks (no 10 nulls)."""
    rel = _release(external_links=None)
    wire = json.loads(rel.model_dump_json(by_alias=True))
    assert wire.get("externalLinks") is None


def test_external_links_dump_uses_camelcase_alias() -> None:
    """Test 3: a single-field model dumps to its camelCase alias key only."""
    assert ExternalLinks(mangaUpdates="6z1uqw7").model_dump(by_alias=True) == {  # type: ignore[call-arg]
        "mangaUpdates": "6z1uqw7"
    }


def test_every_canonical_alias_round_trips() -> None:
    """Test 4: all 10 canonical aliases are constructible and round-trip by alias."""
    for alias in _CANONICAL_ALIASES:
        model = ExternalLinks(**{alias: "x"})  # type: ignore[arg-type]
        assert model.model_dump(by_alias=True) == {alias: "x"}
