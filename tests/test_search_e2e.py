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
from collections.abc import Callable
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


def test_dto_release_votes_serializes_by_alias() -> None:
    # REL-03 / D-19: votes is a display-only advisory field, populated when the
    # source exposes a like/vote/view count, null otherwise.
    with_votes = Release(
        guid="g",
        title="X - Chapter 1 (en)",
        source_key="comix",
        download_handle="h",
        publish_date="2026-05-29T13:57:18+00:00",
        votes=27,
    )
    assert with_votes.model_dump(by_alias=True)["votes"] == 27

    without_votes = Release(
        guid="g",
        title="X - Chapter 1 (en)",
        source_key="mangadex",
        download_handle="h",
        publish_date="2026-05-29T13:57:18+00:00",
    )
    assert without_votes.model_dump(by_alias=True)["votes"] is None


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


@pytest.mark.asyncio
async def test_fanout_persistent_5xx_maps_to_typed_upstream_warning() -> None:
    # #152: a persistent upstream 5xx (e.g. mangadot's 503 "Under Maintenance")
    # exhausts the retry budget and reaches fan_out as a raw httpx.HTTPStatusError,
    # NOT a typed SourceError. It must surface the status ("upstream 503"), never the
    # opaque "unexpected error" the generic `except Exception` would have produced.
    src = _FakeSource("down")

    async def run_one(_: _FakeSource) -> list[Release]:
        request = httpx.Request("GET", "https://mangadot.net/api/search")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("maintenance", request=request, response=response)

    releases, warnings = await fan_out([src], run_one)
    assert releases == []
    assert warnings == [("down", "source_unavailable", "upstream 503")]


@pytest.mark.asyncio
async def test_fanout_transport_error_maps_to_typed_warning() -> None:
    # #152: a transport failure that exhausted retries is an upstream-reachability
    # problem; name the transport class instead of "unexpected error".
    src = _FakeSource("unreachable")

    async def run_one(_: _FakeSource) -> list[Release]:
        raise httpx.ConnectError("connection refused")

    releases, warnings = await fan_out([src], run_one)
    assert releases == []
    assert warnings == [
        ("unreachable", "source_unavailable", "transport error: ConnectError")
    ]


# ───────────────────────── SourceContext retry (WR-01) ──────────────────────────


class _SequenceTransport:
    """Fake Transport returning a queued sequence of responses, one per request."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls += 1
        return self._responses.pop(0)

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _ctx_over(transport: _SequenceTransport, retry_attempts: int = 4) -> object:
    from manga_gateway.framework.context import SourceContext
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager

    return SourceContext(
        source_key="x",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        retry_attempts=retry_attempts,
    )


@pytest.mark.asyncio
async def test_context_retries_5xx_then_succeeds() -> None:
    # WR-01: a 5xx is transient and MUST be retried (matching the docstring/plan);
    # a subsequent 200 wins and the parsed body is returned.
    req = httpx.Request("GET", f"{'https://x/api'}")
    transport = _SequenceTransport(
        [
            httpx.Response(503, request=req),
            httpx.Response(200, json={"data": "ok"}, request=req),
        ]
    )
    ctx = _ctx_over(transport)
    result = await ctx.get_json("https://x/api")  # type: ignore[attr-defined]
    assert result == {"data": "ok"}
    assert transport.calls == 2  # retried once after the 503


@pytest.mark.asyncio
async def test_context_does_not_retry_permanent_4xx() -> None:
    # 401/403/404 are permanent → SourceError on the FIRST attempt, never retried.
    req = httpx.Request("GET", "https://x/api")
    transport = _SequenceTransport([httpx.Response(404, request=req)])
    ctx = _ctx_over(transport)
    with pytest.raises(SourceError):
        await ctx.get_json("https://x/api")  # type: ignore[attr-defined]
    assert transport.calls == 1  # no retry on a permanent 4xx


@pytest.mark.asyncio
async def test_context_attempt_count_is_per_context_search_two_download_four() -> None:
    # 260606-lyb Change 1: the retry budget is now per-context, not a static
    # import-time constant. A SEARCH-path context (retry_attempts=2) makes exactly
    # 2 transport calls on a PERSISTENT retryable 5xx (1 retry); a default context
    # (4) makes 4. Proves search=2 / download=4 from the same code path.
    req = httpx.Request("GET", "https://x/api")

    search_transport = _SequenceTransport(
        [httpx.Response(503, request=req) for _ in range(2)]
    )
    search_ctx = _ctx_over(search_transport, retry_attempts=2)
    with pytest.raises(httpx.HTTPStatusError):
        await search_ctx.get_json("https://x/api")  # type: ignore[attr-defined]
    assert search_transport.calls == 2  # 1 retry → 2 attempts

    download_transport = _SequenceTransport(
        [httpx.Response(503, request=req) for _ in range(4)]
    )
    download_ctx = _ctx_over(download_transport)  # default retry_attempts=4
    with pytest.raises(httpx.HTTPStatusError):
        await download_ctx.get_json("https://x/api")  # type: ignore[attr-defined]
    assert download_transport.calls == 4  # 3 retries → 4 attempts


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

    resp = await client.post(
        # Scope to mangadex so the multi-source fan-out (comix added in 04-03) does
        # not make unmocked calls; this test asserts MangaDex parsing specifically.
        "/search",
        json={"type": "chapter", "query": "Solo Leveling", "sources": ["mangadex"]},
    )
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
    # REL-03: MangaDex has no chapter-vote concept -> votes is null.
    assert first["votes"] is None
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
        "/search",
        json={
            "type": "manga",
            "ids": {"mangadexId": manga_id},
            "sources": ["mangadex"],
        },
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


@pytest.mark.asyncio
async def test_search_empty_id_value_returns_400(client: httpx.AsyncClient) -> None:
    # SRCH-05: an id key present but empty/whitespace is NOT usable input → 400.
    resp = await client.post(
        "/search", json={"type": "manga", "ids": {"mangadexId": "   "}}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


@respx.mock
@pytest.mark.asyncio
async def test_search_truncates_to_limit(client: httpx.AsyncClient) -> None:
    # T-02-04 / WR-03: the merged result is bounded by the requested limit even when
    # the upstream feed returns more rows.
    manga_id = str(uuid.uuid4())
    chapters = [
        {
            "id": str(uuid.uuid4()),
            "type": "chapter",
            "attributes": {
                "volume": None,
                "chapter": str(i),
                "title": None,
                "translatedLanguage": "en",
                "externalUrl": None,
                "isUnavailable": False,
                "publishAt": "2026-05-29T13:57:18+00:00",
                "readableAt": "2026-05-29T13:57:18+00:00",
                "pages": 2,
            },
            "relationships": [
                {"id": "grp", "type": "scanlation_group", "attributes": {"name": "G"}},
                {"id": manga_id, "type": "manga", "attributes": {"title": {"en": "X"}}},
            ],
        }
        for i in range(5)
    ]
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": "ok",
                "response": "collection",
                "data": chapters,
                "total": len(chapters),
                "limit": 100,
                "offset": 0,
            },
        )
    )

    resp = await client.post(
        "/search",
        json={"type": "chapter", "query": "X", "limit": 2, "sources": ["mangadex"]},
    )
    assert resp.status_code == 200
    assert len(resp.json()["releases"]) == 2  # merged total truncated to limit


def _floor_family_feed_payload(manga_id: str) -> dict:
    """A chapter feed exercising the 179.x floor-family + a 180 + a null-chapter row.

    The null-chapter row still produces a Release (mangadex ``_to_release`` only drops
    on missing externalUrl/chapter id), so it reaches ``Source.chapter_matches`` and is
    dropped THERE under an active gate (locked 3).
    """

    def _ch(chapter: str | None, chapter_id: str) -> dict:
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
                {"id": "grp", "type": "scanlation_group", "attributes": {"name": "G"}},
                {
                    "id": manga_id,
                    "type": "manga",
                    "attributes": {"title": {"en": "Solo Leveling"}},
                },
            ],
        }

    data = [
        _ch("179", str(uuid.uuid4())),
        _ch("179.5", str(uuid.uuid4())),
        _ch("179.9", str(uuid.uuid4())),
        _ch("180", str(uuid.uuid4())),
        _ch(None, str(uuid.uuid4())),  # null chapter → Release with chapterNumber None
    ]
    return {
        "result": "ok",
        "response": "collection",
        "data": data,
        "total": len(data),
        "limit": 100,
        "offset": 0,
    }


def _returned_chapter_set(body: dict) -> set[Decimal]:
    # Compare via Decimal(str(...)) to avoid float drift (matches the 1.005 idiom).
    return {
        Decimal(str(r["chapterNumber"]))
        for r in body["releases"]
        if r["chapterNumber"] is not None
    }


@respx.mock
@pytest.mark.asyncio
async def test_search_chapter_filter_floors_to_requested_family(
    client: httpx.AsyncClient,
) -> None:
    """A type=chapter search honors the `chapter` param via Source.chapter_matches.

    chapter=179 AND chapter=179.5 both return ONLY the 179.x floor-family (179, 179.5,
    179.9); 180 is excluded and the null-chapter row is dropped. A chapterless control
    is unfiltered (gate off) — 180 is present and nothing is dropped.
    """
    manga_id = str(uuid.uuid4())
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(200, json=_floor_family_feed_payload(manga_id))
    )

    family = {Decimal("179"), Decimal("179.5"), Decimal("179.9")}

    # (a) integer request → the floor-179 family, 180 excluded, null dropped.
    resp = await client.post(
        "/search",
        json={
            "type": "chapter",
            "query": "Solo Leveling",
            "chapter": 179,
            "sources": ["mangadex"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert _returned_chapter_set(body) == family
    assert Decimal("180") not in _returned_chapter_set(body)
    # 4 non-null + 1 null in the feed; only the 3-member family survives → null dropped.
    assert len(body["releases"]) == 3

    # (b) decimal request → the SAME family (proves floor/whole-number, not exact).
    resp = await client.post(
        "/search",
        json={
            "type": "chapter",
            "query": "Solo Leveling",
            "chapter": 179.5,
            "sources": ["mangadex"],
        },
    )
    assert resp.status_code == 200
    assert _returned_chapter_set(resp.json()) == family

    # (c) control: no `chapter` → gate off, NOT filtered; 180 IS present.
    resp = await client.post(
        "/search",
        json={"type": "chapter", "query": "Solo Leveling", "sources": ["mangadex"]},
    )
    assert resp.status_code == 200
    control = _returned_chapter_set(resp.json())
    assert Decimal("180") in control
    assert family <= control


# ──────────────── Feed-walking: bounded + paginate-all (#144) ───────────────────


def _chapter_row(manga_id: str, chapter: str | None) -> dict:
    """One MangaDex chapter row (same shape as ``_floor_family_feed_payload._ch``)."""
    return {
        "id": str(uuid.uuid4()),
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
            {"id": "grp", "type": "scanlation_group", "attributes": {"name": "G"}},
            {
                "id": manga_id,
                "type": "manga",
                "attributes": {"title": {"en": "Solo Leveling"}},
            },
        ],
    }


def _filler_rows(manga_id: str, chapters: list[str]) -> list[dict]:
    """Build ascending-numbered chapter rows; a list of 100 makes a FULL page."""
    return [_chapter_row(manga_id, ch) for ch in chapters]


def _paginated_chapter_side_effect(
    pages: dict[int, list[dict]], total: int, recorder: list[int]
) -> Callable[[httpx.Request], httpx.Response]:
    """respx side_effect serving ``GET /chapter`` from an offset-keyed page dict.

    Records every requested ``offset`` into ``recorder`` so a test can assert WHICH
    feed pages the walker actually fetched (proves early-stop vs paginate-all).
    """

    def _side_effect(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        recorder.append(offset)
        return httpx.Response(
            200,
            json={
                "result": "ok",
                "response": "collection",
                "data": pages.get(offset, []),
                "total": total,
                "limit": 100,
                "offset": offset,
            },
        )

    return _side_effect


@respx.mock
@pytest.mark.asyncio
async def test_search_walks_to_later_page_for_chapter_family(
    client: httpx.AsyncClient,
) -> None:
    """#144 regression: the 179-family lives on feed page 2 and is still returned.

    Page 0 is a FULL 100-row page of lower chapters (1..100) — none floors past 179 so
    the walk continues — and page 1 (offset 100) holds the 179.x family + a 180. The
    walker must fetch BOTH pages; the family is returned and 180 is excluded.
    """
    manga_id = str(uuid.uuid4())
    page0 = _filler_rows(manga_id, [str(i) for i in range(1, 101)])
    page1 = _filler_rows(manga_id, ["179", "179.5", "179.9", "180"])
    recorder: list[int] = []
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        side_effect=_paginated_chapter_side_effect(
            {0: page0, 100: page1}, total=104, recorder=recorder
        )
    )

    resp = await client.post(
        "/search",
        json={
            "type": "chapter",
            "query": "Solo Leveling",
            "chapter": 179,
            "limit": 1000,
            "sources": ["mangadex"],
        },
    )
    assert resp.status_code == 200
    assert recorder == [0, 100]  # page 2 WAS fetched (the #144 fix)
    returned = _returned_chapter_set(resp.json())
    assert {Decimal("179"), Decimal("179.5"), Decimal("179.9")} <= returned
    assert Decimal("180") not in returned


@respx.mock
@pytest.mark.asyncio
async def test_search_chapter_specific_paginate_all_then_filters_family(
    client: httpx.AsyncClient,
) -> None:
    """A chapter-specific search PAGINATE-ALL walks the whole feed, then filters.

    debug mangadex-search-not-cached: the per-request bounded early-stop walk was
    removed so the Layer-2 cache can key on ``(manga_id, languages)`` ALONE (and serve
    repeats from cache like comix/mangaball). A type=chapter chapter=5 search now walks
    the COMPLETE feed (page 0 full → page 1 offset 100, then exhausted at total=200)
    and applies the ``chapter_matches`` family filter POST-walk — returning exactly the
    family-5 members regardless of which page they land on.
    """
    manga_id = str(uuid.uuid4())
    page0_numbers = ["5", "5.5", *[str(i) for i in range(6, 104)]]  # exactly 100 rows
    assert len(page0_numbers) == 100
    page0 = _filler_rows(manga_id, page0_numbers)
    page1 = _filler_rows(manga_id, [str(i) for i in range(104, 204)])
    recorder: list[int] = []
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        side_effect=_paginated_chapter_side_effect(
            {0: page0, 100: page1}, total=200, recorder=recorder
        )
    )

    resp = await client.post(
        "/search",
        json={
            "type": "chapter",
            "query": "Solo Leveling",
            "chapter": 5,
            "limit": 1000,
            "sources": ["mangadex"],
        },
    )
    assert resp.status_code == 200
    assert recorder == [0, 100]  # paginate-all walks the full feed (both pages)
    # The family-5 filter still returns exactly {5, 5.5}, applied post-walk.
    assert _returned_chapter_set(resp.json()) == {Decimal("5"), Decimal("5.5")}


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "req_body",
    [
        {"type": "manga", "query": "Solo Leveling"},
        {"type": "chapter", "query": "Solo Leveling"},  # chapter=None → paginate-all
    ],
)
async def test_search_paginate_all_walks_every_page(
    client: httpx.AsyncClient, req_body: dict
) -> None:
    """Non-chapter-specific search walks EVERY page and returns the complete set.

    Page 0 = 100 filler chapters (FULL), page 1 (offset 100) = 50 (short → exhausted).
    Both pages are fetched and all 150 releases are returned (limit:1000 avoids the
    route's newest-first truncation).
    """
    manga_id = str(uuid.uuid4())
    page0 = _filler_rows(manga_id, [str(i) for i in range(1, 101)])
    page1 = _filler_rows(manga_id, [str(i) for i in range(101, 151)])
    recorder: list[int] = []
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        side_effect=_paginated_chapter_side_effect(
            {0: page0, 100: page1}, total=150, recorder=recorder
        )
    )

    resp = await client.post(
        "/search",
        json={**req_body, "limit": 1000, "sources": ["mangadex"]},
    )
    assert resp.status_code == 200
    assert recorder == [0, 100]  # the full feed was walked
    assert len(resp.json()["releases"]) == 150


@respx.mock
@pytest.mark.asyncio
async def test_search_skips_rows_with_malformed_attributes(
    client: httpx.AsyncClient,
) -> None:
    """A row whose ``attributes`` is present but not a dict is skipped, not crashed.

    Malformed upstream rows like ``{"attributes": []}`` would otherwise pass the
    top-level dict check and then crash on ``.get(...)`` in the walker's floor
    check / release normalization (CodeRabbit, WR-06 defensive parsing). The good
    rows are still returned.
    """
    manga_id = str(uuid.uuid4())
    bad_row = {"id": str(uuid.uuid4()), "type": "chapter", "attributes": []}
    page0 = [*_filler_rows(manga_id, ["1", "2"]), bad_row]
    recorder: list[int] = []
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(200, json=_manga_search_payload(manga_id))
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        side_effect=_paginated_chapter_side_effect(
            {0: page0}, total=3, recorder=recorder
        )
    )

    resp = await client.post(
        "/search",
        json={"type": "manga", "query": "Solo Leveling", "sources": ["mangadex"]},
    )
    assert resp.status_code == 200
    assert _returned_chapter_set(resp.json()) == {Decimal("1"), Decimal("2")}


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

    resp = await client.post(
        "/search",
        json={"type": "chapter", "query": "Solo Leveling", "sources": ["mangadex"]},
    )
    assert resp.status_code == 200
    handle = resp.json()["releases"][0]["downloadHandle"]
    store = app.state.handle_store
    record = store.resolve(handle)
    assert record is not None
    # chapter_id is a UUID and there is no baseUrl (HDL-01).
    uuid.UUID(record.chapter_id)
    assert not hasattr(record, "base_url")
