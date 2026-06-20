"""Offline tests for ``ComixSource`` external-tracker links (plan 13-04).

Comix carries its series-level tracker links on the SAME ``/api/v1/manga`` search
payload ``search`` already fetches (``result.items[].links`` — full tracker URLs:
``al/mal/mu/md/mb``). Before this plan that ``links`` object was parsed and DISCARDED
by ``_search_series`` (Pitfall 4); plan 13-04 widens the cached Layer-1 candidate tuple
to carry it, so ``fetch_external_links`` parses it PARSE-ONLY (zero added HTTP, R5).

Comix exposes FULL URLs, so the shared ``normalize`` extracts the bare IDs/slugs (R2);
no emitted value may contain ``http``. The single resolved ``ExternalLinks`` is stamped
onto every release of the series (identical-object, R4).

Tests:

1. ``fetch_external_links`` over a stashed full-URL ``links`` dict → the canonical
   bare-ID comix projection, and the hard no-``http`` property on every value.
2. A full driven ``search()`` (the ``test_comix_search_parallel`` harness) over a
   series whose ``result.items[].links`` carry full URLs → every returned release
   carries the SAME bare-ID ``externalLinks`` and the ``/api/v1/manga`` search XHR
   fires at most once (links ride the cached candidate — R5).
3. A series whose ``links`` are all ``null`` → releases returned with
   ``externalLinks`` absent (``None``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.sources.comix import ComixSource

from .conftest import BASE_URL, TEST_API_KEY
from .test_comix_search_parallel import _candidates_json, _SlowComixSolver

_COMIX = ComixSource.base_url

# Live-shape Comix ``links`` (13-RESEARCH frozen table, lines 66-77): full tracker
# URLs for all five mapped keys.
_RAW_LINKS = {
    "al": "https://anilist.co/manga/189223/",
    "mal": "https://myanimelist.net/manga/182157/",
    "mu": "https://www.mangaupdates.com/series/nx0v0fc/",
    "md": "https://mangadex.org/title/2b7f6f76-4f7e-4f9e-8f7e-2b7f6f764f7e/",
    "mb": "https://mangabaka.org/222572/",
}
# Canonical bare-ID projection — every full URL stripped to its bare ID/slug.
_EXPECTED = {
    "anilist": "189223",
    "myAnimeList": "182157",
    "mangaUpdates": "nx0v0fc",
    "mangaDex": "2b7f6f76-4f7e-4f9e-8f7e-2b7f6f764f7e",
    "mangaBaka": "222572",
}


# ──────────────────────────────────────────────────────────────────────────────
# (1) Unit: fetch_external_links parses the stashed full-URL links → bare IDs
# ──────────────────────────────────────────────────────────────────────────────


class _FakeLinksCtx:
    """Minimal ``SourceContext`` stand-in for the parse-only hook.

    Exposes the 13-02 external-links seam the hook touches: the ``external_links_raw``
    scratch map (pre-seeded) + a ``resolve_external_links`` pass-through (the real
    wrapper's cache/timeout/swallow are covered in 13-02). The hook reads
    ``external_links_raw`` ONLY — it makes no network call.
    """

    def __init__(self, raw: dict[str, dict[str, Any]]) -> None:
        self.external_links_raw = raw

    async def resolve_external_links(self, series_id: str, parse_fn: Any) -> Any:
        return await parse_fn()


@pytest.mark.asyncio
async def test_fetch_external_links_extracts_bare_ids_from_full_urls() -> None:
    ctx = _FakeLinksCtx({"hid0": dict(_RAW_LINKS)})
    links = await ComixSource().fetch_external_links("hid0", ctx)  # type: ignore[arg-type]
    assert links is not None
    assert links.model_dump() == _EXPECTED
    # Hard R2 property: not a single emitted value still carries a URL.
    for value in links.model_dump(exclude_none=True).values():
        assert "http" not in value


@pytest.mark.asyncio
async def test_fetch_external_links_missing_series_yields_none() -> None:
    ctx = _FakeLinksCtx({})  # nothing stashed
    assert await ComixSource().fetch_external_links("absent", ctx) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_external_links_all_null_yields_none() -> None:
    ctx = _FakeLinksCtx({"hid0": dict.fromkeys(_RAW_LINKS)})  # every value None
    assert await ComixSource().fetch_external_links("hid0", ctx) is None  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────────────
# (2)/(3) Integration: a full driven search stamps + rides the cached candidate
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def comix_app(tmp_path: Path) -> FastAPI:
    return create_app(
        Settings(
            api_key=TEST_API_KEY,
            db_path=str(tmp_path / "jobs.db"),
            handle_db_path=str(tmp_path / "handles.db"),
            output_root=str(tmp_path / "out"),
        )
    )


@pytest_asyncio.fixture
async def slow_solver() -> _SlowComixSolver:
    return _SlowComixSolver()


@pytest_asyncio.fixture
async def comix_client(
    comix_app: FastAPI, slow_solver: _SlowComixSolver
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=comix_app)
    async with comix_app.router.lifespan_context(comix_app):
        comix_app.state.solver = slow_solver
        comix_app.state.job_manager._engine._solver = slow_solver
        async with httpx.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            yield ac


def _mock_one_series_with_links(
    solver: _SlowComixSolver,
    links: dict[str, Any] | None,
    *,
    n_chapters: int,
) -> respx.Route:
    """Mock ``/api/v1/manga`` to return ONE series carrying ``links`` + stage its
    series-page browser result with ``n_chapters`` chapters.

    Returns the respx route so the caller can assert the search XHR call count.
    """
    item: dict[str, Any] = {
        "id": 1000,
        "hid": "hid0",
        "title": "Series 0",
        "latestChapter": n_chapters,
        "url": "/title/hid0-slug0",
        "hasChapters": True,
        "contentRating": "safe",
    }
    if links is not None:
        item["links"] = links
    route = respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(200, json=_candidates_json([item]))
    )
    chapters = [
        {
            "id": f"chap-{n}",
            "chapter": str(n),
            "lang": "en",
            "groups": [{"name": "Group0"}],
        }
        for n in range(1, n_chapters + 1)
    ]
    solver.stage(f"{_COMIX}/title/hid0-slug0", result=chapters)
    return route


@respx.mock
@pytest.mark.asyncio
async def test_search_stamps_identical_links_with_one_search_xhr(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    route = _mock_one_series_with_links(slow_solver, dict(_RAW_LINKS), n_chapters=3)

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "anything", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text
    releases = resp.json()["releases"]
    assert len(releases) == 3

    # Every release of the series carries the SAME bare-ID externalLinks (R4 stamp).
    for rel in releases:
        assert rel["externalLinks"] == _EXPECTED
        # Hard R2: no emitted value carries a URL.
        for value in rel["externalLinks"].values():
            assert "http" not in value

    # The links rode the cached Layer-1 candidate — the search XHR fired at most once
    # (R5: zero added HTTP for external links).
    assert route.call_count <= 1, route.call_count


@respx.mock
@pytest.mark.asyncio
async def test_search_all_null_links_yields_no_external_links(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    _mock_one_series_with_links(slow_solver, dict.fromkeys(_RAW_LINKS), n_chapters=2)

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "anything", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text
    releases = resp.json()["releases"]
    assert len(releases) == 2
    # A series whose tracker links are all null emits no externalLinks at all.
    for rel in releases:
        assert rel.get("externalLinks") is None
