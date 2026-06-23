"""Unit tests for ``MangaFireSource.search`` — in-process vrf → /filter fan-out.

The LIVE flow (D-13): compute the search ``vrf`` IN-PROCESS via ``compute_vrf(query)``
(no browser, no per-query cache), then ``ctx.get_bytes`` the ``/filter?keyword=…&vrf=…``
HTML (the SAME vrf works on /filter), parse the title-only cards, fan out the per-title
chapter list, filter by ``chapter_matches``, slice to ``req.limit`` and ONLY THEN mint
(GAP-2 mint-after-slice).

No network/browser: ``compute_vrf`` is a pure function, and a fake ``SourceContext``
routes ``get_bytes`` (filter HTML) + ``get_json`` (chapter-list result) and mints real
handles. ``search`` no longer touches ``ctx._solver``; the fake solver is an inert
placeholder kept only so the shared ``_ctx`` helper signature is unchanged.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

import httpx
import pytest

from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangafire import MangaFireSource
from manga_gateway.sources.mangafire_vrf import compute_vrf
from tests.test_mangafire_recent import _cards_html, _chapter_list_html

_GUID_RE = re.compile(r"^mangafire:[\w.-]+:ch-[\d.?]+:[a-z-]+:[\w.-]+$")


class _FakeTypedSolver:
    """Inert placeholder solver. ``search`` no longer drives the browser (it computes
    the ``vrf`` in-process), so it never touches ``ctx._solver``; this double only
    keeps the shared ``_ctx`` helper signature stable. The ``fetch_via_browser`` gate
    asserts search never reaches for a browser primitive.
    """

    async def fetch_via_browser(self, url: str, **kw: Any) -> Any:  # gate only
        raise AssertionError("search must not use the one-shot browser primitive")


class _FakeCtx:
    def __init__(
        self,
        *,
        solver: Any,
        filter_html: bytes,
        chapter_lists: dict[str, str],
        details: dict[str, Any] | None = None,
    ) -> None:
        from manga_gateway.handles.store import HandleStore

        self.handle_store = HandleStore()
        self._solver = solver
        self._filter_html = filter_html
        self._chapter_lists = chapter_lists
        # Phase-13: detail pages keyed by manga_token → HTML (bytes/str) returned by
        # get_bytes, or an Exception instance (raised, to exercise best-effort).
        self._details = details or {}
        self.get_bytes_calls: list[str] = []
        self.detail_calls: list[str] = []
        # CR #289: records the ``limited`` flag passed for each detail GET.
        self.detail_limited: list[bool] = []
        self.candidates_enumerated: int | None = None
        # 13-02 seam: per-request scratch stash (unused by mangafire's detail-GET
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
        return ("mangafire", normalized_query, tuple(sorted(languages)))

    def cached_enumerate_key(
        self, slug_id: str, languages: list[str], *, extra: object = None
    ) -> tuple[Any, ...]:
        return ("mangafire", slug_id, tuple(sorted(languages)))

    async def cached_resolve(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def cached_enumerate(self, key: tuple[Any, ...], fetch_fn: Any) -> Any:
        return await fetch_fn()

    async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
        self.get_bytes_calls.append(url)
        # Detail page GET /manga/{token} (no query) — the Phase-13 external-links GET.
        m = re.search(r"/manga/([^/?]+)$", url)
        if m:
            token = m.group(1)
            self.detail_calls.append(token)
            self.detail_limited.append(limited)
            staged = self._details.get(token)
            if isinstance(staged, Exception):
                raise staged
            if staged is None:
                raise AssertionError(f"no staged detail page for token: {token}")
            return staged if isinstance(staged, bytes) else staged.encode("utf-8")
        return self._filter_html

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        slug_id = url.split("/ajax/manga/")[1].split("/")[0]
        return {"status": 200, "result": self._chapter_lists.get(slug_id, "")}


def _ctx(
    *,
    solver: Any,
    cards: list[tuple[str, str]],
    chapter_lists: dict[str, str],
    details: dict[str, Any] | None = None,
) -> _FakeCtx:
    return _FakeCtx(
        solver=solver,
        filter_html=_cards_html(cards),
        chapter_lists=chapter_lists,
        details=details,
    )


@pytest.mark.asyncio
async def test_search_computes_vrf_and_mints_releases() -> None:
    chapter_lists = {
        "kw9j9": _chapter_list_html(
            [{"number": "346.2", "href": "/read/blue-lockk.kw9j9/en/chapter-346.2"}]
        )
    }
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists=chapter_lists,
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock"), ctx
    )

    # The vrf was computed IN-PROCESS (no browser) and rides the /filter call.
    expected_vrf = compute_vrf("blue lock")
    assert any(
        f"vrf={expected_vrf}" in u and "keyword=" in u for u in ctx.get_bytes_calls
    )
    # One per-chapter release with an opaque handle.
    assert len(releases) == 1
    rel = releases[0]
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.chapter_number == Decimal("346.2")
    assert rel.download_handle and ":" not in rel.download_handle
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == "/read/blue-lockk.kw9j9/en/chapter-346.2"


@pytest.mark.asyncio
async def test_search_gap2_mints_only_for_sliced_releases() -> None:
    chapters = [
        {"number": str(n), "href": f"/read/blue-lockk.kw9j9/en/chapter-{n}"}
        for n in range(40, 0, -1)  # newest-first, 40 chapters
    ]
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists={"kw9j9": _chapter_list_html(chapters)},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock", limit=3), ctx
    )
    assert len(releases) == 3
    # GAP-2: a handle is minted ONLY for the post-slice survivors (store-cap safety).
    assert len(ctx.handle_store._cache) == 3  # noqa: SLF001 — store-size assertion
    # Newest-first slice: chapters 40, 39, 38 survive.
    assert [r.chapter_number for r in releases] == [
        Decimal("40"),
        Decimal("39"),
        Decimal("38"),
    ]


@pytest.mark.asyncio
async def test_search_chapter_type_filters_to_floor_family() -> None:
    chapters = [
        {"number": str(n), "href": f"/read/blue-lockk.kw9j9/en/chapter-{n}"}
        for n in range(5, 0, -1)
    ]
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/blue-lockk.kw9j9", "Blue Lock")],
        chapter_lists={"kw9j9": _chapter_list_html(chapters)},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="chapter", query="blue lock", chapter=3.0), ctx
    )
    assert {r.chapter_number for r in releases} == {Decimal("3")}


def _detail_html(
    *,
    anilist_id: str | None = "30002",
    mal_id: str | None = "2",
    extra: dict[str, Any] | None = None,
    sync_data: bool = True,
) -> str:
    """A MangaFire detail page with (or without) a ``<script id="syncData">``.

    The syncData JSON carries ``anilist_id``+``mal_id`` plus MangaFire-internal keys
    (``manga_id``/``page``) the normalizer DROPS. ``sync_data=False`` renders a page
    with NO syncData script (the absent-script best-effort path).
    """
    if not sync_data:
        return "<html><body><h1>MangaFire</h1></body></html>"
    payload: dict[str, Any] = {"manga_id": "123", "page": "x"}
    if anilist_id is not None:
        payload["anilist_id"] = anilist_id
    if mal_id is not None:
        payload["mal_id"] = mal_id
    if extra:
        payload.update(extra)
    body = json.dumps(payload)
    return f'<html><body><script id="syncData">{body}</script></body></html>'


@pytest.mark.asyncio
async def test_external_links_syncdata_canonical_and_stamped_once() -> None:
    """One detail GET per series → every release carries ``{anilist, myAnimeList}``.

    MangaFire-internal keys (``manga_id``/``page``) are dropped (R3). The detail GET
    fires AT MOST ONCE for the series even with multiple releases, and all releases
    share the IDENTICAL object (R4).
    """
    chapters = [
        {"number": str(n), "href": f"/read/berserkk.m2vv/en/chapter-{n}"}
        for n in (2, 1)
    ]
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/berserkk.m2vv", "Berserk")],
        chapter_lists={"m2vv": _chapter_list_html(chapters)},
        details={"berserkk.m2vv": _detail_html(anilist_id="30002", mal_id="2")},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="berserk"), ctx
    )

    assert len(releases) == 2
    # At most one detail GET per series, keyed on the manga_token.
    assert ctx.detail_calls == ["berserkk.m2vv"]
    links = releases[0].external_links
    assert links is not None
    assert links.model_dump(by_alias=True, exclude_none=True) == {
        "anilist": "30002",
        "myAnimeList": "2",
    }
    # Identical object stamped onto every release (R4).
    assert all(rel.external_links is links for rel in releases)
    # The detail URL was built from the gateway-internal manga_token (card href).
    assert "https://mangafire.to/manga/berserkk.m2vv" in ctx.get_bytes_calls
    # CR #289: the metadata detail GET is rate-limited (shares the per-minute budget).
    assert ctx.detail_limited == [True]


@pytest.mark.asyncio
async def test_external_links_absent_syncdata_leaves_chapters_intact() -> None:
    """A detail page with NO syncData script → ``external_links is None`` (R6)."""
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/berserkk.m2vv", "Berserk")],
        chapter_lists={
            "m2vv": _chapter_list_html(
                [{"number": "1", "href": "/read/berserkk.m2vv/en/chapter-1"}]
            )
        },
        details={"berserkk.m2vv": _detail_html(sync_data=False)},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="berserk"), ctx
    )
    assert len(releases) == 1
    assert releases[0].external_links is None
    assert ctx.detail_calls == ["berserkk.m2vv"]


@pytest.mark.asyncio
async def test_external_links_best_effort_failure_leaves_chapters_intact() -> None:
    """A raising detail GET still returns chapters with ``external_links is None``."""
    ctx = _ctx(
        solver=_FakeTypedSolver(),
        cards=[("/manga/berserkk.m2vv", "Berserk")],
        chapter_lists={
            "m2vv": _chapter_list_html(
                [
                    {"number": "2", "href": "/read/berserkk.m2vv/en/chapter-2"},
                    {"number": "1", "href": "/read/berserkk.m2vv/en/chapter-1"},
                ]
            )
        },
        details={"berserkk.m2vv": RuntimeError("upstream 403")},
    )
    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="berserk"), ctx
    )
    assert len(releases) == 2
    assert all(rel.external_links is None for rel in releases)
    assert ctx.detail_calls == ["berserkk.m2vv"]


# ─────────────── real context/transport seam: no eager CF solve ───────────────


class _RecordingTransport:
    """Serves the mangafire search path over a REAL ``SourceContext``/``SessionManager``
    (no ``_FakeCtx`` shortcut, so ``_clearance_kwargs`` actually runs): GET ``/filter``
    → cards HTML; GET ``/ajax/manga/{slug}/chapter/{lang}`` → chapter-list JSON; GET
    ``/manga/{token}`` → a bare detail page (external-links best-effort).
    """

    def __init__(self, *, filter_html: bytes, chapter_lists: dict[str, str]) -> None:
        self._filter_html = filter_html
        self._chapter_lists = chapter_lists
        self.urls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.urls.append(url)
        req = httpx.Request(method, url)
        if "/ajax/manga/" in url:
            slug_id = url.split("/ajax/manga/")[1].split("/")[0]
            body = {"status": 200, "result": self._chapter_lists.get(slug_id, "")}
            return httpx.Response(200, json=body, request=req)
        if "/filter" in url:
            return httpx.Response(200, content=self._filter_html, request=req)
        if "/manga/" in url:  # detail page (external-links GET, best-effort)
            return httpx.Response(200, content=b"<html></html>", request=req)
        raise AssertionError(f"unexpected url: {url}")  # pragma: no cover

    async def aclose(self) -> None:  # pragma: no cover — interface completeness
        pass


class _PeekOnlySolver:
    """Fails loudly if the real context EAGERLY solves Cloudflare.

    Declares ``solve_if_missing`` so ``SourceContext._call_solver`` forwards the
    on-demand peek kwarg. Returns ``None`` (no held clearance) so a cold request
    proceeds over httpx. ``solve_if_missing=True`` (the eager default, reached only if
    ``cloudflare_challenge_optional`` regresses to ``False``) raises.
    """

    def __init__(self) -> None:
        self.solve_if_missing_calls: list[bool] = []

    async def get_clearance(
        self, source_key: str, *, solve_if_missing: bool = True
    ) -> None:
        self.solve_if_missing_calls.append(solve_if_missing)
        if solve_if_missing:
            raise AssertionError(
                "mangafire search must not eager-solve Cloudflare (cold httpx-first)"
            )
        return None

    async def fetch_via_browser(self, *args: Any, **kwargs: Any) -> Any:  # gate only
        raise AssertionError("search must not touch the browser")


@pytest.mark.asyncio
async def test_search_cold_filter_uses_real_seam_without_eager_solve() -> None:
    """Regression at the REAL clearance seam (260623-m5h): with
    ``cloudflare_challenge_optional=True``, a cold mangafire search peeks held
    clearance (``solve_if_missing=False``) and never eager-solves Cloudflare or touches
    the browser — so search completes over httpx alone. The ``_FakeCtx`` tests above
    bypass ``_clearance_kwargs`` and would pass even if search still eager-solved.
    """
    from manga_gateway.framework.context import SourceContext
    from manga_gateway.framework.ratelimit import RateLimiter
    from manga_gateway.framework.session import SessionManager
    from manga_gateway.handles.store import HandleStore

    chapter_lists = {
        "kw9j9": _chapter_list_html(
            [{"number": "346.2", "href": "/read/blue-lockk.kw9j9/en/chapter-346.2"}]
        )
    }
    transport = _RecordingTransport(
        filter_html=_cards_html([("/manga/blue-lockk.kw9j9", "Blue Lock")]),
        chapter_lists=chapter_lists,
    )
    solver = _PeekOnlySolver()
    ctx = SourceContext(
        source_key="mangafire",
        rate_limit_per_minute=6000,
        session=SessionManager(transport),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,
        antibot="cloudflare",
        cloudflare_challenge_optional=True,
    )

    releases = await MangaFireSource().search(
        SearchRequest(type="manga", query="blue lock"), ctx
    )

    # Cold search completed over httpx and minted a release.
    assert len(releases) == 1
    assert releases[0].chapter_number == Decimal("346.2")
    # The /filter GET carried the in-process vrf.
    expected_vrf = compute_vrf("blue lock")
    assert any("/filter" in u and f"vrf={expected_vrf}" in u for u in transport.urls)
    # The real cf clearance seam ran and ONLY peeked — it never eager-solved.
    assert solver.solve_if_missing_calls
    assert all(v is False for v in solver.solve_if_missing_calls)
