"""Recent-feed + capabilities tests (RCNT-01/02, CAPS-01/02/03).

Task 1 (RED first): GET /recent fans out source-agnostically over the registry to
``MangaDexSource.recent`` (built in Plan 01), returning the same ReleaseListResponse
shape as /search, newest-first by ``publishDate``, language-filterable, with ``since``
filtering and per-source isolation. Mocked with respx — no network.

Task 2: GET /caps advertises the live MangaDex SourceCap read from the registry,
served read-through the 12h caps cache.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx

_MANGADEX = "https://api.mangadex.org"


def _chapter(
    *,
    chapter: str,
    chapter_id: str,
    manga_id: str,
    published: str,
    group: str = "Team A",
) -> dict:
    """One MangaDex /chapter entry with reference-expanded manga + group."""
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
            "publishAt": published,
            "readableAt": published,
            "pages": 3,
        },
        "relationships": [
            {"id": "grp", "type": "scanlation_group", "attributes": {"name": group}},
            {
                "id": manga_id,
                "type": "manga",
                "attributes": {"title": {"en": "Solo Leveling"}},
            },
        ],
    }


def _recent_payload(chapters: list[dict]) -> dict:
    return {
        "result": "ok",
        "response": "collection",
        "data": chapters,
        "total": len(chapters),
        "limit": 100,
        "offset": 0,
    }


# ─────────────────────────── GET /recent (RCNT-01) ──────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_recent_returns_releases_newest_first(client: httpx.AsyncClient) -> None:
    manga_id = str(uuid.uuid4())
    # Deliberately out of order: the route must sort newest-first by publishDate.
    chapters = [
        _chapter(
            chapter="10",
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published="2026-05-20T00:00:00+00:00",
        ),
        _chapter(
            chapter="12",
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published="2026-05-29T00:00:00+00:00",
        ),
        _chapter(
            chapter="11",
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published="2026-05-25T00:00:00+00:00",
        ),
    ]
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_recent_payload(chapters))
    )

    resp = await client.get("/recent")
    assert resp.status_code == 200
    body = resp.json()
    releases = body["releases"]
    assert len(releases) == 3
    # Same Release shape as /search — each carries an opaque handle.
    for rel in releases:
        assert rel["sourceKey"] == "mangadex"
        assert rel["downloadHandle"]
        assert rel["guid"]
        assert rel["publishDate"]
    # RCNT-01: newest-first by publishDate (descending).
    dates = [rel["publishDate"] for rel in releases]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == "2026-05-29T00:00:00+00:00"


@respx.mock
@pytest.mark.asyncio
async def test_recent_languages_filter_and_limit_clamp(
    client: httpx.AsyncClient,
) -> None:
    manga_id = str(uuid.uuid4())
    route = respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200,
            json=_recent_payload(
                [
                    _chapter(
                        chapter="1",
                        chapter_id=str(uuid.uuid4()),
                        manga_id=manga_id,
                        published="2026-05-29T00:00:00+00:00",
                    )
                ]
            ),
        )
    )

    resp = await client.get("/recent", params={"languages": "en", "limit": "9999"})
    assert resp.status_code == 200
    assert route.called
    sent = str(route.calls.last.request.url)
    # languages=en → translatedLanguage[]=en upstream.
    assert "translatedLanguage%5B%5D=en" in sent or "translatedLanguage[]=en" in sent
    # limit clamped to <=100 (RCNT-01 DoS guard T-02-06).
    assert "limit=100" in sent


@respx.mock
@pytest.mark.asyncio
async def test_recent_since_filters_older_items(client: httpx.AsyncClient) -> None:
    manga_id = str(uuid.uuid4())
    chapters = [
        _chapter(
            chapter="1",
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published="2026-05-10T00:00:00+00:00",
        ),
        _chapter(
            chapter="2",
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published="2026-05-28T00:00:00+00:00",
        ),
    ]
    route = respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_recent_payload(chapters))
    )

    since = "2026-05-20T00:00:00+00:00"
    resp = await client.get("/recent", params={"since": since})
    assert resp.status_code == 200
    releases = resp.json()["releases"]
    # RCNT-02: only items newer than `since` survive.
    for rel in releases:
        assert rel["publishDate"] > since
    assert all(r["publishDate"] != "2026-05-10T00:00:00+00:00" for r in releases)
    # Either upstream publishAtSince was sent OR the client-side filter applied.
    sent = str(route.calls.last.request.url)
    upstream_sent = "publishAtSince" in sent
    client_filtered = len(releases) == 1
    assert upstream_sent or client_filtered


@respx.mock
@pytest.mark.asyncio
async def test_recent_erroring_source_yields_warning_still_200(
    client: httpx.AsyncClient,
) -> None:
    # SRCH-03 isolation reused: a source erroring during recent → warnings[], not 5xx.
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(500, json={"result": "error"})
    )

    resp = await client.get("/recent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["releases"] == []
    assert len(body["warnings"]) == 1
    assert body["warnings"][0]["sourceKey"] == "mangadex"


# ─────────────────────────── GET /caps (CAPS-01/02/03) ──────────────────────


@pytest.mark.asyncio
async def test_caps_advertises_live_mangadex_source(client: httpx.AsyncClient) -> None:
    resp = await client.get("/caps")
    assert resp.status_code == 200
    body = resp.json()
    sources = body["sources"]
    assert len(sources) == 1
    md = sources[0]
    assert md["key"] == "mangadex"
    assert md["antibot"] == "none"
    assert md["enabled"] is True
    assert "mangadexId" in md["idTypes"]
    assert md["rateLimitPerMinute"] > 0
    assert md["languages"]  # non-empty


@pytest.mark.asyncio
async def test_caps_served_read_through_cache(
    client: httpx.AsyncClient, app
) -> None:
    # First call populates the 12h caps cache; second returns the same cached object.
    first = await client.get("/caps")
    assert first.status_code == 200
    cached = app.state.caps_cache.get("caps")
    assert cached is not None
    second = await client.get("/caps")
    assert second.json() == first.json()
    # Same object instance is served within TTL (read-through, built once).
    assert app.state.caps_cache.get("caps") is cached
