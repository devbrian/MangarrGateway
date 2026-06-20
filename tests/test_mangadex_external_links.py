"""Unit tests for ``MangadexSource.fetch_external_links`` — zero-added-HTTP links.

MangaDex carries its series-level tracker links on the chapter feed's
``includes[]=manga`` expansion (``relationships[type=manga].attributes.links``,
confirmed live, Pitfall 3) — the SAME feed ``search`` already enumerates and caches.
So the hook is PARSING ONLY: it reads the raw links dict off any cached chapter row
and routes it through ``framework.external_links.normalize`` — NO ``/manga/{id}`` fetch
and no second ``/chapter`` walk (R5).

These tests prove the four behaviors of plan 13-03 Task 1:

1. a feed carrying ``attributes.links`` → every release gets the canonical dict
   (storefront ``amz`` dropped, ``bw`` digits extracted);
2. ZERO added HTTP — no ``/manga/{id}`` call, exactly one ``/chapter`` walk;
3. all N releases of the series share the IDENTICAL ``ExternalLinks`` object (R4);
4. a feed whose manga rel has no ``links`` → ``external_links is None``.

No network: a fake ``SourceContext`` routes ``get_json`` by URL (``/manga`` title
search vs ``/chapter`` feed) and records calls; any ``/manga/{id}`` link fetch would
raise (proving zero added HTTP).
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangadex import MangaDexSource

_BASE = "https://api.mangadex.org"
_MANGA = f"{_BASE}/manga"
_CHAPTER = f"{_BASE}/chapter"
_MANGA_ID = "berserk-manga-id"

# Live-shape raw links (Pitfall 3 / 13-RESEARCH frozen table): ``amz`` is a storefront
# key with no map entry (dropped, R3); ``bw`` is a ``series/{digits}[/list]`` path.
_RAW_LINKS = {
    "al": "179445",
    "mal": "2",
    "mu": "njeqwry",
    "ap": "berserk",
    "kt": "8",
    "bw": "series/16664/list",
    "amz": "B0XXXXLINK",
}
# Canonical bare-ID projection — amz dropped, bw digits extracted.
_EXPECTED = {
    "anilist": "179445",
    "myAnimeList": "2",
    "mangaUpdates": "njeqwry",
    "animePlanet": "berserk",
    "kitsu": "8",
    "bookwalker": "16664",
}


def _chapter(*, chapter_id: str, number: int, links: Any = "ABSENT") -> dict[str, Any]:
    """One ``/chapter`` row with an ``includes[]=manga`` rel carrying ``links``.

    ``links="ABSENT"`` omits the key entirely (the no-tracker case); pass a dict
    (or ``None``) to set it explicitly.
    """
    manga_attrs: dict[str, Any] = {"title": {"en": "Berserk"}, "altTitles": []}
    if links != "ABSENT":
        manga_attrs["links"] = links
    return {
        "id": chapter_id,
        "attributes": {
            "chapter": str(number),
            "translatedLanguage": "en",
            "pages": 10,
            "readableAt": "2026-01-01T00:00:00Z",
        },
        "relationships": [
            {"type": "manga", "id": _MANGA_ID, "attributes": manga_attrs},
            {"type": "scanlation_group", "id": "g1", "attributes": {"name": "Grp"}},
        ],
    }


class _FakeCtxForExternalLinks:
    """``SourceContext`` stand-in: routes ``get_json`` (``/manga`` vs ``/chapter``).

    Mirrors the real ctx's enum-cache pass-through + the 13-02 external-links seam:
    ``external_links_raw`` scratch map + a ``resolve_external_links`` pass-through
    (``await parse_fn()`` — the real wrapper's cache/timeout/swallow are tested in
    13-02). Any ``/manga/{id}`` get_json raises, so an accidental link fetch fails.
    """

    def __init__(self, *, chapters: list[dict[str, Any]]) -> None:
        self.handle_store = HandleStore()
        self._chapters = chapters
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.candidates_enumerated: int | None = None
        self.external_links_raw: dict[str, dict[str, Any]] = {}

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangadex", normalized_query, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        base = ("mangadex", series_id, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def resolve_external_links(self, series_id: str, parse_fn: Any) -> Any:
        return await parse_fn()

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append((url, params))
        if url == _MANGA:  # title search
            return {
                "data": [
                    {
                        "id": _MANGA_ID,
                        "attributes": {"title": {"en": "Berserk"}, "altTitles": []},
                    }
                ]
            }
        if url == _CHAPTER:  # chapter feed (includes[]=manga carries links)
            return {"data": self._chapters, "total": len(self._chapters)}
        # A ``/manga/{id}`` (or any other) fetch is a zero-added-HTTP violation.
        raise AssertionError(f"unexpected get_json url: {url}")


def _ctx(*, chapters: list[dict[str, Any]]) -> Any:
    return _FakeCtxForExternalLinks(chapters=chapters)


def _chapter_calls(ctx: Any) -> list[tuple[str, dict[str, Any]]]:
    return [c for c in ctx.calls if c[0] == _CHAPTER]


async def _search(ctx: Any) -> list[Any]:
    return await MangaDexSource().search(
        SearchRequest(type="manga", query="berserk"), ctx
    )


@pytest.mark.asyncio
async def test_emits_canonical_external_links_from_feed() -> None:
    ctx = _ctx(chapters=[_chapter(chapter_id="c1", number=1, links=_RAW_LINKS)])
    releases = await _search(ctx)
    assert len(releases) == 1
    links = releases[0].external_links
    assert links is not None
    assert links.model_dump() == _EXPECTED


@pytest.mark.asyncio
async def test_zero_added_http() -> None:
    ctx = _ctx(chapters=[_chapter(chapter_id="c1", number=1, links=_RAW_LINKS)])
    await _search(ctx)
    # No second /chapter walk for links; exactly one feed fetch.
    assert len(_chapter_calls(ctx)) == 1
    # NO /manga/{id} detail fetch — links rode the feed (Pitfall 3, R5).
    assert [u for u, _ in ctx.calls if u.startswith(f"{_MANGA}/")] == []


@pytest.mark.asyncio
async def test_all_releases_share_identical_object() -> None:
    chapters = [_chapter(chapter_id=f"c{n}", number=n, links=_RAW_LINKS) for n in (1, 2, 3)]
    ctx = _ctx(chapters=chapters)
    releases = await _search(ctx)
    assert len(releases) == 3
    first = releases[0].external_links
    assert first is not None
    # Resolve-once + stamp: every release carries the SAME object (identity, R4).
    for rel in releases:
        assert rel.external_links is first


@pytest.mark.asyncio
async def test_no_links_yields_none() -> None:
    ctx = _ctx(chapters=[_chapter(chapter_id="c1", number=1)])  # links absent
    releases = await _search(ctx)
    assert len(releases) == 1
    assert releases[0].external_links is None
