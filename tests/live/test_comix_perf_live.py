"""Comix download perf — wall-clock POST /downloads → status: completed (#20).

PR #18's Postman walk-through measured a ~25 s end-to-end Comix download with
nearly all the time (~22 s) inside the `resolving` stage's browser-DOM page-list
extraction. This test is the regression guard for the parallel-watcher rewrite
of ``_CHAPTER_PAGES_EXTRACT_JS`` (issue #20) and the related ``goto`` /
``wait_for`` tightening.

**Budget interpretation (issue #45, 2026-05-31 revision).**

History:

* Original budget was 8 s, set against a measurement that — we now know —
  was silently incorrect: the parallel-scrollIntoView extractor only
  captured 2-4 of the chapter's actual pages (head + tail). Issue #32
  revealed that ``scrollIntoView`` in a tight loop is a synchronous
  viewport mutation; only the FINAL element's scroll position is honored.
  The original 8 s "perf win" was really a "shipping incomplete CBZs
  fast" bug.
* Issue #32 fix walked pages SEQUENTIALLY — scroll → ~150 ms settle →
  poll per page — accepting an O(pages) wall-clock term in exchange for
  correctness. Measured against a 10-page chapter (real Comix, warm
  solver) that landed at ~14-15 s. Budget was bumped to 20 s.
* Issue #45 rewrites Step 2 of the extractor as a two-scroll head+tail
  walk on the INNER Swiper scroll container (an ancestor of the
  ``.rpage-page`` divs with ``overflow:auto|scroll|hidden`` and
  ``scrollHeight > clientHeight``), with a selective per-missing-page
  ``scrollIntoView`` fallback for any page the two-scroll walk fails to
  capture. The empirical basis is the spike
  ``.planning/debug/comix-warm-page-no-speedup-spike.md``: a tall window
  viewport loaded pages 1, 2, 13, AND 14 simultaneously while pages
  3-12 stayed lazy, proving the inner Swiper container is what gates
  middle pages independently of the window viewport. Two batched scrolls
  on that container — to ``scrollHeight/2`` then ``scrollHeight`` — fire
  the middle and tail bands in ~2 settle windows of 500 ms each.

New budget: **15.5 s** (default). Three back-to-back live runs on a
10-page chapter (real Comix, warm solver, headless) measured 11.42 s,
12.74 s, and 11.56 s end-to-end (resolving stage ~11.0 s — the extractor
cost; the remainder is downloading + archiving). 15.5 s is the max
observed (12.74 s) x ~1.21 — ~21 % headroom over the worst run, still
well below the prior 20 s ceiling. Correctness is preserved:
``totalPages`` matched the pre-rewrite baseline (10/10 pages, no drops,
no out-of-order) in every run, and the issue #32 final
``document.querySelectorAll('img')`` sweep is retained verbatim in the
extractor as the silent-truncation safety net.

``COMIX_PERF_BUDGET_SECONDS`` still lets the bar be tightened when
further optimizations land (e.g. a persistent reader page that skips
the bundle parse — tracked separately).

Like the rest of ``tests/live/``, this test:

* is marked ``@pytest.mark.live`` so the nox gate excludes it (``-m 'not live'``
  in pyproject.toml);
* awaits ``solver.warm()`` explicitly before issuing requests so the first
  ``fetch_via_browser`` doesn't race the initial Cloudflare solve;
* is path-agnostic (``tmp_path``) and env-knob-driven so a future nightly job
  reuses it unchanged.

**Chapter pinning — no live search (issue #181, 2026-06-08).**

The test originally measured ``releases[0]`` from a live ``POST /search`` —
whichever chapter comix returned first (newest). That made the measured chapter
NON-DETERMINISTIC: a nightly run landed on "Chapter 24 [Vortex Scans]", whose
image CDN origin was degraded (pages 14+ returned ``upstream 404``), so the
download ``failed`` at 13/24 pages and the test failed — an upstream/origin
flake, not a gateway perf regression.

Pinning a fixed chapter THROUGH search is infeasible: comix search only surfaces
the few newest chapters (observed: 21-24), and that window slides forward as the
series releases, so an older known-good chapter is simply not reachable via the
search path. But this test does not need search at all — search has its own
smoke coverage (``test_search_returns_releases``/``test_search_alt_title_resolves``)
and was already OUTSIDE this test's timed window (the budget is the
``POST /downloads`` → ``completed`` wall-clock only). So the test now MINTS a
download handle DIRECTLY for the same known-good chapter the drift fixture is
recorded against (``mr3m0`` "The Forgotten Field" chapter 20, numeric chapter id
9001596, 17 pages — see ``tests/fixtures/comix/chapter_9001596_pages.json``, the
same chapter ``test_fixture_drift`` navigates) and submits THAT, exercising the
identical resolve → manifest → image-fetch → archive download path with zero
search-path or sliding-window flakiness. The composite resolves to the exact
chapter URL the drift test uses, so the two live comix tests now pin one stable
chapter together.

Knobs:

* ``COMIX_PERF_BUDGET_SECONDS`` — wall-clock budget override (default 15.5,
  the post-issue-#45 measured-derived regression guard; tighten as further
  optimizations land).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.handles.store import ResolutionRecord
from manga_gateway.sources.comix import ComixSource

pytestmark = pytest.mark.live  # excluded from the gate

_TEST_API_KEY = "test-perf-comix-key-DO-NOT-LOG-IN-PROD"

# Issue #181: the pinned known-good chapter — the SAME chapter the drift fixture
# is recorded against and ``test_fixture_drift`` navigates: ``mr3m0`` "The
# Forgotten Field" chapter 20, numeric chapter id 9001596, 17 pages
# (tests/fixtures/comix/chapter_9001596_pages.json). The composite resolves to
# ``{base_url}/title/mr3m0-the-forgotten-field/9001596-chapter-20`` in
# ``fetch_manifest`` — no live search, so the sliding newest-chapter window
# (and its per-chapter CDN flakiness) can never sink this perf measurement.
_PIN_NUMERIC_ID = "9001596"
_PIN_HID = "mr3m0"
_PIN_SLUG = "the-forgotten-field"
_PIN_CHAPTER = "20"
_PIN_TITLE = "The Forgotten Field - Chapter 20 (en) [Vortex Scans]"
_PIN_MANGA = "The Forgotten Field"
_PIN_GROUP = "Vortex Scans"
_PIN_PAGE_COUNT = 17
# Issue #45 (2026-05-31): tightened from 20.0 → 15.5. The 20.0 figure was
# anchored on the issue #32 sequential walk's ~14-15 s wall-clock; the
# issue #45 two-scroll head+tail extractor (inner Swiper container
# scrollTo(scrollHeight/2) then scrollTo(scrollHeight) with 500 ms settles,
# plus a selective per-missing-page scrollIntoView fallback) measured
# 11.42 s / 12.74 s / 11.56 s over three back-to-back live runs on a
# 10-page chapter. 15.5 s = max-observed (12.74) x ~1.21, ~21 % headroom
# over the worst run.
_DEFAULT_BUDGET_SECONDS = 15.5
# Outer poll budget — well above the perf budget so a regressed run still
# yields a measured number to report in the assertion, not a TimeoutError.
_TERMINAL_TIMEOUT_S = 60.0


def _pinned_record() -> ResolutionRecord:
    """The ResolutionRecord for the pinned chapter (issue #181).

    Builds the resolved composite ``chapter_id``
    (``9001596|mr3m0|the-forgotten-field|20``) that ``ComixSource.fetch_manifest``
    reconstructs into the live chapter URL the drift test navigates. Minting this
    directly into the handle store lets the perf test exercise the full download
    path with NO live search — the title/group/page_count fields are advisory
    (job display only) and do not affect the resolve/fetch the budget measures.
    """
    composite = ComixSource._make_composite_chapter_id(
        _PIN_NUMERIC_ID, _PIN_HID, _PIN_SLUG, _PIN_CHAPTER
    )
    return ResolutionRecord(
        source_key="comix",
        chapter_id=composite,
        language="en",
        title=_PIN_TITLE,
        manga_title=_PIN_MANGA,
        chapter_number=Decimal(_PIN_CHAPTER),
        volume=None,
        scanlation_group=_PIN_GROUP,
        page_count=_PIN_PAGE_COUNT,
    )


def _budget_seconds() -> float:
    raw = os.environ.get("COMIX_PERF_BUDGET_SECONDS")
    if not raw:
        return _DEFAULT_BUDGET_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_BUDGET_SECONDS
    # Reject inf/nan/non-positive: an `inf` override would silently disable the
    # perf guard, a `nan` would make the assertion always false, and a negative
    # value would make it always fail without diagnostic value.
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_BUDGET_SECONDS
    return value


def _headless() -> bool:
    return os.environ.get("COMIX_LIVE_HEADLESS", "1") != "0"


async def _poll_until_terminal(
    client: httpx.AsyncClient, job_id: str, *, timeout_s: float
) -> tuple[dict[str, Any], float, dict[str, float]]:
    """Poll ``GET /downloads`` until terminal; return job, elapsed, per-stage.

    Polls at 100 ms cadence (much tighter than the e2e test's 1 s) so the
    measured wall-clock is dominated by the gateway's work, not by poll
    granularity. Also captures per-stage transition timestamps so a perf
    regression points at the bottleneck stage, not just the overall budget.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + timeout_s
    stage_first_seen: dict[str, float] = {}
    while loop.time() < deadline:
        resp = await client.get("/api/v1/downloads")
        resp.raise_for_status()
        jobs = resp.json()["jobs"]
        job = next((j for j in jobs if j["jobId"] == job_id), None)
        if job is not None:
            status = job["status"]
            stage_first_seen.setdefault(status, loop.time() - start)
            if status in ("completed", "failed"):
                return job, loop.time() - start, stage_first_seen
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"job {job_id} did not terminate within {timeout_s}s; "
        f"stages seen: {stage_first_seen}"
    )


async def test_comix_warm_download_under_perf_budget(tmp_path: Path) -> None:
    """Warm-solver Comix download must complete under the regression budget.

    Mint a handle for the PINNED known-good chapter (issue #181 — no live
    search) → submit → measure ``POST /downloads`` → first ``status: completed``
    observation. Asserts the elapsed wall-clock is under :func:`_budget_seconds`
    (default ``_DEFAULT_BUDGET_SECONDS`` = 15.5 s, the post-issue-#45
    measured-derived regression guard for the two-scroll inner-Swiper extractor;
    tighten further when the persistent-reader-page follow-up lands).
    """
    output_root = tmp_path / "out"
    await asyncio.to_thread(output_root.mkdir)

    settings = Settings(
        api_key=_TEST_API_KEY,
        output_root=str(output_root),
        db_path=str(tmp_path / "jobs.db"),
        cloudflare_headless=_headless(),
    )
    app = create_app(settings)

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        # Await clearance before timing anything — the perf budget covers the
        # download path on a warm solver, not the initial Cloudflare solve.
        # #196: scope to comix only — solver.warm() eager-solves every cloudflare
        # key incl. kagane.to, which never clears in CI and burns its full 60s
        # (#197/#198), blowing this ceiling. get_clearance("comix") solves just
        # this host.
        solver = app.state.solver
        await asyncio.wait_for(solver.get_clearance("comix"), timeout=60.0)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers={"X-Api-Key": _TEST_API_KEY},
            timeout=120.0,
        ) as client:
            # Issue #181: NO live search — mint a handle for the pinned
            # known-good chapter directly into the same handle store the
            # download route resolves against (app.state.handle_store). Search
            # has its own smoke coverage and was already outside the timed
            # window; minting here removes the sliding newest-chapter window
            # (and its per-chapter CDN flakiness) that sank the nightly with an
            # `upstream 404` partway through a degraded newest chapter. The
            # resolve → manifest → image-fetch → archive path the budget
            # measures is byte-for-byte the production download path.
            handle = app.state.handle_store.mint(_pinned_record())

            # Start the clock immediately before the submit so the budget
            # captures the SAME path the issue's baseline measured: submit
            # → resolving (browser-DOM extract) → downloading → archiving.
            wall_start = time.perf_counter()
            submit = await client.post(
                "/api/v1/downloads",
                json={"releaseHandle": handle, "sourceKey": "comix"},
            )
            assert submit.status_code == 200, (
                f"submit failed: {submit.status_code} {submit.text[:400]}"
            )
            job_id = submit.json()["jobId"]

            job, elapsed, stages = await _poll_until_terminal(
                client, job_id, timeout_s=_TERMINAL_TIMEOUT_S
            )
            wall_elapsed = time.perf_counter() - wall_start
            assert job["status"] == "completed", (
                f"download did not complete: {job}; "
                f"wall_elapsed={wall_elapsed:.2f}s stages={stages}"
            )

            budget = _budget_seconds()
            # Print the per-stage breakdown so a regression points at the
            # bottleneck stage. With the issue #45 two-scroll head+tail
            # extractor the `resolving` stage is still dominant but now
            # roughly constant in chapter page count (two batched scrolls
            # + ~500 ms settles, plus selective per-missing-page fallback);
            # `downloading` and `archiving` are near-instant in comparison.
            pages = job.get("totalPages")
            print(
                f"\n[perf #20] warm-solver download wall-clock: "
                f"{wall_elapsed:.2f}s (budget {budget:.2f}s, pages={pages})"
            )
            print(f"[perf #20] per-stage first-seen offsets: {stages}")
            assert wall_elapsed < budget, (
                f"warm-solver Comix download took {wall_elapsed:.2f}s "
                f"(budget {budget:.2f}s, pages={pages}); stages={stages}; "
                f"regression vs #20"
            )
