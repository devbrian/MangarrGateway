"""Comix ``search()`` parallel-fan-out behavior (debug ``comix-search-timeout``).

The per-candidate ``_series_chapters`` browser navigations are awaited
CONCURRENTLY via :func:`asyncio.gather` so /search's wall-clock is bounded by
``max(individual)`` rather than ``sum(individual)``. The framework's 20s
per-source fan-out budget was being eaten by the sum (3-5 × ~5s Camoufox navs)
and tipping under variance in nightly run ``26726259461`` (2026-05-31 22:26
UTC). The fix:

* ``ComixSource.search()`` builds a list of ``_series_chapters`` coroutines
  and awaits them via ``asyncio.gather(*coros, return_exceptions=True)``;
  per-candidate failures are isolated (logged + skipped), the survivors flow.
* ``CloudflareSolver._browser_lock`` was downgraded from a 1-wide
  ``asyncio.Lock`` to a bounded ``asyncio.Semaphore`` (default 5 slots,
  matching ``_DEFAULT_SERIES_CANDIDATES``) so the default candidate count
  runs fully in parallel; the bound still caps the simultaneous-page
  fingerprint footprint.

Tests:
* (a) ALL 5 candidates' chapters are returned in candidate order — the
  ``return_exceptions=True`` gather preserves ordering even when individual
  coros complete out-of-order.
* (b) Per-candidate failure does NOT nuke the rest — a single SourceError
  on candidate #3 still yields chapters from candidates #1/#2/#4/#5.
* (c) Wall-clock is bounded by ``max(individual)`` not ``sum`` — five
  navs each sleeping 0.1s complete in well under 0.5s (with margin).
* (d) Order is preserved when navs complete OUT OF the launch order.

Driven by the ``_ComixSolver`` fake from ``test_comix_slice``-style fakes —
no real browser, no network, fully deterministic.
"""

from __future__ import annotations

import asyncio
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
    fetches run in PARALLEL, the gathered wall-clock is ~0.1s, NOT 0.5s. If
    a regression re-introduces the 1-wide Lock OR a sequential ``for`` loop,
    the test sees a wall-clock close to 0.5s and fails.

    Optionally returns an exception on a given URL (instead of the staged
    result) to drive the per-candidate-failure-isolation test.
    """

    def __init__(self) -> None:
        self.browser_results: dict[str, object] = {}
        self.browser_errors: dict[str, Exception] = {}
        self.browser_delays: dict[str, float] = {}
        self.browser_fetch_calls: list[str] = []
        # Cross-call concurrency observability — the parallel test asserts
        # ``max_in_flight > 1`` so a regression to the sequential ``for`` loop
        # (where in-flight is always 1) is caught even when total wall-clock
        # variance is small.
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
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.browser_delays.get(url, 0.0))
            if url in self.browser_errors:
                raise self.browser_errors[url]
            if url not in self.browser_results:
                raise AssertionError(f"unmocked fetch_via_browser({url!r})")
            return self.browser_results[url]
        finally:
            self.in_flight -= 1


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
    """Mock the upstream /api/v1/manga to return 5 candidate series and stage
    a 1-chapter result for each at ``delays[i]`` seconds.

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
    # Per-source fan-out warning is NOT emitted by the source itself (it's
    # only emitted by the framework on a whole-source timeout/SourceError) —
    # a per-CANDIDATE failure stays internal to ComixSource.search() and is
    # logged at WARNING. The /search response is still 200 with releases.
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
    candidate finishes LAST. ``asyncio.gather`` preserves coro-launch order
    in its result list, so the response ordering still matches the relevance
    order (which is also the launch order). This catches a future drift to
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
