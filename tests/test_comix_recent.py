"""Unit tests for Comix issue #42 — deferred chapter-id resolution + recent feed.

Coverage:

* :func:`_resolve_deferred` — 14 cases lifted from spike 002
  (``.planning/spikes/002-comix-deferred-chapter-id-resolution/test_deferred_resolver.py``):
  int chapter, decimal chapter, trailing-zero / decimal normalization,
  multi-group tie-breaks (newest → non-empty-groups → lowest-id),
  strict-match staleness (missing chapter → ``_DeferredResolutionError``),
  malformed-row tolerance, composite roundtrip + empty-segment rejection.
* :meth:`ComixSource.fetch_manifest` — the deferred branch substitutes the
  resolved numeric id before building the chapter URL; a missing chapter on
  the series page surfaces as ``SourceError('source_unavailable', …)``.
* :meth:`ComixSource.recent` — respx-mocked plaintext one-call feed produces
  the expected Release shape (``:DEFERRED`` guid suffix, deferred composite
  in the handle store, decimal-normalized chapter strings, skips for
  ``hasChapters: false`` / missing hid / unparseable relative time).

No network, no browser — synthetic chapter lists drive the matcher tests, a
fake ``SourceContext`` drives the ``fetch_manifest`` tests, and respx mocks
the single ``/api/v1/manga`` call for the ``recent`` test. The four locked
invariants (decision 1: ``:DEFERRED`` guid permanent; decision 4: strict
staleness; decision 5: Decimal-aware matching; decision 7: no series-page
nav in ``recent()``) are unit-asserted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.comix import (
    _DEFERRED_SENTINEL,
    ComixSource,
    _DeferredResolutionError,
    _make_deferred_composite,
    _resolve_deferred,
)

if TYPE_CHECKING:
    from manga_gateway.framework.context import SourceContext


def _row(
    chapter_id: str,
    chapter: str,
    *,
    rel: str | None = None,
    groups: list[dict[str, Any]] | None = None,
    lang: str = "en",
) -> dict[str, Any]:
    """Build a realistic chapter-list row (matches ``_CHAPTER_LIST_EXTRACT_JS``)."""
    return {
        "id": chapter_id,
        "chapter": chapter,
        "lang": lang,
        "groups": groups or [],
        "publishedAtRelative": rel,
    }


# ────────────────────────── Case 1: int chapter number ──────────────────────


def test_resolve_deferred_int_chapter_number() -> None:
    series = [
        _row("12345", "23", rel="3h ago", groups=[{"name": "GroupA"}]),
        _row("12344", "22", rel="1d ago"),
        _row("12343", "21", rel="2d ago"),
    ]
    assert _resolve_deferred("23", series) == "12345"


# ──────────────────────── Case 2: decimal chapter number ────────────────────


def test_resolve_deferred_decimal_chapter_1_2() -> None:
    series = [
        _row("99001", "2", rel="1h ago"),
        _row("99000", "1.2", rel="2h ago", groups=[{"name": "Decimals R Us"}]),
        _row("98999", "1.1", rel="3h ago"),
        _row("98998", "1", rel="4h ago"),
    ]
    assert _resolve_deferred("1.2", series) == "99000"


def test_resolve_deferred_decimal_chapter_72_8() -> None:
    series = [
        _row("777", "72.8", rel="5h ago"),
        _row("776", "72.5", rel="8h ago"),
    ]
    assert _resolve_deferred("72.8", series) == "777"


# ──────────────────── Case 3: trailing-zero normalization ───────────────────


def test_resolve_deferred_handle_23_dot_0_matches_page_23() -> None:
    """Locked decision 5: Decimal-aware. ``'23.0'`` matches series row ``'23'``."""
    series = [_row("555", "23", rel="2h ago")]
    assert _resolve_deferred("23.0", series) == "555"


def test_resolve_deferred_handle_23_matches_page_23_dot_0() -> None:
    """Reverse direction: ``'23'`` matches series row ``'23.0'`` via Decimal."""
    series = [_row("556", "23.0", rel="2h ago")]
    assert _resolve_deferred("23", series) == "556"


# ────────────── Case 4: multi-group same chapter (tie-break ladder) ─────────


def test_resolve_deferred_multi_group_newest_wins() -> None:
    """Tie-break rung 1 (locked decision 6): newest ``publishedAtRelative``."""
    series = [
        _row("4001", "10", rel="5d ago", groups=[{"name": "OldGroup"}]),
        _row("4002", "10", rel="2h ago", groups=[{"name": "NewGroup"}]),  # newest
        _row("4003", "10", rel="1d ago", groups=[{"name": "MidGroup"}]),
    ]
    assert _resolve_deferred("10", series) == "4002"


def test_resolve_deferred_tie_on_time_prefers_non_empty_groups() -> None:
    """Tie-break rung 2: tie on relative time → non-empty groups wins."""
    series = [
        _row("5001", "10", rel="3h ago", groups=[]),
        _row("5002", "10", rel="3h ago", groups=[{"name": "G"}]),
    ]
    assert _resolve_deferred("10", series) == "5002"


def test_resolve_deferred_tie_on_time_and_groups_lowest_numeric_id() -> None:
    """Tie-break rung 3: tie on time + groups → lowest numeric id wins."""
    series = [
        _row("6002", "10", rel="3h ago"),
        _row("6001", "10", rel="3h ago"),
    ]
    assert _resolve_deferred("10", series) == "6001"


# ────── CodeRabbit PR #50: regression tests for two tie-break helper bugs ────


def test_resolve_deferred_zero_seconds_ago_stays_newest() -> None:
    """CodeRabbit PR #50 bug A: `_relative_seconds("0s ago") == 0`, which the
    old `or 10**12` fallback treated as falsy and demoted to the oldest slot —
    inverting the "newest wins" rule. Guard on ``is None`` instead.

    Without the fix this returns ``"9001"`` (the months-old row), because
    "0s ago" → 0 → 10**12 outranks "30d ago" → 2_592_000.
    """
    series = [
        _row("9001", "10", rel="30d ago", groups=[{"name": "Older"}]),
        _row("9002", "10", rel="0s ago", groups=[{"name": "Brand-new"}]),
    ]
    assert _resolve_deferred("10", series) == "9002"


def test_resolve_deferred_months_ago_ranked_correctly_vs_minutes() -> None:
    """CodeRabbit PR #50 bug B: regex alternation `(s|m|...|mo|mos|y)\\w*`
    captured ``"m"`` for ``"5mo ago"`` (with ``\\w*`` eating ``"o"``), so months
    scored as minutes (43,200× under-count). A months-old row would have
    appeared newer than a real minutes-old row and won rung-1.

    Without the fix this returns ``"8001"`` (the months-old row, scored as 5m).
    """
    series = [
        _row("8001", "10", rel="5mo ago", groups=[{"name": "Stale"}]),
        _row("8002", "10", rel="6m ago", groups=[{"name": "Fresh"}]),
    ]
    assert _resolve_deferred("10", series) == "8002"


# ─────────────────────── Case 5: chapter missing (strict) ───────────────────


def test_resolve_deferred_missing_chapter_raises_strict() -> None:
    """Locked decision 4: strict-match staleness. Missing chapter → raise."""
    series = [
        _row("700", "24", rel="1h ago"),
        _row("699", "22", rel="2h ago"),  # 23 was deleted upstream
    ]
    with pytest.raises(_DeferredResolutionError, match="23 not present"):
        _resolve_deferred("23", series)


# ───────────────────── Case 6: malformed series rows skipped ────────────────


def test_resolve_deferred_skips_malformed_rows() -> None:
    """Malformed rows (None chapter, ``'abc'`` chapter, non-dict, None entry)
    are skipped; the one valid matching row still resolves."""
    series: list[Any] = [
        {"id": "111", "chapter": None, "groups": []},
        {"id": "112", "chapter": "abc", "groups": []},
        _row("113", "5", rel="1h ago"),
        "not a dict",
        None,
    ]
    assert _resolve_deferred("5", series) == "113"


# ───────────────── Case 7: composite roundtrip + sentinel ───────────────────


def test_make_deferred_composite_well_formed_int_chapter() -> None:
    comp = _make_deferred_composite("mr3m0", "the-forgotten-field", "23")
    assert comp == "DEFERRED|mr3m0|the-forgotten-field|23"


def test_make_deferred_composite_roundtrips_through_parser() -> None:
    """Locked decision 8: the deferred composite has exactly 4 non-empty
    segments so the existing ``_parse_composite_chapter_id`` accepts it
    unchanged. The leading segment must equal the ``_DEFERRED_SENTINEL``."""
    comp = _make_deferred_composite("mr3m0", "the-forgotten-field", "23")
    numeric_id, hid, slug, number = ComixSource._parse_composite_chapter_id(comp)
    assert numeric_id == _DEFERRED_SENTINEL == "DEFERRED"
    assert hid == "mr3m0"
    assert slug == "the-forgotten-field"
    assert number == "23"


def test_make_deferred_composite_empty_hid_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("", "slug", "23")


def test_make_deferred_composite_empty_slug_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("mr3m0", "", "23")


def test_make_deferred_composite_empty_chapter_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("mr3m0", "slug", "")


# ─────────────────── Case 8: decimal composite roundtrip ────────────────────


def test_make_deferred_composite_decimal_chapter_roundtrips() -> None:
    comp = _make_deferred_composite("gzg9", "long-forgotten-name", "1.2")
    assert comp == "DEFERRED|gzg9|long-forgotten-name|1.2"
    numeric_id, hid, slug, number = ComixSource._parse_composite_chapter_id(comp)
    assert numeric_id == _DEFERRED_SENTINEL
    assert hid == "gzg9"
    assert slug == "long-forgotten-name"
    assert number == "1.2"


# Note: ``test_comix_declares_supports_recent_true`` lives at the end of this
# file once Task 3 flips the capability — kept out of Task 1 so the helpers
# can land independently of the flag (the plan deliberately stages it).


# ────────────────── fetch_manifest deferred-branch (Task 2) ─────────────────


class _FakeSolver:
    """Records the ``chapter_url`` passed to ``eval_in_webview`` so the test can
    assert the deferred branch resolved the sentinel before navigating.

    Phase 14: comix runs BOTH the chapter-list enumeration AND the chapter-pages
    manifest read through ``eval_in_webview`` (the redroid WebView). Tests that
    exercise the REAL deferred path (no ``_series_chapters`` monkeypatch) stage the
    full series chapter list keyed by the series URL; ``eval_in_webview`` serves it
    for the series URL and the chapter-page image URLs for chapter URLs."""

    def __init__(self, urls: list[str] | None = None) -> None:
        self.last_url: str | None = None
        self._urls = urls or [
            "https://jdpw.wowpic1.store/si/AAAAAAAAAAAAAAAAAAAA/01.webp",
            "https://jdpw.wowpic1.store/si/AAAAAAAAAAAAAAAAAAAA/02.webp",
        ]
        self.paginated_results: dict[str, list[dict[str, Any]]] = {}
        self.paginated_fetch_calls: list[str] = []

    def stage_browser_paginated(self, url: str, result: list[dict[str, Any]]) -> None:
        self.paginated_results[url] = result

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109 — mirrors the seam signature
    ) -> list[str] | list[dict[str, Any]]:
        _ = (js, wait_for, timeout)
        self.last_url = challenge_url
        # The chapter-list enumeration (spike 019/021) rides the eval seam, keyed by
        # the series URL; serve the staged full list for it. Chapter-page manifest
        # reads (chapter URLs) return the image-URL list.
        if challenge_url in self.paginated_results:
            self.paginated_fetch_calls.append(challenge_url)
            return self.paginated_results[challenge_url]
        return self._urls


class _FakeCtxForFetchManifest:
    """Minimal ``SourceContext`` stand-in for the deferred-branch path.

    ``fetch_manifest`` reads ``ctx`` only via :meth:`ComixSource._solver_from_ctx`
    (which reads ``_solver``) and via the ``_series_chapters`` browser nav
    (also through the same solver). ``handle_store`` is included for parity
    with the real ``SourceContext``.
    """

    def __init__(self, solver: _FakeSolver) -> None:
        self._solver = solver
        self.handle_store = HandleStore()


def _ctx_for_fetch(solver: _FakeSolver) -> SourceContext:
    return _FakeCtxForFetchManifest(solver)  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_fetch_manifest_resolves_deferred_composite_then_navigates_resolved_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composite with ``numeric_id == 'DEFERRED'`` triggers
    ``_series_chapters`` + ``_resolve_deferred`` and the resulting chapter URL
    contains the resolved numeric id, NOT the ``DEFERRED`` sentinel."""
    source = ComixSource()
    composite = _make_deferred_composite("mr3m0", "the-forgotten-field", "23")
    # Confirm parser roundtrip (Task 1 already pinned this, asserted again
    # here so a parser regression breaks this test on the right line).
    numeric_id, hid, slug, number = ComixSource._parse_composite_chapter_id(composite)
    assert numeric_id == _DEFERRED_SENTINEL
    assert (hid, slug, number) == ("mr3m0", "the-forgotten-field", "23")

    # Monkeypatch _series_chapters to return a synthetic list containing the
    # deferred chapter number. Real chapter-id strings are numeric and
    # downstream code uses them to build the chapter URL.
    async def fake_series_chapters(
        self: ComixSource,
        series_hid: str,
        series_slug: str,
        limit: int,
        offset: int,
        ctx: Any,
    ) -> list[dict[str, Any]]:
        assert series_hid == "mr3m0"
        assert series_slug == "the-forgotten-field"
        return [
            {"id": "9938735", "chapter": "23", "groups": [{"name": "Thunderscans"}]},
            {"id": "9938700", "chapter": "22", "groups": []},
        ]

    monkeypatch.setattr(ComixSource, "_series_chapters", fake_series_chapters)

    solver = _FakeSolver()
    urls = await source.fetch_manifest(composite, _ctx_for_fetch(solver))
    assert urls  # the fake returns a non-empty allowed-CDN list
    # The chapter URL embeds the RESOLVED numeric id from the series row, not
    # the sentinel — proves the deferred branch substituted before navigating.
    assert solver.last_url is not None
    assert "/9938735-chapter-23" in solver.last_url
    assert "DEFERRED" not in solver.last_url


@pytest.mark.asyncio
async def test_fetch_manifest_deferred_chapter_missing_raises_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locked decision 4: when the deferred chapter is no longer present on
    the series page, ``fetch_manifest`` translates ``_DeferredResolutionError``
    into ``SourceError('source_unavailable', ...)`` — never silently rebinds
    to a different chapter."""
    source = ComixSource()
    composite = _make_deferred_composite("mr3m0", "the-forgotten-field", "23")

    async def fake_series_chapters_without_23(
        self: ComixSource,
        series_hid: str,
        series_slug: str,
        limit: int,
        offset: int,
        ctx: Any,
    ) -> list[dict[str, Any]]:
        # Chapter 23 has been deleted/replaced upstream — only 22 and 24 remain.
        return [
            {"id": "9938800", "chapter": "24", "groups": []},
            {"id": "9938700", "chapter": "22", "groups": []},
        ]

    monkeypatch.setattr(
        ComixSource, "_series_chapters", fake_series_chapters_without_23
    )

    solver = _FakeSolver()
    with pytest.raises(SourceError) as excinfo:
        await source.fetch_manifest(composite, _ctx_for_fetch(solver))
    assert excinfo.value.code == "source_unavailable"
    assert "not present" in str(excinfo.value)
    # The solver was NEVER called — we failed BEFORE navigating to the
    # (now-stale) chapter URL.
    assert solver.last_url is None


@pytest.mark.asyncio
async def test_fetch_manifest_resolved_composite_skips_deferred_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NON-deferred composite (numeric_id is a real id) MUST NOT call
    ``_series_chapters`` — zero regression on the search-path download flow."""
    source = ComixSource()
    resolved_composite = ComixSource._make_composite_chapter_id(
        "9938735", "mr3m0", "the-forgotten-field", "23"
    )

    series_calls: list[tuple[str, str]] = []

    async def fake_series_chapters_should_not_be_called(
        self: ComixSource,
        series_hid: str,
        series_slug: str,
        limit: int,
        offset: int,
        ctx: Any,
    ) -> list[dict[str, Any]]:
        series_calls.append((series_hid, series_slug))
        return []

    monkeypatch.setattr(
        ComixSource, "_series_chapters", fake_series_chapters_should_not_be_called
    )

    solver = _FakeSolver()
    urls = await source.fetch_manifest(resolved_composite, _ctx_for_fetch(solver))
    assert urls
    assert series_calls == [], "deferred branch ran on a resolved composite"
    # URL uses the search-path numeric id verbatim.
    assert solver.last_url is not None
    assert "/9938735-chapter-23" in solver.last_url


@pytest.mark.asyncio
async def test_fetch_manifest_deferred_resolves_against_full_walked_list() -> None:
    """#146 + deferred (locked decision 4): the deferred branch re-reads the
    FULL paginated chapter list via the REAL ``_series_chapters`` (NO monkeypatch)
    and strict-matches a chapter that lives DEEP in the list. Proves the always-
    walk paginated primitive feeds ``_resolve_deferred`` the complete enumeration,
    so a low/old chapter promised by a recent-feed handle is still resolvable."""
    source = ComixSource()
    composite = _make_deferred_composite("mr3m0", "the-forgotten-field", "5")

    solver = _FakeSolver()
    series_url = f"{ComixSource.base_url}/title/mr3m0-the-forgotten-field"
    # 30-chapter full list; chapter 5 sits deep (would render only on a later
    # pagination page on the live site — the #146 failure mode). The fake
    # paginated primitive returns the FULL merged list the real primitive walks.
    full_list = [
        {"id": f"99388{n:02d}", "chapter": str(n), "groups": []}
        for n in range(30, 0, -1)
    ]
    solver.stage_browser_paginated(series_url, full_list)

    urls = await source.fetch_manifest(composite, _ctx_for_fetch(solver))
    assert urls  # the fake returns a non-empty allowed-CDN list
    # The chapter-list enumeration (spike 019/021) was served for the series URL via
    # the eval_in_webview seam (recorded here), feeding the full list to
    # _resolve_deferred.
    assert solver.paginated_fetch_calls == [series_url]
    # Chapter 5's numeric id (9938805) was resolved from the deep row and used to
    # build the chapter URL — NOT the DEFERRED sentinel.
    assert solver.last_url is not None
    assert "/9938805-chapter-5" in solver.last_url
    assert "DEFERRED" not in solver.last_url


# ───────────────────────── recent() shape (Task 3) ──────────────────────────


# The homepage nav target the recent eval runs against (Phase 14). recent() now
# runs the env-module ``c.list({order:{chapter_updated_at:'desc'}, …})`` eval INSIDE
# the cleared WebView (``_RECENT_LIST_API_EXTRACT_JS``) and gets the decrypted
# newest-first envelope back DIRECTLY — no /browse nav, no token-URL capture, no
# get_json_plain replay (the ``_=`` token is signed by the env-module axios in-page).
_RECENT_EVAL_URL = "https://comix.to/"


class _RecentFakeSolver:
    """Fake solver for the ``recent()`` in-WebView ``c.list`` eval path (Phase 14).

    ``recent()`` runs ONE ``eval_in_webview`` against the homepage that mints the
    signed recent-feed query and returns the decrypted envelope; this fake records
    the nav target and returns the staged recent payload. recent() never enumerates
    a per-series chapter list (locked decision 7) — there is exactly one eval.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.eval_navs: list[str] = []
        self._payload = payload

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109 — mirrors the seam signature
    ) -> dict[str, Any]:
        _ = (js, wait_for, timeout)
        self.eval_navs.append(challenge_url)
        return self._payload


class _FakeCtxForRecent:
    """Minimal ``SourceContext`` stand-in for the ``recent()`` in-WebView eval path.

    ``recent()`` (Phase 14) reads ``ctx`` only via ``_solver`` (the one homepage
    ``eval_in_webview`` that mints + decrypts the recent feed) and
    ``handle_store.mint``. The recent payload is served by the solver's eval (no
    httpx / get_json_plain replay anymore).
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.handle_store = HandleStore()
        self._solver = _RecentFakeSolver(payload)
        # Phase-13 (WR-01): per-request scratch stash + best-effort resolve seam.
        self.external_links_raw: dict[str, dict[str, Any]] = {}

    async def resolve_external_links(self, series_id: str, parse_fn: Any) -> Any:
        # Mirror SourceContext.resolve_external_links' best-effort swallow-all.
        try:
            return await parse_fn()
        except Exception:
            return None


def _ctx_for_recent(payload: dict[str, Any]) -> SourceContext:
    return _FakeCtxForRecent(payload)  # type: ignore[return-value]


def _viable_item(
    *,
    hid: str,
    slug_url: str,
    title: str,
    latest_chapter: Any,
    relative: str = "3h ago",
) -> dict[str, Any]:
    """One viable ``/api/v1/manga`` item shape (matches live recon)."""
    return {
        "hid": hid,
        "title": title,
        "url": slug_url,
        "latestChapter": latest_chapter,
        "chapterUpdatedAtFormatted": relative,
        "hasChapters": True,
    }


@pytest.mark.asyncio
async def test_recent_returns_releases_with_deferred_guids_and_composites() -> None:
    """``recent()`` synthesizes one Release per viable item:

    * Exactly 10 viable rows → 10 Releases (3 skip rows ignored).
    * Every guid ends with the literal ``:DEFERRED`` (locked decision 1).
    * Every ``scanlation_group`` and ``page_count`` is ``None`` (populated
      at download-resolve, not at mint).
    * Every Release's resolved ``ResolutionRecord`` carries a composite
      ``chapter_id`` starting with ``DEFERRED|`` matching
      ``_make_deferred_composite(hid, slug, ch_str)``.
    * ``chapter_number`` for the ``"30.1"`` row equals ``Decimal('30.1')``
      (decimal-normalized; not ``Decimal('30.10')``).
    * recent() runs exactly one in-WebView ``c.list`` eval against the homepage
      (Phase 14: the env-module mints + decrypts the recent feed in one device
      round-trip), and no series-page chapter-list enumeration (locked decision 7).
    """
    payload = {
        "status": "ok",
        "result": {
            "items": [
                _viable_item(
                    hid="aaaa1",
                    slug_url="/title/aaaa1-series-one",
                    title="Series One",
                    latest_chapter="23",
                ),
                _viable_item(
                    hid="aaaa2",
                    slug_url="/title/aaaa2-series-two",
                    title="Series Two",
                    latest_chapter="30.1",
                ),
                _viable_item(
                    hid="aaaa3",
                    slug_url="/title/aaaa3-series-three",
                    title="Series Three",
                    latest_chapter="72.8",
                ),
                _viable_item(
                    hid="aaaa4",
                    slug_url="/title/aaaa4-series-four",
                    title="Series Four",
                    latest_chapter="1.2",
                ),
                _viable_item(
                    hid="aaaa5",
                    slug_url="/title/aaaa5-series-five",
                    title="Series Five",
                    latest_chapter=23,  # int form
                ),
                _viable_item(
                    hid="aaaa6",
                    slug_url="/title/aaaa6-series-six",
                    title="Series Six",
                    latest_chapter=0.5,
                    relative="1d ago",
                ),
                _viable_item(
                    hid="aaaa7",
                    slug_url="/title/aaaa7-series-seven",
                    title="Series Seven",
                    latest_chapter="100",
                    relative="2w ago",
                ),
                _viable_item(
                    hid="aaaa8",
                    slug_url="/title/aaaa8-series-eight",
                    title="Series Eight",
                    latest_chapter="5",
                    relative="1mo ago",
                ),
                _viable_item(
                    hid="aaaa9",
                    slug_url="/title/aaaa9-series-nine",
                    title="Series Nine",
                    latest_chapter="2.5",
                ),
                _viable_item(
                    hid="aaab0",
                    slug_url="/title/aaab0-series-ten",
                    title="Series Ten",
                    latest_chapter="7",
                ),
                # SKIP rows below.
                {
                    "hid": "skip1",
                    "title": "Has No Chapters",
                    "url": "/title/skip1-has-no-chapters",
                    "latestChapter": 0,
                    "chapterUpdatedAtFormatted": "1h ago",
                    "hasChapters": False,
                },
                {
                    # Missing hid.
                    "title": "Missing Hid",
                    "url": "/title/-missing-hid",
                    "latestChapter": "9",
                    "chapterUpdatedAtFormatted": "2h ago",
                    "hasChapters": True,
                },
                {
                    "hid": "skip3",
                    "title": "Unparseable Time",
                    "url": "/title/skip3-unparseable-time",
                    "latestChapter": "12",
                    "chapterUpdatedAtFormatted": "yesterday",
                    "hasChapters": True,
                },
            ],
        },
    }
    source = ComixSource()
    ctx = _ctx_for_recent(payload)
    releases = await source.recent(languages=None, limit=20, since=None, ctx=ctx)

    assert len(releases) == 10
    # Phase 14 / locked decision 7: recent() runs EXACTLY ONE in-WebView eval against
    # the homepage (the env-module ``c.list`` recent query mints + decrypts the feed
    # in one device round-trip) — no /browse nav, no token-URL replay, no
    # per-series chapter-list enumeration.
    assert ctx._solver.eval_navs == [_RECENT_EVAL_URL]  # type: ignore[attr-defined]

    # Locked decision 1: literal `:DEFERRED` guid suffix on every Release.
    for rel in releases:
        assert rel.guid.endswith(":DEFERRED"), rel.guid
        assert rel.scanlation_group is None
        assert rel.page_count is None
        assert rel.source_key == "comix"
        assert rel.language == "en"
        assert rel.download_handle

    # Locked decision 5: decimal-normalized chapter strings flow through to
    # both the composite and the Release.chapter_number.
    by_hid = {rel.ids["comixSeriesId"]: rel for rel in releases}
    rel_2 = by_hid["aaaa2"]
    assert rel_2.chapter_number == Decimal("30.1")
    # The guid uses the normalized ch_str ("30.1", NOT "30.10").
    assert ":ch-30.1:" in rel_2.guid

    # Resolution record carries the deferred composite — matches what the
    # search-path's fetch_manifest deferred branch (Task 2) will decode.
    for rel in releases:
        record = await ctx.handle_store.resolve(rel.download_handle)  # type: ignore[attr-defined]
        assert record is not None
        assert record.chapter_id.startswith("DEFERRED|")
        # Composite roundtrips through the existing parser unchanged.
        sentinel, hid, slug, number = ComixSource._parse_composite_chapter_id(
            record.chapter_id
        )
        assert sentinel == _DEFERRED_SENTINEL
        assert hid == rel.ids["comixSeriesId"]
        assert slug  # non-empty
        # The chapter_number on the record (Decimal) round-trips with ch_str.
        assert record.chapter_number is not None
        assert format(record.chapter_number.normalize(), "f") == number


@pytest.mark.asyncio
async def test_recent_populates_external_links_from_item_links() -> None:
    """WR-01: the tokenized ``/api/v1/manga`` feed item carries the SAME ``links``
    tracker dict ``search`` stashes; ``recent()`` must wire it through the parse-only
    normalize seam (ZERO added HTTP) so recent Releases carry ``externalLinks``.

    * An item WITH ``links`` → Release.external_links carries the canonical bare IDs
      (URLs reduced to IDs, R2; storefront keys dropped, R3).
    * An item WITHOUT ``links`` → Release.external_links is ``None``.
    * No extra outbound call is made (still exactly one in-WebView eval).
    """
    item_with = _viable_item(
        hid="bbbb1",
        slug_url="/title/bbbb1-with-links",
        title="With Links",
        latest_chapter="5",
    )
    item_with["links"] = {
        "al": "https://anilist.co/manga/189223/",
        "mu": "https://www.mangaupdates.com/series/nx0v0fc/",
        "amz": "https://www.amazon.com/dp/x",  # storefront -> DROP (R3)
    }
    item_without = _viable_item(
        hid="bbbb2",
        slug_url="/title/bbbb2-no-links",
        title="No Links",
        latest_chapter="7",
    )
    payload = {"status": "ok", "result": {"items": [item_with, item_without]}}
    source = ComixSource()
    ctx = _ctx_for_recent(payload)
    releases = await source.recent(languages=None, limit=20, since=None, ctx=ctx)

    assert len(releases) == 2
    # Still exactly one in-WebView eval — links rode the SAME feed (no extra call).
    assert ctx._solver.eval_navs == [_RECENT_EVAL_URL]  # type: ignore[attr-defined]
    by_hid = {rel.ids["comixSeriesId"]: rel for rel in releases}

    links = by_hid["bbbb1"].external_links
    assert links is not None
    dumped = links.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {"anilist": "189223", "mangaUpdates": "nx0v0fc"}
    # No emitted value contains a URL (R2); storefront key dropped (R3).
    assert all("http" not in v for v in dumped.values())

    # The link-less item carries no externalLinks.
    assert by_hid["bbbb2"].external_links is None


# Need this import after the test definition above to avoid a forward
# reference in the type annotations; pytest collects on import regardless.


# ────────────────────── Symmetric capability assertion ──────────────────────


def test_comix_declares_supports_recent_true() -> None:
    """Symmetric counterpart to the now-deleted issue-#31 assertion.

    Issue #42 flipped ``supports_recent`` from ``False`` to ``True``; ``/caps``
    advertises ``supportsRecent: true`` for Comix (the route-level assertion
    is in ``tests/test_comix_publishdate_and_recent.py``)."""
    assert ComixSource.supports_recent is True
