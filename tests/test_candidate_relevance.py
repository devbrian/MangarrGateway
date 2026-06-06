"""Tests for the shared candidate-relevance prune (issue #126).

Two layers:

1. **Helper unit tests** drive :func:`prune_candidates` directly over plain
   dict candidates — exact-match prunes to 1, ambiguous fans out to the cap,
   plus the single/empty/empty-title edge cases.
2. **Source integration** mirrors ``tests/test_mangadot_search.py``'s fake ctx
   (records one ``array_calls`` entry per ``chapters/list`` fan-out call) to
   prove the gate is wired at the narrow-before-fanout seam: an exact-match
   query issues exactly ONE chapter enumeration; an ambiguous one still fans out
   to the full cap.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from manga_gateway.framework.relevance import prune_candidates
from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangadex import MangaDexSource
from manga_gateway.sources.mangadot import MangadotSource


def _by_title(item: dict[str, str]) -> str | None:
    return item.get("title")


def _by_titles(item: dict[str, Any]) -> list[str | None]:
    """Extract main + alt titles as the ``keys=`` variant expects."""
    return [item.get("title"), *(item.get("alt_titles") or [])]


# --- Helper unit tests -------------------------------------------------------


def test_exact_match_prunes_to_one() -> None:
    candidates = [
        {"title": "The Forgotten Field!"},  # exact match after normalization
        {"title": "Forgotten Tales"},
        {"title": "A Field Guide"},
    ]
    result = prune_candidates(candidates, "the forgotten field", key=_by_title, cap=5)
    assert len(result) == 1
    assert result[0]["title"] == "The Forgotten Field!"


def test_ambiguous_query_fans_out_to_cap() -> None:
    # Several candidates merely share a common word — no exact match, no
    # dominant gap → fall back to candidates[:cap].
    candidates = [
        {"title": "Dragon Saga"},
        {"title": "Dragon Quest"},
        {"title": "Dragon Heart"},
        {"title": "Dragon Knight"},
        {"title": "Dragon Tales"},
        {"title": "Dragon World"},
    ]
    result = prune_candidates(candidates, "dragon", key=_by_title, cap=5)
    assert len(result) == 5
    # Order + identity preserved (the first `cap` items, unchanged).
    assert [c["title"] for c in result] == [c["title"] for c in candidates[:5]]


def test_single_candidate_passes_through() -> None:
    candidates = [{"title": "Only One"}]
    result = prune_candidates(candidates, "anything", key=_by_title, cap=5)
    assert result == candidates


def test_empty_list_passes_through() -> None:
    result = prune_candidates([], "anything", key=_by_title, cap=5)
    assert result == []


def test_empty_title_never_wins_exact_match() -> None:
    # An empty query normalizes to "" and a candidate with an empty title also
    # normalizes to "" — but the empty-query guard returns candidates[:cap]
    # rather than letting "" == "" prune to the empty-title candidate.
    candidates = [
        {"title": ""},
        {"title": "Real Series"},
        {"title": "Another"},
    ]
    result = prune_candidates(candidates, "", key=_by_title, cap=5)
    assert len(result) == 3


def test_empty_title_candidate_does_not_match_real_query() -> None:
    candidates = [
        {"title": ""},
        {"title": "Some Manga"},
    ]
    # No exact match (empty title scores 0), no dominant gap → fan out (cap=5
    # but only 2 candidates, so both return).
    result = prune_candidates(candidates, "totally different", key=_by_title, cap=5)
    assert len(result) == 2


def test_dominant_top_score_prunes_to_one() -> None:
    # Top candidate is a near-exact prefix; the rest are unrelated → the
    # dominant-gap branch prunes to the leader even without an exact match.
    candidates = [
        {"title": "Solo Leveling Side Story"},
        {"title": "Completely Unrelated Title"},
        {"title": "Nothing Like It"},
    ]
    result = prune_candidates(candidates, "solo leveling", key=_by_title, cap=5)
    assert len(result) == 1
    assert result[0]["title"] == "Solo Leveling Side Story"


def test_two_exact_matches_are_ambiguous() -> None:
    # Two candidates both exactly match → genuine ambiguity → fan out.
    candidates = [
        {"title": "Berserk"},
        {"title": "berserk!"},
        {"title": "Berserk of Gluttony"},
    ]
    result = prune_candidates(candidates, "berserk", key=_by_title, cap=5)
    assert len(result) == 3


# --- keys= (multi-title / alt-title-aware) unit tests ------------------------


def test_keys_exact_on_alt_title_prunes_to_one() -> None:
    # Candidate A matches the query ONLY via its alternate (native) title;
    # no main title matches → still prune to exactly [A] (#139).
    candidates = [
        {"title": "The Forgotten Field", "alt_titles": ["잊혀진 들판"]},
        {"title": "Forgotten Tales", "alt_titles": []},
        {"title": "A Field Guide", "alt_titles": ["A Different Native Name"]},
    ]
    result = prune_candidates(candidates, "잊혀진 들판", keys=_by_titles, cap=5)
    assert len(result) == 1
    assert result[0]["title"] == "The Forgotten Field"


def test_keys_main_title_still_wins() -> None:
    # Main-title exact match is byte-identical behavior to the key= path.
    candidates = [
        {"title": "The Forgotten Field", "alt_titles": ["잊혀진 들판"]},
        {"title": "Forgotten Tales", "alt_titles": []},
        {"title": "A Field Guide", "alt_titles": []},
    ]
    result = prune_candidates(candidates, "the forgotten field", keys=_by_titles, cap=5)
    assert len(result) == 1
    assert result[0]["title"] == "The Forgotten Field"


def test_keys_two_exact_across_main_and_alt_are_ambiguous() -> None:
    # Candidate A matches "op" via its ALT title; candidate B matches via its
    # MAIN title — two DIFFERENT candidates each exact → genuine ambiguity → fan
    # out to candidates[:cap], NOT length 1.
    candidates = [
        {"title": "One Piece", "alt_titles": ["OP"]},  # exact via alt
        {"title": "OP", "alt_titles": []},  # exact via main
        {"title": "Other Title", "alt_titles": []},
    ]
    result = prune_candidates(candidates, "op", keys=_by_titles, cap=5)
    assert len(result) == 3


def test_keys_and_key_are_mutually_exclusive() -> None:
    candidates = [{"title": "A"}, {"title": "B"}]
    # Both → error.
    with pytest.raises((TypeError, ValueError)):
        prune_candidates(candidates, "a", key=_by_title, keys=_by_titles, cap=5)
    # Neither → error.
    with pytest.raises((TypeError, ValueError)):
        prune_candidates(candidates, "a", cap=5)


def test_keys_with_none_and_empty_entries_are_dropped() -> None:
    # A candidate whose every title is None/empty scores 0 and never wins.
    candidates = [
        {"title": None, "alt_titles": [None, "", "   "]},
        {"title": "Real Series", "alt_titles": []},
        {"title": "Another", "alt_titles": []},
    ]
    # Query the all-empty candidate's normalized "" — the empty-query guard does
    # NOT apply here (query is non-empty), but the empty-title candidate scores 0.
    result = prune_candidates(
        candidates, "totally different query", keys=_by_titles, cap=5
    )
    # No exact, no dominant gap → fan out (3 < cap).
    assert len(result) == 3
    # And a query exactly matching an empty string must not prune to the empty
    # candidate (it scores 0, not exact).
    result2 = prune_candidates(candidates, "real series", keys=_by_titles, cap=5)
    assert len(result2) == 1
    assert result2[0]["title"] == "Real Series"


# --- mangadot source integration --------------------------------------------

_BASE = "https://mangadot.net"
_SEARCH = f"{_BASE}/api/search"


def _chapters_url(manga_id: str) -> str:
    return f"{_BASE}/api/manga/{manga_id}/chapters/list"


class _FakeCtxForSearch:
    """``SourceContext`` stand-in mirroring ``test_mangadot_search.py``.

    Records one ``array_calls`` entry per ``chapters/list`` fan-out so the prune
    count is ``len(ctx.array_calls)``.
    """

    def __init__(
        self,
        *,
        manga_list: list[dict[str, Any]],
        listings: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.handle_store = HandleStore()
        self._manga_list = manga_list
        self._listings = listings
        self.array_calls: list[str] = []
        self.candidates_enumerated: int | None = None

    # Enum-cache seam (09-06): mirror the real SourceContext's default-None
    # pass-through — bare ``await fetch_fn()``, no caching — so these fan-out-count
    # integration tests exercise byte-for-byte the pre-cache behavior.
    def cached_resolve_key(
        self, normalized_query: str, languages: list[str]
    ) -> tuple[Any, ...]:
        return ("mangadot", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str]
    ) -> tuple[Any, ...]:
        return ("mangadot", series_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    def cache_replace(self, key: tuple[Any, ...], enum: Any) -> None:
        return None

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        if url == _SEARCH:
            return {
                "manga_list": self._manga_list,
                "pagination": {"current_page": 1, "total_pages": 1},
            }
        raise AssertionError(f"unexpected get_json url: {url}")

    async def get_json_array(self, url: str, **params: Any) -> list[Any]:
        self.array_calls.append(url)
        m = re.match(rf"{re.escape(_BASE)}/api/manga/(.+)/chapters/list$", url)
        if not m:
            raise AssertionError(f"unexpected get_json_array url: {url}")
        return self._listings.get(m.group(1), [])


def _manga(*, manga_id: str, title: str) -> dict[str, Any]:
    return {"id": manga_id, "title": title, "status": "ongoing"}


def _row(*, chapter_id: str) -> dict[str, Any]:
    return {
        "id": chapter_id,
        "chapter_number": 1,
        "volume_number": None,
        "chapter_title": None,
        "language": "en",
        "group_name": "Stick",
        "scanlator_name": "Stick",
        "date_added": "2026-05-12 12:21:39+00",
        "page_count": 75,
        "source": "user",
    }


@pytest.mark.asyncio
async def test_mangadot_exact_match_prunes_fanout_to_one() -> None:
    manga_list = [
        _manga(manga_id="1", title="Murim Psychopath"),  # exact match
        _manga(manga_id="2", title="Murim Login"),
        _manga(manga_id="3", title="Psychopath Diary"),
        _manga(manga_id="4", title="Murim Returns"),
        _manga(manga_id="5", title="Another Murim"),
        _manga(manga_id="6", title="Sixth"),
    ]
    listings = {str(i): [_row(chapter_id=f"c{i}")] for i in range(1, 7)}
    ctx = _FakeCtxForSearch(manga_list=manga_list, listings=listings)

    await MangadotSource().search(
        SearchRequest(type="manga", query="murim psychopath"), ctx
    )

    assert len(ctx.array_calls) == 1
    assert ctx.array_calls == [_chapters_url("1")]
    assert ctx.candidates_enumerated == 1


@pytest.mark.asyncio
async def test_mangadot_ambiguous_query_fans_out_to_five() -> None:
    manga_list = [_manga(manga_id=str(i), title=f"Murim Tale {i}") for i in range(8)]
    listings = {str(i): [_row(chapter_id=f"c{i}")] for i in range(8)}
    ctx = _FakeCtxForSearch(manga_list=manga_list, listings=listings)

    await MangadotSource().search(SearchRequest(type="manga", query="murim"), ctx)

    assert len(ctx.array_calls) == 5  # _DEFAULT_MANGA_CANDIDATES, unchanged
    assert ctx.candidates_enumerated == 5


# --- comix alt-title acceptance (real fixture) -------------------------------

_COMIX_FIXTURE = (
    Path(__file__).parent / "fixtures" / "comix" / "search_the_forgotten_field.json"
)


def _comix_series_tuples() -> list[tuple[str, str, str, list[str]]]:
    """Build the ``_search_series`` 4-tuples from the real comix search fixture.

    Mirrors ``ComixSource._search_series``'s parse: skip ``hasChapters is False``
    items and emit ``(hid, slug, title, alt_titles)`` from the same payload that
    already carries ``altTitles``.
    """
    data = json.loads(_COMIX_FIXTURE.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str, list[str]]] = []
    for item in data["result"]["items"]:
        if not isinstance(item, dict):
            continue
        hid = item.get("hid")
        if not hid:
            continue
        if item.get("hasChapters") is False:
            continue
        title = str(item.get("title") or "")
        slug = str(item.get("url") or "")
        alt_titles = [s for s in (item.get("altTitles") or []) if isinstance(s, str)]
        out.append((str(hid), slug, title, alt_titles))
    return out


def _comix_keys(t: tuple[str, str, str, list[str]]) -> list[str | None]:
    return [t[2], *t[3]]


def test_comix_alt_title_korean_prunes_to_forgotten_field() -> None:
    # The Korean alt title of "The Forgotten Field" (hid mr3m0, altTitles
    # ["잊혀진 들판"]) must prune the per-candidate browser fan-out to that ONE
    # series — even though no MAIN title matches the Korean query (#139).
    series = _comix_series_tuples()
    assert len(series) > 1  # the fixture has many same-keyword series
    result = prune_candidates(series, "잊혀진 들판", keys=_comix_keys, cap=len(series))
    assert len(result) == 1
    assert result[0][2] == "The Forgotten Field"
    assert result[0][0] == "mr3m0"


def test_comix_main_title_prunes_to_forgotten_field() -> None:
    # Parity with #126: the English MAIN title also prunes to the one series.
    series = _comix_series_tuples()
    result = prune_candidates(
        series, "the forgotten field", keys=_comix_keys, cap=len(series)
    )
    assert len(result) == 1
    assert result[0][2] == "The Forgotten Field"
    assert result[0][0] == "mr3m0"


# --- mangadex source integration (#139, fourth source) ----------------------

_MANGADEX_BASE = "https://api.mangadex.org"
_MANGADEX_SEARCH = f"{_MANGADEX_BASE}/manga"
_MANGADEX_FEED = f"{_MANGADEX_BASE}/chapter"


class _FakeMangaDexCtx:
    """``SourceContext`` stand-in for the MangaDex title-fallback fan-out.

    Records one ``feed_calls`` entry per ``GET /chapter`` enumeration so the
    prune count is ``len(ctx.feed_calls)`` — the MangaDex analog of mangadot's
    ``array_calls`` / comix's per-candidate nav count.
    """

    def __init__(self, *, search_data: list[dict[str, Any]]) -> None:
        self.handle_store = HandleStore()
        self._search_data = search_data
        self.feed_calls: list[str] = []
        self.candidates_enumerated: int | None = None

    # Enum-cache seam (09-04): mirror the real SourceContext's default-None
    # pass-through — bare ``await fetch_fn()``, no caching — so these fan-out-count
    # integration tests exercise byte-for-byte the pre-cache behavior.
    def cached_resolve_key(
        self, normalized_query: str, languages: list[str]
    ) -> tuple[Any, ...]:
        return ("mangadex", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str]
    ) -> tuple[Any, ...]:
        return ("mangadex", series_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    def cache_replace(self, key: tuple[Any, ...], enum: Any) -> None:
        return None

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        if url == _MANGADEX_SEARCH:
            return {"result": "ok", "data": self._search_data}
        if url == _MANGADEX_FEED:
            # Record one enumeration per fan-out candidate; return a single
            # plausible chapter so _to_release has something to mint.
            self.feed_calls.append(str(params.get("manga")))
            return {"result": "ok", "data": [_mangadex_chapter()]}
        raise AssertionError(f"unexpected get_json url: {url}")


def _mangadex_item(
    *, manga_id: str, title: str, alt_titles: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """One ``GET /manga?title=`` search item with localized title + altTitles."""
    return {
        "id": manga_id,
        "type": "manga",
        "attributes": {
            "title": {"en": title},
            "altTitles": alt_titles or [],
            "availableTranslatedLanguages": ["en"],
        },
        "relationships": [],
    }


def _mangadex_chapter() -> dict[str, Any]:
    return {
        "id": "chapter-uuid",
        "type": "chapter",
        "attributes": {
            "volume": None,
            "chapter": "1",
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
        ],
    }


@pytest.mark.asyncio
async def test_mangadex_alt_title_match_prunes_fanout_to_one() -> None:
    # The correct manga matches the query ONLY via its native/alt title; the
    # distractors share the keyword but neither main nor alt matches → prune the
    # per-candidate chapter-feed fan-out to that ONE manga_id (#139).
    search_data = [
        _mangadex_item(
            manga_id="correct",
            title="The Forgotten Field",
            alt_titles=[{"ko": "잊혀진 들판"}],
        ),
        _mangadex_item(manga_id="d1", title="Forgotten Tales"),
        _mangadex_item(
            manga_id="d2",
            title="A Field Guide",
            alt_titles=[{"ja": "別の名前"}],
        ),
    ]
    ctx = _FakeMangaDexCtx(search_data=search_data)

    await MangaDexSource().search(
        SearchRequest(type="chapter", query="잊혀진 들판"), ctx
    )

    assert ctx.feed_calls == ["correct"]
    assert ctx.candidates_enumerated == 1


@pytest.mark.asyncio
async def test_mangadex_main_title_match_prunes_fanout_to_one() -> None:
    # Parity with #126: the exact MAIN title query also prunes to one fan-out.
    search_data = [
        _mangadex_item(
            manga_id="correct",
            title="Solo Leveling",
            alt_titles=[{"ko": "나 혼자만 레벨업"}],
        ),
        _mangadex_item(manga_id="d1", title="Solo Camping"),
        _mangadex_item(manga_id="d2", title="Leveling Up Alone"),
    ]
    ctx = _FakeMangaDexCtx(search_data=search_data)

    await MangaDexSource().search(
        SearchRequest(type="chapter", query="Solo Leveling"), ctx
    )

    assert ctx.feed_calls == ["correct"]
    assert ctx.candidates_enumerated == 1


@pytest.mark.asyncio
async def test_mangadex_ambiguous_query_fans_out_to_full_set() -> None:
    # Several candidates merely share a common word — no exact match, no
    # dominant gap → fan out to every candidate (#126 conservative behavior).
    search_data = [
        _mangadex_item(manga_id=f"m{i}", title=f"Dragon Tale {i}") for i in range(4)
    ]
    ctx = _FakeMangaDexCtx(search_data=search_data)

    await MangaDexSource().search(SearchRequest(type="chapter", query="dragon"), ctx)

    assert len(ctx.feed_calls) == 4
    assert ctx.candidates_enumerated == 4
