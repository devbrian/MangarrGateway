"""Comix ``search()`` parallel-fan-out behavior (debug ``comix-parallel-engine-probe``).

The per-candidate ``_series_chapters`` browser navigations are awaited
CONCURRENTLY via :func:`asyncio.gather` so /search's wall-clock is bounded by
``max(individual)`` rather than ``sum(individual)``. The fan-out is bounded
entirely by the framework's existing ``CloudflareSolver._browser_lock``
Semaphore (``cloudflare_fetch_concurrency``) — this source adds no second gate.

ENGINE NOTE (debug ``comix-parallel-engine-probe``, 2026-06-01): the default is
now ``engine=patchright`` (Chromium) + ``cloudflare_fetch_concurrency=3``, so
/search fans out 3-at-a-time out of the box. ``engine=camoufox`` (Firefox)
stalls concurrent CF navs and MUST be pinned to 1 (enforced by the Settings
``_reject_camoufox_parallel`` validator, #64). These tests drive the
``search()`` gather directly through a fake solver, so they validate the
source-level concurrency shape independently of the engine.

Tests:
* (a) ALL 5 candidates' chapters are returned in candidate (relevance) order —
  the ``return_exceptions=True`` gather preserves coroutine-launch ordering
  even when individual coros complete out-of-order.
* (b) Per-candidate failure does NOT nuke the rest — a single SourceError on
  candidate #2 still yields chapters from candidates #0/#1/#3/#4.
* (c) Wall-clock bounded by ``max(individual)`` not ``sum`` — five navs each
  sleeping 0.1s complete well under the sequential 0.5s, AND a cross-coro
  ``max_in_flight >= 2`` witness catches a regression to a sequential loop even
  when wall-clock variance is small.
* (d) Order is preserved when navs complete OUT of the launch order.
* (e) A Semaphore wrapped around the fake solver (the framework
  ``_browser_lock`` analog) caps simultaneous in-flight navs at its limit —
  proving the gather self-throttles to ``cloudflare_fetch_concurrency``.

Driven by a deterministic fake solver — no real browser, no network.
"""

from __future__ import annotations

import asyncio
import json
import time
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
from manga_gateway.framework.antibot import Clearance
from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.comix import ComixSource

from .conftest import BASE_URL, TEST_API_KEY

_COMIX = ComixSource.base_url
_CF_COOKIE = {"cf_clearance": "CF-CLEAR-TOKEN"}
_CF_UA = "Mozilla/5.0 (Comix-Chrome) AppleWebKit/537.36"


class _SlowComixSolver:
    """Fake solver whose ``fetch_via_browser`` sleeps per URL (URL→delay map).

    Used to assert parallel fan-out: if 5 candidates each sleep 0.1s and the
    fetches run in PARALLEL, the gathered wall-clock is ~0.1s, NOT 0.5s. If a
    regression re-introduces a sequential ``for`` loop, the test sees a
    wall-clock close to 0.5s and ``max_in_flight == 1`` and fails.

    Optionally returns an exception on a given URL (instead of the staged
    result) to drive the per-candidate-failure-isolation test. An optional
    ``gate`` Semaphore models the framework's ``_browser_lock`` so the cap test
    can assert the gather self-throttles.
    """

    def __init__(self, gate: asyncio.Semaphore | None = None) -> None:
        self.browser_results: dict[str, object] = {}
        self.browser_errors: dict[str, Exception] = {}
        self.browser_delays: dict[str, float] = {}
        self.browser_fetch_calls: list[str] = []
        self._gate = gate
        # Cross-call concurrency observability — the parallel test asserts
        # ``max_in_flight > 1`` so a regression to the sequential ``for`` loop
        # (where in-flight is always 1) is caught even when total wall-clock
        # variance is small. With a ``gate`` set, ``max_in_flight`` is bounded
        # by the Semaphore limit (the cap test).
        self.in_flight = 0
        self.max_in_flight = 0

    def stage(
        self,
        url: str,
        *,
        result: object = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        if error is not None:
            self.browser_errors[url] = error
        else:
            self.browser_results[url] = result
        self.browser_delays[url] = delay

    async def get_clearance(self, source_key: str) -> Clearance:
        return Clearance(cookies=dict(_CF_COOKIE), user_agent=_CF_UA)

    async def _gated_fetch(self, url: str) -> object:
        """Shared in-flight/gate/delay accounting for the browser primitives."""
        if self._gate is not None:
            await self._gate.acquire()
        try:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(self.browser_delays.get(url, 0.0))
                if url in self.browser_errors:
                    raise self.browser_errors[url]
                if url not in self.browser_results:
                    raise AssertionError(f"unmocked browser fetch({url!r})")
                return self.browser_results[url]
            finally:
                self.in_flight -= 1
        finally:
            if self._gate is not None:
                self._gate.release()

    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        _ = (extract, wait_for, timeout)
        self.browser_fetch_calls.append(url)
        return await self._gated_fetch(url)

    async def fetch_via_browser_typed(
        self,
        url: str,
        *,
        type_selector: str,
        type_text: str,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        # Comix search mints a `_=` token by typing into the SPA search box
        # (debug comix-search-api-403). The production extract reads the tokenized
        # /api/v1/manga URL off Resource-Timing; the fake returns the equivalent
        # URL so the source's verbatim get_json_plain replay hits the respx mock
        # (respx matches /api/v1/manga by path, ignoring the query/token).
        _ = (url, type_selector, extract, wait_for, timeout)
        return (
            f"{_COMIX}/api/v1/manga?keyword={type_text}"
            "&limit=6&content_rating=suggestive&_=faketoken"
        )

    async def fetch_via_browser_paginated(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        next_selector: str,
        route_limit_rewrite: tuple[str, int] | None = None,
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        # #232: ``_series_chapters`` always-enumerates via the SEQUENTIAL paginated
        # primitive (reverted #222's parallel fan-out — the signed ``_=`` token is
        # incompatible with the parallel URL-rewrite), so the per-candidate
        # concurrency test must exercise THIS method (the one the source actually
        # calls). The per-candidate ``search()`` gather is still concurrent; this
        # fake's in-flight/gate/delay accounting is what those assertions read.
        _ = (extract, wait_for, next_selector, route_limit_rewrite)
        _ = (max_pages, timeout)
        self.browser_fetch_calls.append(url)
        return await self._gated_fetch(url)


@pytest.fixture
def comix_app(tmp_path: Path) -> FastAPI:
    return create_app(
        Settings(
            api_key=TEST_API_KEY,
            db_path=str(tmp_path / "jobs.db"),
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


def _candidates_json(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok",
        "result": {
            "items": items,
            "meta": {
                "total": len(items),
                "perPage": 28,
                "page": 1,
                "lastPage": 1,
            },
        },
    }


def _mock_five_series(solver: _SlowComixSolver, delays: list[float]) -> list[str]:
    """Mock the upstream /api/v1/manga to return 5 candidate series and stage a
    1-chapter result for each at ``delays[i]`` seconds.

    Returns the list of series titles in order (so tests can assert relevance
    ordering is preserved across the parallel fan-out)."""
    assert len(delays) == 5, "five-series helper requires exactly 5 delays"
    hids = [f"hid{i}" for i in range(5)]
    titles = [f"Series {i}" for i in range(5)]
    items = [
        {
            "id": 1000 + i,
            "hid": hids[i],
            "title": titles[i],
            "latestChapter": 1,
            "url": f"/title/{hids[i]}-slug{i}",
            "hasChapters": True,
            "contentRating": "safe",
        }
        for i in range(5)
    ]
    respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(200, json=_candidates_json(items))
    )
    for i in range(5):
        series_url = f"{_COMIX}/title/{hids[i]}-slug{i}"
        chapter = {
            "id": f"chap-{i}",
            "chapter": "1",
            "lang": "en",
            "groups": [{"name": f"Group{i}"}],
        }
        solver.stage(series_url, result=[chapter], delay=delays[i])
    return titles


# ──────────────────────────────────────────────────────────────────────────────
# (a) ALL 5 candidates returned in candidate (relevance) order
# ──────────────────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_search_returns_all_five_candidates_in_order(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    titles = _mock_five_series(slow_solver, [0.0] * 5)

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "anything", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    releases = body["releases"]
    # User hard constraint: /search MUST return ALL chapters for ALL matching
    # candidates (5-candidate cap; one chapter per candidate in this fixture).
    assert len(releases) == 5, releases
    # Candidate ordering is preserved (relevance-sorted upstream).
    assert [r["mangaTitle"] for r in releases] == titles
    # No warnings on the clean path.
    assert body.get("warnings") == []


# ──────────────────────────────────────────────────────────────────────────────
# (b) Per-candidate failure does NOT nuke the rest
# ──────────────────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_one_candidate_failure_does_not_nuke_other_four(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    titles = _mock_five_series(slow_solver, [0.0] * 5)
    # Re-stage candidate #2 (hid2) to raise — simulates a transient browser
    # failure on one series. asyncio.gather(return_exceptions=True) must
    # surface this as a per-coro exception; ComixSource.search() must skip it
    # while still returning chapters from candidates #0/#1/#3/#4.
    boom_url = f"{_COMIX}/title/hid2-slug2"
    slow_solver.stage(
        boom_url,
        error=SourceError("source_unavailable", "browser blew up on this candidate"),
    )

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "anything", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    releases = body["releases"]
    # Four survivors, in relevance order, candidate #2 removed.
    assert len(releases) == 4
    assert [r["mangaTitle"] for r in releases] == [
        titles[0],
        titles[1],
        titles[3],
        titles[4],
    ]
    # A per-CANDIDATE failure stays internal to ComixSource.search() (logged at
    # WARNING) — it is NOT a whole-source warning, so the /search response is
    # still 200 with the survivors and no ``warnings[]`` entry.
    assert body.get("warnings") == []


# ──────────────────────────────────────────────────────────────────────────────
# (c) Wall-clock bounded by max(individual), not sum
# ──────────────────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_search_wall_clock_bounded_by_max_not_sum(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    """Five candidates, each browser nav sleeping 0.1s. Sequential = 0.5s wall-
    clock; parallel = ~0.1s wall-clock. A 0.3s ceiling catches the sequential
    regression with margin for scheduler jitter and the rest of the search
    path (mocked respx upstream call, _to_release synthesis)."""
    per_call_delay = 0.1
    delays = [per_call_delay] * 5
    _mock_five_series(slow_solver, delays)
    sum_sequential = per_call_delay * 5  # 0.5s

    t0 = time.perf_counter()
    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "anything", "sources": ["comix"]},
    )
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    assert len(resp.json()["releases"]) == 5
    # The meaningful bound: well under the sum. Use a 0.3s ceiling
    # (3 × per_call_delay) to leave generous headroom for scheduler jitter
    # while still failing fast if someone re-introduces a sequential loop.
    assert elapsed < sum_sequential * 0.6, (
        f"search took {elapsed:.3f}s — should be parallel (max={per_call_delay}s, "
        f"sequential={sum_sequential}s)"
    )
    # Cross-coro concurrency witness — at least 2 calls were in-flight at the
    # same time. If someone regresses to a sequential ``for`` loop, this drops
    # to 1 and the test fails directly (not just on wall-clock).
    assert slow_solver.max_in_flight >= 2, (
        f"only {slow_solver.max_in_flight} concurrent browser fetches — "
        "search likely regressed to a sequential loop"
    )


# ──────────────────────────────────────────────────────────────────────────────
# (d) Order preserved when navs complete OUT of launch order
# ──────────────────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_search_preserves_candidate_order_under_skewed_delays(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    """Skewed per-candidate delays — last candidate finishes FIRST, first
    candidate finishes LAST. ``asyncio.gather`` preserves coro-launch order in
    its result list, so the response ordering still matches the relevance order
    (which is also the launch order). This catches a future drift to
    ``asyncio.as_completed`` that would surface results in completion order."""
    titles = _mock_five_series(
        slow_solver,
        # Candidate 0 slowest, candidate 4 fastest.
        delays=[0.05, 0.04, 0.03, 0.02, 0.01],
    )

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "anything", "sources": ["comix"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [r["mangaTitle"] for r in body["releases"]] == titles


# ──────────────────────────────────────────────────────────────────────────────
# (e) A Semaphore (the framework _browser_lock analog) caps in-flight navs
# ──────────────────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_search_fan_out_is_capped_by_browser_semaphore(
    comix_app: FastAPI,
) -> None:
    """The parallel gather self-throttles to the framework's ``_browser_lock``.

    In production the per-nav ``fetch_via_browser`` calls all queue on the one
    ``CloudflareSolver._browser_lock`` ``asyncio.Semaphore(fetch_concurrency)``,
    so ``cloudflare_fetch_concurrency`` is the real cap — ComixSource.search()
    adds no second gate. Here we wrap the fake solver in a 2-wide Semaphore (the
    ``_browser_lock`` analog) and assert that with five 0.1s candidates the
    observed ``max_in_flight`` never exceeds 2 — i.e. the gather honors the cap
    rather than opening all five at once."""
    cap = 2
    gated_solver = _SlowComixSolver(gate=asyncio.Semaphore(cap))
    _mock_five_series(gated_solver, [0.1] * 5)

    transport = httpx.ASGITransport(app=comix_app)
    async with comix_app.router.lifespan_context(comix_app):
        comix_app.state.solver = gated_solver
        comix_app.state.job_manager._engine._solver = gated_solver
        async with httpx.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            resp = await ac.post(
                "/search",
                json={"type": "chapter", "query": "anything", "sources": ["comix"]},
            )

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["releases"]) == 5
    # The cap is honored: never more than ``cap`` navs in flight simultaneously.
    assert gated_solver.max_in_flight <= cap, (
        f"max_in_flight={gated_solver.max_in_flight} exceeded the "
        f"Semaphore cap={cap} — fan-out is not honoring the browser gate"
    )
    # And it DID run in parallel up to the cap (not serialized to 1).
    assert gated_solver.max_in_flight == cap, (
        f"max_in_flight={gated_solver.max_in_flight} — expected to saturate the "
        f"cap of {cap} with five concurrent candidates"
    )


# ──────────────────────────────────────────────────────────────────────────────
# (f) Alt-title prune runs through the REAL _search_series + prune call site
# ──────────────────────────────────────────────────────────────────────────────
#
# GAP 1 (#139 coverage review): the only other comix alt-title test
# (``tests/test_candidate_relevance.py::test_comix_alt_title_korean_prunes_to_
# forgotten_field``) drives ``prune_candidates`` over a test-LOCAL tuple builder
# that REIMPLEMENTS ``_search_series``'s parse — so a regression in the real
# 4-tuple threading or the production ``prune_candidates(keys=...)`` call site
# would not be caught. THIS test drives the REAL ``ComixSource.search()`` over
# the real ``tests/fixtures/comix/search_the_forgotten_field.json`` payload, so
# both the production ``_search_series`` parse (``altTitles`` → ``t[3]``) AND the
# production ``keys=lambda t: [t[2], *t[3]]`` prune call execute. The browser
# fan-out count (``slow_solver.browser_fetch_calls``) is the prune result: an
# alt/main-title query that prunes to the single ``mr3m0`` series triggers
# exactly ONE per-candidate browser nav, not one per valid candidate.

_COMIX_FIXTURE = (
    Path(__file__).parent / "fixtures" / "comix" / "search_the_forgotten_field.json"
)


def _forgotten_field_fixture() -> dict[str, Any]:
    """The real comix ``/api/v1/manga`` search payload (28 items, 22 valid)."""
    return json.loads(_COMIX_FIXTURE.read_text(encoding="utf-8"))


def _valid_series_urls() -> list[str]:
    """The series-page URLs the REAL ``_search_series`` would emit per candidate.

    Mirrors ``_search_series``'s skip rules (no hid / ``hasChapters is False``)
    only to know which series-page URLs to STAGE on the fake solver — the
    production parse + prune still run for real over the fixture; this just
    pre-stages a browser result for every candidate that COULD be navigated so
    the test's narrowing assertion is meaningful (an un-pruned fan-out would hit
    every staged URL).
    """
    urls: list[str] = []
    for item in _forgotten_field_fixture()["result"]["items"]:
        if not isinstance(item, dict):
            continue
        hid = item.get("hid")
        if not hid or item.get("hasChapters") is False:
            continue
        slug = ComixSource._slug_from_item(item)  # noqa: SLF001 — mirror the URL build
        urls.append(f"{_COMIX}/title/{hid}-{slug}")
    return urls


def _stage_forgotten_field(solver: _SlowComixSolver) -> list[str]:
    """Mock the upstream search with the real fixture + stage every candidate.

    Returns the list of valid series URLs (so the test can assert the prune
    narrowed the browser fan-out to a strict SUBSET — ideally the single
    ``mr3m0`` URL)."""
    respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(200, json=_forgotten_field_fixture())
    )
    urls = _valid_series_urls()
    for url in urls:
        solver.stage(
            url,
            result=[{"id": "c1", "chapter": "1", "lang": "en", "groups": []}],
        )
    return urls


_FORGOTTEN_FIELD_URL = f"{_COMIX}/title/mr3m0-the-forgotten-field"


@respx.mock
@pytest.mark.asyncio
async def test_real_search_alt_title_korean_prunes_browser_fanout_to_one(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    """The Korean alt title ``잊혀진 들판`` prunes the REAL search to ``mr3m0`` only.

    The fixture has 22 valid candidates (many share the "Forgotten"/"Field"
    keyword); the correct series matches the Korean query ONLY via its
    ``altTitles`` entry. If the production ``_search_series`` failed to thread
    ``altTitles`` into the 4-tuple OR the production ``prune_candidates(keys=...)``
    call regressed, the prune would not narrow and the browser would be navigated
    for every candidate (or fail to find the alt match) — both caught here."""
    valid_urls = _stage_forgotten_field(slow_solver)
    assert len(valid_urls) > 1  # the fixture genuinely fans out without the prune

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "잊혀진 들판", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text

    # The prune narrowed the per-candidate browser fan-out to the single correct
    # series — exactly ONE browser nav, to mr3m0's series page.
    assert slow_solver.browser_fetch_calls == [_FORGOTTEN_FIELD_URL], (
        slow_solver.browser_fetch_calls
    )
    body = resp.json()
    assert body.get("warnings") == []
    assert len(body["releases"]) == 1
    assert body["releases"][0]["mangaTitle"] == "The Forgotten Field"


@respx.mock
@pytest.mark.asyncio
async def test_real_search_main_title_prunes_browser_fanout_to_one(
    comix_client: httpx.AsyncClient, slow_solver: _SlowComixSolver
) -> None:
    """Parity (#126): the English MAIN title also prunes the REAL search to one.

    Same production path as the Korean case, proving the alt-title threading did
    not break the pre-existing main-title prune behavior."""
    _stage_forgotten_field(slow_solver)

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "The Forgotten Field", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text

    assert slow_solver.browser_fetch_calls == [_FORGOTTEN_FIELD_URL], (
        slow_solver.browser_fetch_calls
    )
    body = resp.json()
    assert body.get("warnings") == []
    assert len(body["releases"]) == 1
    assert body["releases"][0]["mangaTitle"] == "The Forgotten Field"
