"""Search-slice tests: framework fan-out isolation, handle store, DTO aliasing,
decimal-chapter fidelity (Task 2) + the end-to-end POST /search path (Task 3).

Task 2 (this file's first commit) is RED: the framework/handles/search modules do
not exist yet, so the unit-level fan-out/store/DTO/decimal tests fail on import.
The E2E POST /search tests are marked xfail until Task 3 wires the route, then
flipped active.

Covers: SRCH-03/04 (isolation vs 0-results), HDL-01/02 (opaque handle, no baseUrl),
REL-01/02 (DTO aliasing), SRCH-06 (decimal >=3 places), and (Task 3) D-18/D-21/D-22.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
import respx

from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.fanout import fan_out
from manga_gateway.handles.store import HandleStore, ResolutionRecord
from manga_gateway.models.search import Release

# ─────────────────────────── DTO aliasing (REL-01/02) ───────────────────────────


def test_dto_release_serializes_by_camelcase_alias() -> None:
    rel = Release(
        guid="mangadex:m:ch-1:en:c",
        title="Solo Leveling - Chapter 1 (en)",
        source_key="mangadex",
        download_handle="opaque-token",
        publish_date="2026-05-29T13:57:18+00:00",
        manga_title="Solo Leveling",
        scanlation_group="Team Lumikha",
        page_count=42,
    )
    dumped = rel.model_dump(by_alias=True)
    assert dumped["sourceKey"] == "mangadex"
    assert dumped["downloadHandle"] == "opaque-token"
    assert dumped["publishDate"] == "2026-05-29T13:57:18+00:00"
    assert dumped["mangaTitle"] == "Solo Leveling"
    assert dumped["scanlationGroup"] == "Team Lumikha"
    assert dumped["pageCount"] == 42


def test_dto_decimal_chapter_survives_three_places() -> None:
    # SRCH-06 / Pitfall 1: a decimal chapter must survive to >=3 places.
    rel = Release(
        guid="g",
        title="X - Chapter 1.005 (en)",
        source_key="mangadex",
        download_handle="h",
        publish_date="2026-05-29T13:57:18+00:00",
        chapter_number=Decimal("1.005"),
    )
    dumped = rel.model_dump(by_alias=True)
    assert Decimal(str(dumped["chapterNumber"])) == Decimal("1.005")
    assert str(dumped["chapterNumber"]).count("5") >= 1
    # 3 decimal places preserved (not truncated to 1.0 / 1.01).
    assert Decimal(str(dumped["chapterNumber"])) != Decimal("1.0")


# ─────────────────────────── Handle store (HDL-01/02) ───────────────────────────


def _record() -> ResolutionRecord:
    return ResolutionRecord(
        source_key="mangadex",
        chapter_id=str(uuid.uuid4()),
        language="en",
        title="Solo Leveling - Chapter 1 (en)",
        manga_title="Solo Leveling",
        chapter_number=Decimal("1"),
        volume=None,
        scanlation_group="Team Lumikha",
        page_count=42,
    )


def test_store_mint_then_resolve_roundtrips() -> None:
    store = HandleStore()
    record = _record()
    handle = store.mint(record)
    assert isinstance(handle, str)
    assert handle  # non-empty opaque token
    assert store.resolve(handle) == record


def test_store_resolve_unknown_returns_none() -> None:
    store = HandleStore()
    assert store.resolve("nonexistent") is None


def test_store_mints_are_opaque_and_distinct() -> None:
    store = HandleStore()
    h1 = store.mint(_record())
    h2 = store.mint(_record())
    assert h1 != h2  # CSPRNG, zero structure


def test_resolution_record_has_no_volatile_token_fields() -> None:
    # HDL-01 / Pitfall 6: NEVER store the at-home baseUrl / cookies.
    record = _record()
    for forbidden in ("base_url", "cookies", "at_home", "baseUrl"):
        assert not hasattr(record, forbidden)


# ─────────────────────────── Fan-out isolation (SRCH-03/04) ─────────────────────


class _FakeSource:
    def __init__(self, key: str) -> None:
        self.key = key


@pytest.mark.asyncio
async def test_fanout_isolates_one_failing_source() -> None:
    ok = _FakeSource("ok")
    bad = _FakeSource("bad")

    one_release = [
        Release(
            guid="g",
            title="t",
            source_key="ok",
            download_handle="h",
            publish_date="2026-05-29T13:57:18+00:00",
        )
    ]

    async def run_one(src: _FakeSource) -> list[Release]:
        if src.key == "bad":
            raise SourceError("source_unavailable", "boom")
        return one_release

    releases, warnings = await fan_out([ok, bad], run_one)
    assert len(releases) == 1
    assert len(warnings) == 1
    code = warnings[0][1] if isinstance(warnings[0], tuple) else warnings[0].code
    assert code == "source_unavailable"


@pytest.mark.asyncio
async def test_fanout_empty_success_yields_no_warning() -> None:
    # SRCH-04: a source returning [] is "0 results", NOT an error — no warning.
    src = _FakeSource("empty")

    async def run_one(_: _FakeSource) -> list[Release]:
        return []

    releases, warnings = await fan_out([src], run_one)
    assert releases == []
    assert warnings == []


@pytest.mark.asyncio
async def test_fanout_timeout_maps_to_warning() -> None:
    import asyncio

    src = _FakeSource("slow")

    async def run_one(_: _FakeSource) -> list[Release]:
        await asyncio.sleep(5)
        return []

    releases, warnings = await fan_out([src], run_one, per_source_timeout=0.05)
    assert releases == []
    assert len(warnings) == 1


# ─────────────────────────── E2E POST /search (Task 3) ──────────────────────────

_MANGADEX = "https://api.mangadex.org"


def _manga_search_payload(manga_id: str) -> dict:
    return {
        "result": "ok",
        "response": "collection",
        "data": [
            {
                "id": manga_id,
                "type": "manga",
                "attributes": {
                    "title": {"en": "Solo Leveling"},
                    "altTitles": [],
                    "availableTranslatedLanguages": ["en"],
                },
                "relationships": [],
            }
        ],
        "total": 1,
        "limit": 1,
        "offset": 0,
    }


def _chapter_feed_payload(manga_id: str) -> dict:
    def _ch(chapter: str, chapter_id: str, group: str) -> dict:
        return {
            "id": chapter_id,
            "type": "chapter",
            "attributes": {
                "volume": None,
                "chapter": chapter,
                "title": None,
                "translatedLanguage": "en",
                "externalUrl": None,
                "isUnavailable": False,
                "publishAt": "2026-05-29T13:57:18+00:00",
                "readableAt": "2026-05-29T13:57:18+00:00",
                "pages": 2,
            },
            "relationships": [
                {
                    "id": "grp",
                    "type": "scanlation_group",
                    "attributes": {"name": group},
                },
                {
                    "id": manga_id,
                    "type": "manga",
                    "attributes": {"title": {"en": "Solo Leveling"}},
                },
            ],
        }

    return {
        "result": "ok",
        "response": "collection",
        "data": [
            _ch("1.005", str(uuid.uuid4()), "Team A"),
            _ch("1.005", str(uuid.uuid4()), "Team B"),
        ],
        "total": 2,
        "limit": 100,
        "offset": 0,
    }


@respx.mock
@pytest.mark.asyncio
async def test_search_title_returns_releases(client: httpx.AsyncClient) -> None:
    manga_id = str(uuid.uuid4())
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_chapter_feed_payload(manga_id))
    )

    resp = await client.post("/search", json={"type": "chapter", "query": "Solo Leveling"})
    assert resp.status_code == 200
    body = resp.json()
    releases = body["releases"]
    assert releases
    first = releases[0]
    assert first["sourceKey"] == "mangadex"
    assert first["downloadHandle"]
    assert first["guid"]
    assert first["title"]
    assert first["publishDate"]
    # SRCH-06: decimal survives to >=3 places, and appears in the title (D-18).
    assert Decimal(str(first["chapterNumber"])) == Decimal("1.005")
    assert "1.005" in first["title"]
    # D-21: two group uploads of the same chapter → two distinct guids.
    guids = {r["guid"] for r in releases}
    assert len(guids) == len(releases)
    assert len(releases) == 2


@respx.mock
@pytest.mark.asyncio
async def test_search_by_id_hits_direct_lookup(client: httpx.AsyncClient) -> None:
    manga_id = str(uuid.uuid4())
    direct = respx.get(f"{_MANGADEX}/manga/{manga_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": "ok",
                "response": "entity",
                "data": _manga_search_payload(manga_id)["data"][0],
            },
        )
    )
    title_search = respx.get(f"{_MANGADEX}/manga", params={"title": "Solo Leveling"})
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_chapter_feed_payload(manga_id))
    )

    resp = await client.post(
        "/search", json={"type": "manga", "ids": {"mangadexId": manga_id}}
    )
    assert resp.status_code == 200
    assert direct.called  # SRCH-07/D-22: direct /manga/{id} lookup
    assert not title_search.called  # NOT the title search path


@pytest.mark.asyncio
async def test_search_no_query_no_id_returns_400(client: httpx.AsyncClient) -> None:
    # SRCH-05 / D-23: neither query nor a usable id → 400 bad_request.
    resp = await client.post("/search", json={"type": "manga"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


@respx.mock
@pytest.mark.asyncio
async def test_search_minted_handle_resolves_in_store(
    client: httpx.AsyncClient, app
) -> None:
    manga_id = str(uuid.uuid4())
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_chapter_feed_payload(manga_id))
    )

    resp = await client.post("/search", json={"type": "chapter", "query": "Solo Leveling"})
    handle = resp.json()["releases"][0]["downloadHandle"]
    store = app.state.handle_store
    record = store.resolve(handle)
    assert record is not None
    # chapter_id is a UUID and there is no baseUrl (HDL-01).
    uuid.UUID(record.chapter_id)
    assert not hasattr(record, "base_url")
