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
from datetime import datetime

import httpx
import pytest
import respx

from manga_gateway.api.routes.recent import _split_multi

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

    # Scope to mangadex so the multi-source fan-out (comix added in 04-03) does not
    # make unmocked calls; this test asserts MangaDex's recent parsing specifically.
    resp = await client.get("/recent", params={"sources": "mangadex"})
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
async def test_recent_sorts_by_instant_across_mixed_offsets(
    client: httpx.AsyncClient,
) -> None:
    """WR-02 regression: newest-first compares true instants, not ISO strings.

    A lexicographic string sort would order ``+09:00`` ahead of a later ``+00:00``
    timestamp because the date substring compares before the offset suffix. Parsing
    to aware datetimes orders them by actual instant.
    """
    manga_id = str(uuid.uuid4())
    # X: EARLIER instant (2026-05-28T15:00Z) but a LATER local date string.
    x_local = "2026-05-29T00:00:00+09:00"
    # Y: LATER instant (2026-05-28T20:00Z) but an earlier date string.
    y_utc = "2026-05-28T20:00:00+00:00"
    chapters = [
        _chapter(
            chapter="10",
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published=x_local,
        ),
        _chapter(
            chapter="11",
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published=y_utc,
        ),
    ]
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_recent_payload(chapters))
    )

    resp = await client.get("/recent", params={"sources": "mangadex"})
    assert resp.status_code == 200
    releases = resp.json()["releases"]
    # True newest-first: Y (20:00Z) precedes X (15:00Z) — opposite of a string sort.
    assert releases[0]["publishDate"] == y_utc
    assert releases[1]["publishDate"] == x_local


def test_split_multi_accepts_repeated_csv_and_mixed() -> None:
    """The /recent source/language parser flattens repeated params AND CSV."""
    assert _split_multi(None) is None
    assert _split_multi([]) is None
    assert _split_multi([""]) is None
    assert _split_multi(["mangadex"]) == ["mangadex"]
    # Repeated params (?sources=a&sources=b&sources=c) — the Mangarr form.
    assert _split_multi(["mangadex", "comix", "mangaball"]) == [
        "mangadex",
        "comix",
        "mangaball",
    ]
    # Legacy single CSV value (?sources=a,b,c).
    assert _split_multi(["mangadex,comix,mangaball"]) == [
        "mangadex",
        "comix",
        "mangaball",
    ]
    # Mixed + surrounding whitespace + empty fragments are all normalised.
    assert _split_multi(["mangadex, comix", "", " mangaball "]) == [
        "mangadex",
        "comix",
        "mangaball",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_recent_repeated_source_params_select_all_sources(
    client: httpx.AsyncClient,
) -> None:
    """Regression (prod): Mangarr sends ``?sources=a&sources=b`` (repeated params),
    not CSV. The route must fan out to BOTH, not silently keep only the last value
    (which dropped every source but the last in prod).

    mangadex (mocked) yields a release AND mangaball (stubbed 403) yields a warning
    — both appearing proves the fan-out hit both selected sources, not just one.
    """
    manga_id = str(uuid.uuid4())
    respx.get(f"{_MANGADEX}/chapter").mock(
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
    # MangaBall (antibot=none, csrf-bootstrap) → fast permanent 403 → a per-source
    # warnings[] entry (no browser, no retry) — mirrors the contract harness stub.
    respx.route(host="mangaball.net").mock(return_value=httpx.Response(403))

    # httpx encodes a list value as repeated params: ?sources=mangadex&sources=mangaball
    resp = await client.get(
        "/recent", params={"sources": ["mangadex", "mangaball"], "limit": "50"}
    )
    assert resp.status_code == 200
    body = resp.json()
    # mangadex was queried (its release is present) — NOT dropped as a non-last value.
    assert any(r["sourceKey"] == "mangadex" for r in body["releases"])
    # mangaball was ALSO queried (it failed → warning) — both sources fanned out.
    assert any(w["sourceKey"] == "mangaball" for w in body["warnings"])


@respx.mock
@pytest.mark.asyncio
async def test_recent_csv_source_param_still_works(client: httpx.AsyncClient) -> None:
    """Backward-compat: the legacy single CSV value (?sources=a,b) still works."""
    manga_id = str(uuid.uuid4())
    respx.get(f"{_MANGADEX}/chapter").mock(
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
    respx.route(host="mangaball.net").mock(return_value=httpx.Response(403))

    resp = await client.get("/recent", params={"sources": "mangadex,mangaball"})
    assert resp.status_code == 200
    body = resp.json()
    assert any(r["sourceKey"] == "mangadex" for r in body["releases"])
    assert any(w["sourceKey"] == "mangaball" for w in body["warnings"])


@respx.mock
@pytest.mark.asyncio
async def test_recent_truncates_merged_to_limit(client: httpx.AsyncClient) -> None:
    # T-02-06 (#2): the merged feed is truncated to the requested limit, even when
    # the upstream returns more rows than requested.
    manga_id = str(uuid.uuid4())
    chapters = [
        _chapter(
            chapter=str(i),
            chapter_id=str(uuid.uuid4()),
            manga_id=manga_id,
            published=f"2026-05-2{i}T00:00:00+00:00",
        )
        for i in range(3)
    ]
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_recent_payload(chapters))
    )

    resp = await client.get("/recent", params={"limit": "2", "sources": "mangadex"})
    assert resp.status_code == 200
    assert len(resp.json()["releases"]) == 2


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

    resp = await client.get(
        "/recent", params={"languages": "en", "limit": "9999", "sources": "mangadex"}
    )
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
    resp = await client.get("/recent", params={"since": since, "sources": "mangadex"})
    assert resp.status_code == 200
    releases = resp.json()["releases"]
    # RCNT-02: only items newer than `since` survive — compare instants, not ISO
    # strings (lexical compare misorders mixed Z/+00:00 offsets).
    since_dt = datetime.fromisoformat(since)
    for rel in releases:
        assert datetime.fromisoformat(rel["publishDate"]) > since_dt
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

    resp = await client.get("/recent", params={"sources": "mangadex"})
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
    # The live registry now advertises MangaDex AND Comix (04-03) — assert MangaDex's
    # SourceCap shape specifically rather than the full source count.
    md = next((s for s in sources if s["key"] == "mangadex"), None)
    assert md is not None
    assert md["antibot"] == "none"
    assert md["enabled"] is True
    assert "mangadexId" in md["idTypes"]
    assert md["rateLimitPerMinute"] > 0
    assert md["languages"]  # non-empty


@pytest.mark.asyncio
async def test_caps_advertises_mangaball_antibot_cloudflare(
    client: httpx.AsyncClient,
) -> None:
    # 07-03: registering MangaBallSource auto-advertises it via registry.caps()
    # (CAPS-02). Since the 2026-06-15 site-wide managed-challenge escalation (debug
    # mangaball-cloudflare-csrf-243) MangaBall is antibot "cloudflare" (was "none").
    # Deterministic gate: CloudflareSolver.warm is the conftest no-op, so mangaball is
    # NOT force_disabled and stays advertised enabled.
    resp = await client.get("/caps")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    mb = next((s for s in sources if s["key"] == "mangaball"), None)
    assert mb is not None, "mangaball not advertised in /caps"
    assert mb["antibot"] == "cloudflare"
    assert mb["enabled"] is True
    assert mb["rateLimitPerMinute"] > 0
    assert mb["languages"]  # non-empty ALL_LANGUAGES set
    assert mb["supportsSearch"] is True
    assert mb["supportsRecent"] is True


@pytest.mark.asyncio
async def test_caps_served_read_through_cache(client: httpx.AsyncClient, app) -> None:
    # First call populates the 12h STATIC skeleton cache (D-38: the per-source
    # sources[] is rebuilt live each call so a breaker trip is never masked by the
    # cache, but the version/limits/formats skeleton stays cached).
    first = await client.get("/caps")
    assert first.status_code == 200
    cached = app.state.caps_cache.get("caps_skeleton")
    assert cached is not None  # skeleton cached after the first poll
    second = await client.get("/caps")
    # The full document (skeleton + live sources) is byte-stable across polls when
    # no breaker trips, even though sources[] is recomputed each call.
    assert second.json() == first.json()
    # Same cached SKELETON instance is reused within TTL (the dynamic sources[] is
    # layered on via model_copy, not re-cached).
    assert app.state.caps_cache.get("caps_skeleton") is cached
