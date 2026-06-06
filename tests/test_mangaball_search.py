"""Unit tests for ``MangaBallSource.search`` (Task 1, rebuilt for GAP-1).

The LIVE flow is TWO calls (the old fixtures fabricated a ``title["chapters"]``
shape the API never returns — that false shape is RETIRED here):

1. ``POST /api/v1/title/search-advanced/`` → a TITLE-ONLY envelope (no ``chapters``
   key). ``search`` slices the first ``_DEFAULT_TITLE_CANDIDATES`` candidates.
2. ``POST /api/v1/chapter/chapter-listing-by-title-id/`` per candidate → the FLAT
   ``{code,message,ALL_CHAPTERS:[…]}`` envelope whose ``translations`` are the
   release granularity.

Each Release carries the fully-specific guid
``mangaball:{title_id}:ch-{number_float}:{language}:{translation_id}`` (D-08), an
opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is the bare
``translation_id``, ``page_count`` from ``translation.pages``, and HTML-string
title fields stripped (never raw).

No network: a fake ``SourceContext`` routes ``post_json`` by URL (search-advanced
vs chapter-listing) and records calls for assertions.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangaball import MangaBallSource

# guid contract (D-08): mangaball:{24-hex title}:ch-{float}:{lang}:{24-hex tx}
_GUID_RE = re.compile(r"^mangaball:[0-9a-f]{24}:ch-[\d.]+:[a-z-]{2,}:[0-9a-f]{24}$")

_SEARCH_ADVANCED = "https://mangaball.net/api/v1/title/search-advanced/"
_CHAPTER_LISTING = "https://mangaball.net/api/v1/chapter/chapter-listing-by-title-id/"


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes ``post_json`` by URL (two-call flow).

    A ``search-advanced`` POST returns the title-only envelope; a
    ``chapter-listing-by-title-id`` POST returns the flat listing keyed by the
    posted ``title_id``. Calls are recorded for assertions. The production
    ``SourceContext.post_json`` is the layer that touches httpx.
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
        self, normalized_query: str, languages: list[str]
    ) -> tuple[Any, ...]:
        return ("mangaball", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str]
    ) -> tuple[Any, ...]:
        return ("mangaball", series_id, tuple(sorted(languages)))

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
        if url == _CHAPTER_LISTING:
            title_id = str(data.get("title_id"))
            return _chapter_listing(self._listings.get(title_id, []))
        raise AssertionError(f"unexpected post_json url: {url}")


def _ctx(
    *,
    titles: list[dict[str, Any]],
    listings: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    return _FakeCtxForSearch(titles=titles, listings=listings or {})


def _translation(
    *,
    tx_id: str,
    language: str = "en",
    language_name: str = "English",
    group_name: str | None = "Rayquaza",
    date: str = "2026-06-01 23:33:42",
    pages: int = 66,
) -> dict[str, Any]:
    """One ``translation`` object (the release granularity; recon §3)."""
    group: dict[str, Any] | None = (
        {"_id": "daomeoden", "name": group_name, "icon": "/storage/x.png"}
        if group_name
        else None
    )
    return {
        "id": tx_id,
        "name": f"Chapter {language_name}",
        "language": language,
        "languageName": language_name,
        "group": group,
        "date": date,
        "pages": pages,
        "size": "0MB",  # unreliable — recon Gotchas
        "url": f"http://mangaball.net/chapter-detail/{tx_id}/",
        "volume": 0,
    }


def _chapter(
    *,
    number: str = "Ch. 1184.1",
    number_float: Any = 1184.1,
    translations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One flat ``ALL_CHAPTERS`` row (chapter-listing-by-title-id, §3)."""
    return {
        "number": number,
        "number_float": number_float,
        "title": "",
        "translations": translations
        or [_translation(tx_id="6a1e164ac01e2cf095f75b1a")],
    }


def _title(
    *,
    title_id: str,
    name: str = "One Piece",
    alternate_name: str = 'ワンピース<span class="text-muted">/</span>OP',
) -> dict[str, Any]:
    """A TITLE-ONLY search-advanced hit — explicitly NO ``chapters`` key (GAP-1).

    The live ``search-advanced`` returns titles only; chapters/translations exist
    ONLY in ``chapter-listing-by-title-id``. HTML-string fields
    (``alternateName``/``status``/``last_chapter``) must be stripped, never raw.
    ``alternate_name`` defaults to the real ``/``-separated HTML shape (native
    title + romaji abbreviation) so the alt-title prune (#139) has something to
    split; override it for distractor titles whose alt names must NOT match.
    """
    return {
        "_id": title_id,
        "name": name,
        # HTML-string fields (recon Gotchas) — must be stripped, never raw.
        "alternateName": alternate_name,
        "status": '<span class="badge">Ongoing</span>',
        "last_chapter": '<div class="lc"><a href="/x">Ch. 1184.1</a></div>',
        "url": f"http://mangaball.net/title-detail/one-piece-{title_id}/",
        "updated_at": "2026-06-01 23:33:42",
    }


def _search_envelope(titles: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": 200,
        "message": "ok",
        "data": titles,
        "pagination": {
            "total": len(titles),
            "limit": 28,
            "current_page": 1,
            "last_page": 1,
        },
    }


def _chapter_listing(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    """The FLAT chapter-listing envelope (NOT the standard ``data`` envelope)."""
    return {
        "code": 200,
        "message": "ok",
        "TOTAL_CHAPTERS": len(chapters),
        "ALL_CHAPTERS": chapters,
        "ALL_LANGUAGES": ["en", "vi", "es"],
    }


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
    assert body0["search_input"] == "one piece"
    assert body0["filters[page]"] == 1
    assert body0["filters[sort]"] == "updated_chapters_desc"
    url1, body1 = ctx.calls[1]
    assert url1 == _CHAPTER_LISTING
    assert body1["title_id"] == title_id


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
    tx_id = "6a1e164ac01e2cf095f75b1a"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={
            title_id: [
                _chapter(
                    number_float=1184.1,
                    translations=[_translation(tx_id=tx_id, language="vi")],
                )
            ]
        },
    )
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="one piece"), ctx)

    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == f"mangaball:{title_id}:ch-1184.1:vi:{tx_id}"
    # Opaque, non-empty handle.
    assert rel.download_handle
    assert ":" not in rel.download_handle  # not a structured composite
    # The handle resolves to a record whose chapter_id == the translation id.
    record = ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == tx_id
    assert record.source_key == "mangaball"
    assert record.page_count == 66  # from translation.pages, reliable
    assert rel.page_count == 66
    # GAP-3 (live W-04): the space-separated listing date is normalized to RFC3339
    # ``date-time`` (``T`` separator + UTC) so it conforms to Release.publishDate.
    assert rel.publish_date == "2026-06-01T23:33:42+00:00"
    assert rel.language == "vi"
    assert rel.scanlation_group == "Rayquaza"
    assert rel.chapter_number == Decimal("1184.1")


@pytest.mark.asyncio
async def test_search_multi_group_same_language_yields_two_releases() -> None:
    """GAP-1 lock: a chapter with TWO ``en`` translations (distinct ids/groups)
    → TWO distinct releases with TWO distinct guids."""
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={
            title_id: [
                _chapter(
                    number_float=7.0,
                    translations=[
                        _translation(
                            tx_id="aaaaaaaaaaaaaaaaaaaaaaaa",
                            language="en",
                            group_name="Comick",
                        ),
                        _translation(
                            tx_id="bbbbbbbbbbbbbbbbbbbbbbbb",
                            language="en",
                            group_name="Mangahub",
                        ),
                    ],
                )
            ]
        },
    )
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x"), ctx)

    assert len(releases) == 2
    assert {rel.language for rel in releases} == {"en"}
    guids = {rel.guid for rel in releases}
    assert len(guids) == 2  # distinct translation ids → distinct guids
    assert {rel.scanlation_group for rel in releases} == {"Comick", "Mangahub"}


@pytest.mark.asyncio
async def test_search_one_chapter_many_translations_mints_one_release_each() -> None:
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={
            title_id: [
                _chapter(
                    translations=[
                        _translation(tx_id="aaaaaaaaaaaaaaaaaaaaaaaa", language="en"),
                        _translation(tx_id="bbbbbbbbbbbbbbbbbbbbbbbb", language="vi"),
                        _translation(tx_id="cccccccccccccccccccccccc", language="es"),
                    ]
                )
            ]
        },
    )
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x"), ctx)

    assert len(releases) == 3
    assert {rel.language for rel in releases} == {"en", "vi", "es"}
    assert len({rel.guid for rel in releases}) == 3
    for rel in releases:
        assert _GUID_RE.match(rel.guid), rel.guid


@pytest.mark.asyncio
async def test_search_language_filter_drops_unrequested_languages() -> None:
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(
        titles=[_title(title_id=title_id)],
        listings={
            title_id: [
                _chapter(
                    translations=[
                        _translation(tx_id="aaaaaaaaaaaaaaaaaaaaaaaa", language="en"),
                        _translation(tx_id="bbbbbbbbbbbbbbbbbbbbbbbb", language="vi"),
                    ]
                )
            ]
        },
    )
    source = MangaBallSource()
    releases = await source.search(
        SearchRequest(type="manga", query="x", languages=["vi"]), ctx
    )
    assert len(releases) == 1
    assert releases[0].language == "vi"


@pytest.mark.asyncio
async def test_search_per_candidate_slice_respects_limit_newest_first() -> None:
    """Per-candidate releases are sliced to ``req.limit``, newest-first by date."""
    title_id = "68515540702284f8341784c8"
    chapters = [
        _chapter(
            number_float=float(n),
            translations=[
                _translation(
                    tx_id=f"{n:024x}",
                    language="en",
                    date=f"2026-06-{n:02d} 00:00:00",
                )
            ],
        )
        for n in range(1, 6)  # 5 chapters: dates 2026-06-01 .. 2026-06-05
    ]
    ctx = _ctx(titles=[_title(title_id=title_id)], listings={title_id: chapters})
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x", limit=2), ctx)
    assert len(releases) == 2
    # Newest-first: the two latest dates (06-05, 06-04) survive the slice.
    # publishDate is normalized to RFC3339 (GAP-3).
    assert releases[0].publish_date == "2026-06-05T00:00:00+00:00"
    assert releases[1].publish_date == "2026-06-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_search_strips_html_string_fields() -> None:
    """``alternateName`` / ``status`` HTML never reaches an emitted field value."""
    title_id = "68515540702284f8341784c8"
    ctx = _ctx(titles=[_title(title_id=title_id)], listings={title_id: [_chapter()]})
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

    A long-running title (One Piece ≈ 1382 chapters × thousands of translations)
    must NOT mint a handle per (chapter×translation) for the whole listing — that
    blew past ``HandleStore`` ``maxsize`` (10_000) so the TTLCache evicted the very
    handles attached to the returned releases, and ``POST /downloads`` for
    ``releases[0]`` resolved to a miss. Here a 40-chapter listing with ``limit=3``
    yields 3 releases AND mints exactly 3 handles — and every returned handle still
    resolves (the eviction regression would leave it unresolvable).
    """
    title_id = "68515540702284f8341784c8"
    chapters = [
        _chapter(
            number_float=float(n),
            translations=[
                _translation(tx_id=f"{n:024x}", date=f"2026-06-01 {n:02d}:00:00")
            ],
        )
        for n in range(1, 41)  # 40 chapters » limit
    ]
    ctx = _ctx(titles=[_title(title_id=title_id)], listings={title_id: chapters})
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x", limit=3), ctx)

    assert len(releases) == 3
    # Exactly one handle minted per returned release — NOT one per listing row.
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    # Every returned release's handle resolves (no eviction of survivors).
    for rel in releases:
        assert ctx.handle_store.resolve(rel.download_handle) is not None


# --- alt-title prune wiring (#139, GAP 2) ------------------------------------
#
# These drive the REAL ``MangaBallSource.search()`` so the production
# ``_split_alt`` / ``_strip_html(alternateName)`` extractor AND the production
# ``prune_candidates(keys=...)`` call site both execute. The existing
# ``test_search_strips_html_string_fields`` only checks HTML-stripping of emitted
# fields — NO test previously exercised the alt-title prune wiring (the
# ``alternateName`` → ``_split_alt`` → ``keys=`` path). The number of
# ``chapter-listing-by-title-id`` POSTs is the prune count: an exact-match query
# (main OR alt) deep-enumerates only the one correct title; an ambiguous query
# fans out to the full set.


def _listing_calls(ctx: Any) -> list[tuple[str, dict[str, Any]]]:
    return [c for c in ctx.calls if c[0] == _CHAPTER_LISTING]


@pytest.mark.asyncio
async def test_search_alt_title_match_prunes_fanout_to_one() -> None:
    """A query matching ONLY a title's alt name prunes the listing fan-out to it.

    The correct title (``OP``) matches the query via its ``alternateName``
    (``ワンピース<span>/</span>OP`` → ``["ワンピース", "OP"]`` after
    ``_split_alt``/``_strip_html``); the distractors share neither main nor alt.
    If ``_split_alt`` or the production ``prune_candidates(keys=...)`` call broke,
    the prune would not narrow and ALL candidates would be deep-enumerated."""
    correct_id = "aaaaaaaaaaaaaaaaaaaaaaaa"
    titles = [
        _title(
            title_id=correct_id,
            name="One Piece",
            # Native title + the romaji abbreviation "OP" via the real HTML shape.
            alternate_name="ワンピース<span>/</span>OP",
        ),
        _title(
            title_id="bbbbbbbbbbbbbbbbbbbbbbbb",
            name="One Punch Man",
            alternate_name="ワンパンマン<span>/</span>OPM",
        ),
        _title(
            title_id="cccccccccccccccccccccccc",
            name="Overlord",
            alternate_name="オーバーロード<span>/</span>OVL",
        ),
    ]
    listings = {t["_id"]: [_chapter()] for t in titles}
    ctx = _ctx(titles=titles, listings=listings)

    await MangaBallSource().search(SearchRequest(type="manga", query="OP"), ctx)

    listing_calls = _listing_calls(ctx)
    assert len(listing_calls) == 1
    assert listing_calls[0][1]["title_id"] == correct_id


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
    assert listing_calls[0][1]["title_id"] == correct_id


@pytest.mark.asyncio
async def test_search_ambiguous_query_fans_out_to_full_set() -> None:
    """An ambiguous query (shared keyword, no exact main/alt hit) fans out fully.

    Several candidates merely share the word "Dragon"; none is an exact match on
    main OR alt → the prune falls back to the full candidate set (#126
    conservative behavior), so EVERY candidate is deep-enumerated."""
    titles = [
        _title(
            title_id=f"{i:024x}",
            name=f"Dragon Tale {i}",
            alternate_name=f"ドラゴン{i}<span>/</span>DT{i}",
        )
        for i in range(4)
    ]
    listings = {t["_id"]: [_chapter()] for t in titles}
    ctx = _ctx(titles=titles, listings=listings)

    await MangaBallSource().search(SearchRequest(type="manga", query="dragon"), ctx)

    assert len(_listing_calls(ctx)) == 4
