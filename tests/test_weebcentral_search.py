"""Unit tests for ``WeebCentralSource.search`` — /search/data → full-chapter-list.

The LIVE flow is TWO HTML calls (live recon 2026-06-08):

1. ``GET /search/data`` → a TITLE-ONLY page, one ``<article class="bg-base-300 …">``
   per series. ``search`` prunes to ``_DEFAULT_TITLE_CANDIDATES`` candidates.
2. ``GET /series/{id}/full-chapter-list`` per candidate → the COMPLETE newest-first
   list, one ``<a href=".../chapters/{ULID}">`` + ``Chapter N`` + ``<time datetime>``
   per chapter (one Release each).

Each Release carries the guid ``weebcentral:{seriesId}:ch-{number}:{chapterId}``
(D-08), an opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is
the BARE chapter ULID (the manifest endpoint needs only it). Handles are minted AFTER
the per-candidate slice (mint-after-slice / store-cap safety).

No network: a fake ``SourceContext`` routes ``get_bytes`` by URL (search vs
full-chapter-list) and returns canned HTML byte fixtures built from the live shapes.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.weebcentral import WeebCentralSource, _parse_track_links

_GUID_RE = re.compile(r"^weebcentral:[0-9A-Za-z]{26}:ch-[\d.?]+:[0-9A-Za-z]{26}$")
_BASE = "https://weebcentral.com"
_SEARCH = f"{_BASE}/search/data"

# Valid 26-char Crockford-base32 ULIDs for fixtures.
_SERIES = "01J76XY7E9FNDZ1DBBM6PBJPFK"


def _series_id(n: int) -> str:
    """A deterministic distinct 26-char ULID-shaped id for fixture series n."""
    return f"01J76XY7E9FNDZ1DBBM6PBJP{n:02d}"


def _chapter_id(n: int) -> str:
    """A deterministic distinct 26-char ULID-shaped id for fixture chapter n."""
    return f"01KSTMTWZE2WHXAP7E18YE{n:04d}"


def _search_html(series: list[tuple[str, str]]) -> bytes:
    """Build a /search/data page: one bg-base-300 article per (series_id, title).

    Mirrors the live markup (a nested cover ``<article>`` + the series anchor + the
    cover ``<img alt="<Title> cover">``).
    """
    articles = "\n".join(
        f"""
        <article class="bg-base-300 flex gap-4 p-4">
          <a href="{_BASE}/series/{sid}/{title.replace(" ", "-")}">
            <article class="hidden lg:block">
              <picture>
                <img src="https://temp.compsci88.com/cover/fallback/{sid}.jpg"
                     alt="{title} cover" width="400" height="600">
              </picture>
            </article>
          </a>
        </article>
        """
        for sid, title in series
    )
    return f"<div>{articles}</div>".encode()


def _chapter_list_html(chapters: list[dict[str, Any]]) -> bytes:
    """Build a /full-chapter-list page from chapter dicts (newest-first order).

    Each dict: ``chapter_id`` (required), ``number`` (str/int/None), ``date`` (str).
    Mirrors the live anchor: ``Chapter N`` span text + a ``<time datetime>``.
    """
    rows = []
    for ch in chapters:
        cid = ch["chapter_id"]
        num = ch.get("number")
        label = f"Chapter {num}" if num is not None else "Oneshot"
        date = ch.get("date", "2026-05-29T19:54:30.766Z")
        rows.append(
            f"""
            <a href="{_BASE}/chapters/{cid}" class="flex-1 flex items-center p-2">
              <span class="grow flex items-center gap-2">
                <span class="">{label}</span>
              </span>
              <time class="text-datetime" datetime="{date}">{date}</time>
            </a>
            """
        )
    return f"<div>{''.join(rows)}</div>".encode()


class _FakeCtxForSearch:
    """``SourceContext`` stand-in: routes ``get_bytes`` by URL (search/chapter-list)."""

    def __init__(
        self,
        *,
        search_html: bytes,
        listings: dict[str, bytes],
        details: dict[str, Any] | None = None,
    ) -> None:
        self.handle_store = HandleStore()
        self._search_html = search_html
        self._listings = listings
        # Phase-13: series MAIN pages keyed by the slugged token ``{ULID}/{slug}`` →
        # Track HTML (bytes) returned by get_bytes, or an Exception (raised, to exercise
        # the best-effort path).
        self._details = details or {}
        self.calls: list[str] = []
        self.detail_calls: list[str] = []
        # WR-03: records the ``limited`` flag passed for each detail (main-page) GET.
        self.detail_limited: list[bool] = []
        self.candidates_enumerated: int | None = None
        # 13-02 seam: per-request scratch stash (unused by weebcentral's main-page GET
        # path, present for parity with the real SourceContext).
        self.external_links_raw: dict[str, dict[str, Any]] = {}

    async def resolve_external_links(self, series_id: str, parse_fn: Any) -> Any:
        # Mirror SourceContext.resolve_external_links' best-effort swallow-all.
        try:
            return await parse_fn()
        except Exception:
            return None

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("weebcentral", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("weebcentral", series_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
        self.calls.append(url)
        parsed = urlparse(url)
        path = parsed.path
        if path == "/search/data":
            return self._search_html
        m = re.match(r"^/series/([^/]+)/full-chapter-list$", path)
        if m is not None:
            return self._listings.get(m.group(1), b"<div></div>")
        # Series MAIN page GET /series/{ULID}/{slug} — the Phase-13 external-links GET.
        main = re.match(r"^/series/([^/]+)/([^/]+)$", path)
        if main is not None and main.group(2) != "full-chapter-list":
            token = f"{main.group(1)}/{main.group(2)}"
            self.detail_calls.append(token)
            self.detail_limited.append(limited)
            staged = self._details.get(token)
            if isinstance(staged, Exception):
                raise staged
            if staged is None:
                raise AssertionError(f"no staged main page for token: {token}")
            return staged
        raise AssertionError(f"unexpected get_bytes url: {url}")


def _ctx(
    *,
    series: list[tuple[str, str]],
    listings: dict[str, list[dict[str, Any]]] | None = None,
    details: dict[str, Any] | None = None,
) -> Any:
    listings = listings or {}
    return _FakeCtxForSearch(
        search_html=_search_html(series),
        listings={sid: _chapter_list_html(chs) for sid, chs in listings.items()},
        details=details,
    )


def _chapter(
    *, chapter_id: str, number: Any = 1184, date: str = "2026-05-29T19:54:30.766Z"
) -> dict[str, Any]:
    return {"chapter_id": chapter_id, "number": number, "date": date}


def _list_calls(ctx: Any) -> list[str]:
    return [u for u in ctx.calls if "/full-chapter-list" in u]


@pytest.mark.asyncio
async def test_search_one_listing_per_candidate() -> None:
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1))]},
    )
    await WeebCentralSource().search(
        SearchRequest(type="manga", query="one piece"), ctx
    )
    # One /search/data, then exactly one /full-chapter-list for the candidate.
    assert sum(1 for u in ctx.calls if urlparse(u).path == "/search/data") == 1
    assert len(_list_calls(ctx)) == 1
    assert f"/series/{_SERIES}/full-chapter-list" in ctx.calls[1]
    # The query rides the ``text`` param.
    assert "text=one+piece" in ctx.calls[0]


@pytest.mark.asyncio
async def test_search_caps_candidates_to_five() -> None:
    series = [(_series_id(i), f"Series {i}") for i in range(8)]
    listings = {_series_id(i): [_chapter(chapter_id=_chapter_id(i))] for i in range(8)}
    ctx = _ctx(series=series, listings=listings)
    await WeebCentralSource().search(SearchRequest(type="manga", query="series"), ctx)
    assert len(_list_calls(ctx)) == 5  # _DEFAULT_TITLE_CANDIDATES


@pytest.mark.asyncio
async def test_search_mints_bare_handle_and_guid() -> None:
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1), number=1184)]},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="one piece"), ctx
    )
    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == f"weebcentral:{_SERIES}:ch-1184:{_chapter_id(1)}"
    assert rel.download_handle
    assert ":" not in rel.download_handle  # opaque, not a structured composite
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    # The BARE chapter ULID — the manifest endpoint needs only it (NOT a composite).
    assert record.chapter_id == _chapter_id(1)
    assert record.source_key == "weebcentral"
    assert rel.language == "en"
    assert rel.chapter_number == Decimal("1184")
    assert rel.publish_date == "2026-05-29T19:54:30.766000+00:00"
    assert rel.ids == {
        "weebcentralSeriesId": _SERIES,
        "weebcentralChapterId": _chapter_id(1),
    }
    assert "(en)" in rel.title


@pytest.mark.asyncio
async def test_search_decimal_chapter_number_preserved() -> None:
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1), number="1184.5")]},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x"), ctx
    )
    assert releases[0].chapter_number == Decimal("1184.5")
    assert releases[0].guid == f"weebcentral:{_SERIES}:ch-1184.5:{_chapter_id(1)}"


@pytest.mark.asyncio
async def test_search_newest_first_slice_respects_limit() -> None:
    # Document order IS newest-first (live full-chapter-list); 5,4,3,2,1 top→bottom.
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in (5, 4, 3, 2, 1)]
    ctx = _ctx(series=[(_SERIES, "S")], listings={_SERIES: chapters})
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x", limit=2), ctx
    )
    assert len(releases) == 2
    assert [r.chapter_number for r in releases] == [Decimal("5"), Decimal("4")]


@pytest.mark.asyncio
async def test_search_mints_handles_only_for_returned_releases() -> None:
    """Mint-after-slice: a handle ONLY for the post-slice survivors (store-cap)."""
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in range(40, 0, -1)]
    ctx = _ctx(series=[(_SERIES, "S")], listings={_SERIES: chapters})
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x", limit=3), ctx
    )
    assert len(releases) == 3
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    for rel in releases:
        assert await ctx.handle_store.resolve(rel.download_handle) is not None


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in (5, 4, 3, 2, 1)]
    ctx = _ctx(series=[(_SERIES, "S")], listings={_SERIES: chapters})
    releases = await WeebCentralSource().search(
        SearchRequest(type="chapter", query="x", chapter=3.0), ctx
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}


@pytest.mark.asyncio
async def test_search_empty_results_returns_no_releases() -> None:
    ctx = _ctx(series=[])
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="nothing"), ctx
    )
    assert releases == []
    # Only the /search/data call — no candidate to enumerate.
    assert len(_list_calls(ctx)) == 0


@pytest.mark.asyncio
async def test_search_non_english_language_filter_returns_empty() -> None:
    """Weeb Central is English-only: a non-en filter yields nothing (no calls)."""
    ctx = _ctx(
        series=[(_SERIES, "One Piece")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1))]},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="x", languages=["ja"]), ctx
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call


# ───────────────────────── Phase-13 external links (R2/R4/R6) ─────────────────────
# The series MAIN page (/series/{ULID}/{slug}) Track section carries three labeled
# anchors as FULL URLs: AniList + MangaUpdates + Official Source. The hook does ONE
# best-effort main-page GET per series, parses the Track section, drops Official
# Source, extracts the bare id/slug, and stamps the single object on every release.
# Solo Leveling live IDs (RESEARCH live-smoke): anilist=105398, mangaUpdates=6z1uqw7.

# The search-href slug is ``title.replace(" ", "-")`` (see _search_html), so "Solo
# Leveling" → "Solo-Leveling"; the slugged main-page token is ``{ULID}/{slug}``.
_SOLO_SLUG = "Solo-Leveling"
_SOLO_TOKEN = f"{_SERIES}/{_SOLO_SLUG}"


def _track_html(
    *,
    anilist: str | None = "https://anilist.co/manga/105398/Solo-Leveling/",
    mangaupdates: str | None = (
        "https://www.mangaupdates.com/series/6z1uqw7/solo-leveling"
    ),
    official: str | None = "https://www.tapas.io/series/solo-leveling",
    track_section: bool = True,
) -> bytes:
    """Build a WeebCentral series MAIN page with (or without) a Track section.

    The Track section renders as ``<strong>Track:</strong>`` then one labeled
    ``<span data-tip="<Label>"><a href="<URL>"></span>`` per tracker (AniList,
    MangaUpdates, Official Source) — full URLs, mirroring the live markup. A ``None``
    href drops that label; ``track_section=False`` renders a page with NO Track section
    (the absent-Track best-effort path).
    """
    if not track_section:
        return b"<html><body><h1>Solo Leveling</h1></body></html>"
    spans = []
    for label, href in (
        ("AniList", anilist),
        ("MangaUpdates", mangaupdates),
        ("Official Source", official),
    ):
        if href is None:
            continue
        spans.append(
            f'<span class="tooltip" data-tip="{label}">'
            f'<a href="{href}">{label}</a></span>'
        )
    body = "".join(spans)
    return (
        f"<html><body><section><strong>Track:</strong>{body}</section></body></html>"
    ).encode()


def test_parse_track_links_matches_exact_heading() -> None:
    """IN-04: the real ``Track:`` heading is matched (links collected)."""
    out = _parse_track_links(_track_html())
    assert out == {
        "AniList": "https://anilist.co/manga/105398/Solo-Leveling/",
        "MangaUpdates": "https://www.mangaupdates.com/series/6z1uqw7/solo-leveling",
        "Official Source": "https://www.tapas.io/series/solo-leveling",
    }


def test_parse_track_links_ignores_lookalike_heading() -> None:
    """IN-04: a ``Soundtrack`` / ``Backtrack`` heading is NOT a substring match —
    its sibling spans must not be collected as tracker links."""
    html = (
        b"<html><body>"
        b"<section><strong>Soundtrack</strong>"
        b'<span data-tip="AniList"><a href="https://anilist.co/manga/999/">x</a>'
        b"</span></section>"
        b"<section><strong>Backtrack</strong>"
        b'<span data-tip="MangaUpdates"><a href="https://x.example/series/abc">y</a>'
        b"</span></section>"
        b"</body></html>"
    )
    assert _parse_track_links(html) == {}


@pytest.mark.asyncio
async def test_external_links_track_canonical_and_stamped_once() -> None:
    """One main-page GET per series → every release carries ``{anilist, mangaUpdates}``.

    Official Source is dropped (R3), no value is a URL (R2), the GET fires AT MOST ONCE
    for the series, and all releases share the IDENTICAL object (R4).
    """
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in (2, 1)]
    ctx = _ctx(
        series=[(_SERIES, "Solo Leveling")],
        listings={_SERIES: chapters},
        details={_SOLO_TOKEN: _track_html()},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="solo leveling"), ctx
    )
    assert len(releases) == 2
    # At most one main-page GET per series, keyed on the slugged token.
    assert ctx.detail_calls == [_SOLO_TOKEN]
    # WR-03: the metadata detail GET is rate-limited (shares the per-minute budget).
    assert ctx.detail_limited == [True]
    links = releases[0].external_links
    assert links is not None
    dumped = links.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {"anilist": "105398", "mangaUpdates": "6z1uqw7"}
    # Official Source dropped; no emitted value contains a URL (R2).
    assert all("http" not in v for v in dumped.values())
    # Identical object stamped onto every release (R4).
    assert all(rel.external_links is links for rel in releases)
    # The main-page URL was built from the gateway-internal id/slug (search href).
    assert f"{_BASE}/series/{_SOLO_TOKEN}" in ctx.calls


@pytest.mark.asyncio
async def test_external_links_drops_official_source_only() -> None:
    """A Track section with ONLY Official Source → ``external_links is None`` (R3)."""
    ctx = _ctx(
        series=[(_SERIES, "Solo Leveling")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1))]},
        details={_SOLO_TOKEN: _track_html(anilist=None, mangaupdates=None)},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="solo leveling"), ctx
    )
    assert len(releases) == 1
    assert releases[0].external_links is None
    assert ctx.detail_calls == [_SOLO_TOKEN]


@pytest.mark.asyncio
async def test_external_links_absent_track_leaves_chapters_intact() -> None:
    """A main page with NO Track section → ``external_links is None`` (R6)."""
    ctx = _ctx(
        series=[(_SERIES, "Solo Leveling")],
        listings={_SERIES: [_chapter(chapter_id=_chapter_id(1))]},
        details={_SOLO_TOKEN: _track_html(track_section=False)},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="solo leveling"), ctx
    )
    assert len(releases) == 1
    assert releases[0].external_links is None
    assert ctx.detail_calls == [_SOLO_TOKEN]


@pytest.mark.asyncio
async def test_external_links_best_effort_failure_leaves_chapters_intact() -> None:
    """A raising main-page GET still returns chapters, ``external_links is None``."""
    chapters = [_chapter(chapter_id=_chapter_id(n), number=n) for n in (2, 1)]
    ctx = _ctx(
        series=[(_SERIES, "Solo Leveling")],
        listings={_SERIES: chapters},
        details={_SOLO_TOKEN: RuntimeError("upstream 403")},
    )
    releases = await WeebCentralSource().search(
        SearchRequest(type="manga", query="solo leveling"), ctx
    )
    assert len(releases) == 2
    assert all(rel.external_links is None for rel in releases)
    assert ctx.detail_calls == [_SOLO_TOKEN]
