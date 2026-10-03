"""Unit tests for ``MangaBallSource.search`` (v2 backend, 261003-mangaball-api-v2).

The LIVE flow is TWO calls:

1. ``POST /api/v1/title/search-advanced`` (form, query key ``keyword`` + ``limit``)
   → a TITLE-ONLY envelope. ``search`` prunes to ``_DEFAULT_TITLE_CANDIDATES``.
2. ``POST /api/v1/chapter/chapter-listing-by-title-id`` per candidate with a JSON
   body ``{"title_id": <_id>}`` → ``{"status":"success","data":[row…]}``, FLAT and
   complete: one row per (chapter × language × group) — the release granularity.

Each Release carries the guid ``mangaball:{title_id}:ch-{number}:{lang}:{row_id}``
(D-08), an opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id``
is the row ``id``, and ``page_count`` None (the v2 API exposes no page count).

No network: a fake ``SourceContext`` routes ``post_json`` (search-advanced) and
``post_json_body`` (listing) and records calls for assertions.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangaball import MangaBallSource

# guid contract (D-08): mangaball:{24-hex title}:ch-{float}:{lang}:{24-hex row}
_GUID_RE = re.compile(r"^mangaball:[0-9a-f]{24}:ch-[\d.]+:[a-z-]{2,}:[0-9a-f]{24}$")

_SEARCH_ADVANCED = "https://mangaball.com/api/v1/title/search-advanced"
_CHAPTER_LISTING = "https://mangaball.com/api/v1/chapter/chapter-listing-by-title-id"


class _FakeCtxForSearch:
    """``SourceContext`` stand-in for the two-call flow.

    ``post_json`` serves the title-only ``search-advanced`` envelope;
    ``post_json_body`` serves the flat listing keyed by the posted
    ``body["title_id"]``. Both record ``(url, payload)`` in ``calls``.
    """

    def __init__(
        self,
        *,
        titles: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.handle_store = HandleStore()
        self._titles = titles
        self._listings = listings
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # Enum-cache seam (09-06): mirror the real SourceContext's default-None
    # pass-through — bare ``await fetch_fn()``, no caching — so these fan-out-count
    # tests exercise byte-for-byte the pre-cache behavior they were written for.
    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangaball", normalized_query, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangaball", series_id, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    def cache_replace(self, key: tuple[Any, ...], enum: Any) -> None:
        return None

    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, data))
        if url == _SEARCH_ADVANCED:
            return _search_envelope(self._titles)
        raise AssertionError(f"unexpected post_json url: {url}")

    async def post_json_body(
        self, url: str, *, body: dict[str, Any], **_kw: Any
    ) -> dict[str, Any]:
        self.calls.append((url, body))
        if url == _CHAPTER_LISTING:
            return _chapter_listing(self._listings.get(str(body["title_id"]), []))
        raise AssertionError(f"unexpected post_json_body url: {url}")


def _ctx(
    *,
    titles: list[dict[str, Any]],
    listings: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    return _FakeCtxForSearch(titles=titles, listings=listings or {})


def _chapter(
    *,
    row_id: str = "6a1e164ac01e2cf095f75b1a",
    number: Any = 1184.1,
    lang: str = "en",
    group_name: str | None = "Rayquaza",
    created_at: str = "2026-06-01T23:33:42",
    title_id: str = "68515540702284f8341784c8",
    views: int | None = None,
) -> dict[str, Any]:
    """ONE flat v2 listing row (chapter × language × group; live keys)."""
    row: dict[str, Any] = {
        "id": row_id,
        "title_id": title_id,
        "lang": lang,
        "name": f"Chapter {number}",
        "number": number,
        "chapter_number": number,
        "volume": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "group": (
            {"id": "g1", "_id": "g1", "name": group_name, "slug": "g"}
            if group_name
            else None
        ),
        "group_name": group_name,
        "group_id": "g1" if group_name else None,
    }
    if views is not None:
        row["views"] = views
    return row


def _title(
    *,
    title_id: str,
    name: str = "One Piece",
    alternate_name: Any = None,
    recent_chapters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A TITLE-ONLY search-advanced / recent hit (v2 keys: ``_id`` + ``id``).

    ``alternate_name`` defaults to the live LIST shape (native title + romaji
    abbreviation) so the alt-title prune (#139) has something to match; override it
    for distractors (a legacy ``/``-separated HTML string still works).
    """
    return {
        "_id": title_id,
        "id": title_id,
        "name": name,
        "slug": name.lower().replace(" ", "-"),
        "alternateName": (
            ["ワンピース", "OP"] if alternate_name is None else alternate_name
        ),
        "image": "https://mangaball.com/covers/x.jpg",
        "status": "ongoing",
        "updated_at": "2026-06-01T23:33:42",
        "recent_chapters": recent_chapters or [],
    }


def _search_envelope(titles: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": 200,
        "status": "success",
        "message": "ok",
        "data": titles,
        "pagination": {"total": len(titles), "page": 1, "limit": 50, "total_pages": 1},
    }


def _chapter_listing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The v2 chapter-listing envelope (flat, complete, no pagination)."""
    return {"status": "success", "data": rows}


@pytest.mark.asyncio
async def test_search_posts_search_advanced_then_one_listing_per_candidate() -> None:
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={title_id: [_chapter()]},
    )
    source = MangaBallSource()
    await source.search(SearchRequest(type="manga", query="one piece"), ctx)

    # One search-advanced POST + one chapter-listing POST for the single candidate.
    assert len(ctx.calls) == 2
    url0, body0 = ctx.calls[0]
    assert url0 == _SEARCH_ADVANCED
    assert body0["keyword"] == "one piece"
    assert "search_input" not in body0  # v2 ignores it (returns every title)
    assert body0["limit"] == 50
    assert body0["filters[page]"] == 1
    assert body0["filters[sort]"] == "updated_chapters_desc"
    url1, body1 = ctx.calls[1]
    assert url1 == _CHAPTER_LISTING
    assert body1 == {"title_id": title_id}


@pytest.mark.asyncio
async def test_search_caps_candidates_to_five() -> None:
    """At most 5 candidates are deep-enumerated, regardless of search-hit count."""
    titles = [_title(title_id=f"{i:024x}") for i in range(8)]
    listings = {f"{i:024x}": [_chapter()] for i in range(8)}
    ctx = _ctx(titles=titles, listings=listings)
    source = MangaBallSource()
    await source.search(SearchRequest(type="manga", query="x"), ctx)

    listing_calls = [c for c in ctx.calls if c[0] == _CHAPTER_LISTING]
    assert len(listing_calls) == 5  # _DEFAULT_TITLE_CANDIDATES


@pytest.mark.asyncio
async def test_search_interactive_does_not_change_candidate_count() -> None:
    """GAP-1 lock: interactive=True and =False yield the SAME candidate count."""
    titles = [_title(title_id=f"{i:024x}") for i in range(8)]
    listings = {f"{i:024x}": [_chapter()] for i in range(8)}

    ctx_a = _ctx(titles=titles, listings=listings)
    await MangaBallSource().search(
        SearchRequest(type="manga", query="x", interactive=False), ctx_a
    )
    ctx_b = _ctx(titles=titles, listings=listings)
    await MangaBallSource().search(
        SearchRequest(type="manga", query="x", interactive=True), ctx_b
    )

    n_a = len([c for c in ctx_a.calls if c[0] == _CHAPTER_LISTING])
    n_b = len([c for c in ctx_b.calls if c[0] == _CHAPTER_LISTING])
    assert n_a == n_b == 5


@pytest.mark.asyncio
async def test_search_mints_fully_specific_guid_and_opaque_handle() -> None:
    title_id = "68515540702284f8341784c8"
    row_id = "6a1e164ac01e2cf095f75b1a"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={title_id: [_chapter(row_id=row_id, number=1184.1, lang="vi")]},
    )
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="one piece"), ctx)

    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == f"mangaball:{title_id}:ch-1184.1:vi:{row_id}"
    # Opaque, non-empty handle.
    assert rel.download_handle
    assert ":" not in rel.download_handle  # not a structured composite
    # The handle resolves to a record whose chapter_id == the row id.
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == row_id
    assert record.source_key == "mangaball"
    # The v2 API exposes no page count anywhere.
    assert record.page_count is None
    assert rel.page_count is None
    # The tz-less ISO ``created_at`` is normalized to RFC3339 (UTC) for
    # Release.publishDate conformance.
    assert rel.publish_date == "2026-06-01T23:33:42+00:00"
    assert rel.language == "vi"
    assert rel.scanlation_group == "Rayquaza"
    assert rel.chapter_number == Decimal("1184.1")
    assert rel.volume is None  # volume 0 = none


@pytest.mark.asyncio
async def test_search_multi_group_same_language_yields_two_releases() -> None:
    """Two rows with the same number + ``en`` but distinct ids/groups → TWO
    distinct releases with TWO distinct guids."""
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={
            title_id: [
                _chapter(row_id="a" * 24, number=7, group_name="Comick"),
                _chapter(row_id="b" * 24, number=7, group_name="Mangahub"),
            ]
        },
    )
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x"), ctx)

    assert len(releases) == 2
    assert {rel.language for rel in releases} == {"en"}
    guids = {rel.guid for rel in releases}
    assert len(guids) == 2  # distinct row ids → distinct guids
    assert {rel.scanlation_group for rel in releases} == {"Comick", "Mangahub"}


@pytest.mark.asyncio
async def test_search_one_chapter_many_languages_mints_one_release_each() -> None:
    """One release per row; site codes pass through lowercased, except
    ``kr``→``ko`` (and ``cn``→``zh``)."""
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={
            title_id: [
                _chapter(row_id="a" * 24, lang="en"),
                _chapter(row_id="b" * 24, lang="vi"),
                _chapter(row_id="c" * 24, lang="kr"),
                _chapter(row_id="d" * 24, lang="pt-br"),
                _chapter(row_id="e" * 24, lang="CN"),
            ]
        },
    )
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x"), ctx)

    assert len(releases) == 5
    assert {rel.language for rel in releases} == {"en", "vi", "ko", "pt-br", "zh"}
    assert len({rel.guid for rel in releases}) == 5
    for rel in releases:
        assert _GUID_RE.match(rel.guid), rel.guid


@pytest.mark.asyncio
async def test_search_language_filter_drops_unrequested_languages() -> None:
    """The languages filter applies to the MAPPED code (``kr`` matches ``ko``)."""
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={
            title_id: [
                _chapter(row_id="a" * 24, lang="en"),
                _chapter(row_id="b" * 24, lang="vi"),
                _chapter(row_id="c" * 24, lang="kr"),
            ]
        },
    )
    source = MangaBallSource()
    releases = await source.search(
        SearchRequest(type="manga", query="x", languages=["vi", "ko"]), ctx
    )
    assert sorted(rel.language for rel in releases) == ["ko", "vi"]


@pytest.mark.asyncio
async def test_search_per_candidate_slice_respects_limit_newest_first() -> None:
    """Per-candidate releases are sliced to ``req.limit``, newest-first."""
    title_id = "68515540702284f8341784c8"
    rows = [
        _chapter(row_id=f"{n:024x}", number=n, created_at=f"2026-06-{n:02d}T00:00:00")
        for n in range(1, 6)  # 5 rows: 2026-06-01 .. 2026-06-05
    ]
    ctx = _ctx(titles=[_title(title_id=title_id)], listings={title_id: rows})
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x", limit=2), ctx)
    assert len(releases) == 2
    # Newest-first: the two latest rows (06-05, 06-04) survive the slice.
    assert releases[0].publish_date == "2026-06-05T00:00:00+00:00"
    assert releases[1].publish_date == "2026-06-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_search_strips_html_string_fields() -> None:
    """A legacy HTML ``alternateName`` never reaches an emitted field value."""
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id, alternate_name="ワンピース<span>/</span>OP")],
        listings={title_id: [_chapter()]},
    )
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x"), ctx)

    assert releases
    for rel in releases:
        assert "<" not in rel.title
        assert ">" not in rel.title
        assert "<" not in (rel.manga_title or "")


@pytest.mark.asyncio
async def test_search_empty_results_returns_no_releases() -> None:
    ctx = _ctx(titles=[])
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="nothing"), ctx)
    assert releases == []
    # Only the search-advanced POST fired — no candidate to deep-enumerate.
    assert len(ctx.calls) == 1


@pytest.mark.asyncio
async def test_search_mints_handles_only_for_returned_releases() -> None:
    """GAP-2 (live): a handle is minted ONLY for the post-slice survivors.

    A long-running title (One Piece ≈ 1382 chapters × many rows) must NOT mint a
    handle per listing row — that blew past ``HandleStore`` ``maxsize`` (default
    200_000, GATEWAY_HANDLE_MAXSIZE) so the TTLCache evicted the very handles
    attached to the returned releases. Here a 40-row listing with ``limit=3`` yields
    3 releases AND mints exactly 3 handles — and every returned handle resolves.
    """
    title_id = "68515540702284f8341784c8"
    rows = [
        _chapter(
            row_id=f"{n:024x}",
            number=n,
            created_at=f"2026-06-01T{n % 24:02d}:{n:02d}:00",
        )
        for n in range(1, 41)  # 40 rows » limit
    ]
    ctx = _ctx(titles=[_title(title_id=title_id)], listings={title_id: rows})
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x", limit=3), ctx)

    assert len(releases) == 3
    # Exactly one handle minted per returned release — NOT one per listing row.
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    # Every returned release's handle resolves (no eviction of survivors).
    for rel in releases:
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


# --- alt-title prune wiring (#139, GAP 2) ------------------------------------
#
# These drive the REAL ``MangaBallSource.search()`` so the production
# ``_split_alt`` extractor AND the production ``prune_candidates(keys=...)`` call
# site both execute. The number of ``chapter-listing-by-title-id`` POSTs is the
# prune count: an exact-match query (main OR alt) deep-enumerates only the one
# correct title; an ambiguous query fans out to the full set.


def _listing_calls(ctx: Any) -> list[tuple[str, dict[str, Any]]]:
    return [c for c in ctx.calls if c[0] == _CHAPTER_LISTING]


@pytest.mark.asyncio
async def test_search_alt_title_match_prunes_fanout_to_one() -> None:
    """A query matching ONLY a title's alt name prunes the listing fan-out to it.

    The correct title matches ``OP`` via its v2 list ``alternateName``; one
    distractor keeps the legacy ``/``-separated HTML string (both shapes must be
    split). If ``_split_alt`` or the ``prune_candidates(keys=...)`` call broke, the
    prune would not narrow and ALL candidates would be deep-enumerated."""
    correct_id = "aaaaaaaaaaaaaaaaaaaaaaaa"
    titles = [
        _title(
            title_id=correct_id, name="One Piece", alternate_name=["ワンピース", "OP"]
        ),
        _title(
            title_id="bbbbbbbbbbbbbbbbbbbbbbbb",
            name="One Punch Man",
            alternate_name="ワンパンマン<span>/</span>OPM",  # legacy HTML shape
        ),
        _title(
            title_id="cccccccccccccccccccccccc",
            name="Overlord",
            alternate_name=["オーバーロード", "OVL"],
        ),
    ]
    listings = {t["_id"]: [_chapter()] for t in titles}
    ctx = _ctx(titles=titles, listings=listings)

    await MangaBallSource().search(SearchRequest(type="manga", query="OP"), ctx)

    listing_calls = _listing_calls(ctx)
    assert len(listing_calls) == 1
    assert listing_calls[0][1] == {"title_id": correct_id}


@pytest.mark.asyncio
async def test_search_main_title_match_prunes_fanout_to_one() -> None:
    """Parity (#126): an exact MAIN-title query also prunes the fan-out to one."""
    correct_id = "aaaaaaaaaaaaaaaaaaaaaaaa"
    titles = [
        _title(title_id=correct_id, name="One Piece"),
        _title(title_id="bbbbbbbbbbbbbbbbbbbbbbbb", name="One Punch Man"),
        _title(title_id="cccccccccccccccccccccccc", name="Overlord"),
    ]
    listings = {t["_id"]: [_chapter()] for t in titles}
    ctx = _ctx(titles=titles, listings=listings)

    await MangaBallSource().search(SearchRequest(type="manga", query="One Piece"), ctx)

    listing_calls = _listing_calls(ctx)
    assert len(listing_calls) == 1
    assert listing_calls[0][1] == {"title_id": correct_id}


@pytest.mark.asyncio
async def test_search_ambiguous_query_fans_out_to_full_set() -> None:
    """An ambiguous query (shared keyword, no exact main/alt hit) fans out fully."""
    titles = [
        _title(
            title_id=f"{i:024x}",
            name=f"Dragon Tale {i}",
            alternate_name=[f"ドラゴン{i}", f"DT{i}"],
        )
        for i in range(4)
    ]
    listings = {t["_id"]: [_chapter()] for t in titles}
    ctx = _ctx(titles=titles, listings=listings)

    await MangaBallSource().search(SearchRequest(type="manga", query="dragon"), ctx)

    assert len(_listing_calls(ctx)) == 4


# ─────────────────────────── votes from row views (REL-03) ──────────────────


def test_to_release_populates_votes_from_views() -> None:
    """A row carrying ``views`` maps to ``Release.votes`` (REL-03)."""
    ctx = _ctx(titles=[])
    source = MangaBallSource()
    release = source._to_release(
        "b" * 24, "One Piece", Decimal("1184.1"), _chapter(views=2789), ctx
    )
    assert release is not None
    assert release.votes == 2789


def test_to_release_votes_none_when_no_views() -> None:
    """A row with no ``views`` (e.g. a recent row) leaves ``Release.votes`` None."""
    ctx = _ctx(titles=[])
    source = MangaBallSource()
    release = source._to_release(
        "b" * 24, "One Piece", Decimal("1184.1"), _chapter(), ctx
    )
    assert release is not None
    assert release.votes is None
