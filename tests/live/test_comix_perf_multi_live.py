"""Comix multi-chapter perf — sequential-download regression guard (#45).

**History note.** This file was added during issue #23 to validate the
persistent-reader-page warm-call hypothesis (second+ chapters skipping
bundle parse + Swiper init + encrypted ``/api/v1/chapters/{id}`` fetch
~3-3.5s/call). The hypothesis was empirically refuted by this very test
on its first live run — ``page.goto(new_url)`` resets the JS execution
context regardless of page reuse, so the "warm bundle" benefit across
navigations does not exist (see
``.planning/debug/comix-warm-page-no-speedup.md``). Issue #23 was
abandoned without merging; there is no per-page warmth in the gateway.

The "cold vs warm" framing was vestigial after that and was removed in
the #45 cleanup. The persistent ``BrowserLifecycle`` context still
holds cf_clearance / HTTP cache / cookies — but it does so for EVERY
chapter call, not just the second+ ones. Each chapter download opens a
fresh page (``ctx.new_page()`` in ``fetch_via_browser``); there is no
position-dependent perf characteristic, only a chapter-size-dependent
one.

What the test asserts now:

* every chapter completes successfully;
* chapters 2..N land under ``_DEFAULT_PER_CHAPTER_BUDGET_SECONDS``
  (default 22.0 s — the steady-state regression guard, sized for the
  largest chapters observed); chapter 1 gets a larger cold allowance
  (``_FIRST_CHAPTER_COLD_BUDGET_SECONDS``, default 40.0 s) for the
  one-time headed-Chromium reader-pipeline boot;
* per-page wall-clock for chapters 2..N stays close to the first
  chapter's per-page wall-clock (``later_per_page_avg ≤ first_per_page
  x 1.30``) — catches a state-leak regression where the solver gets
  cumulatively slower per page across N downloads.

Like the rest of ``tests/live/``, this test:

* is marked ``@pytest.mark.live`` so the nox gate excludes it
  (``-m 'not live'`` in pyproject.toml);
* awaits ``solver.warm()`` explicitly before timing anything so the
  measured wall-clocks cover the post-warm() download path, not the
  initial Cloudflare solve;
* is path-agnostic (``tmp_path``) and env-knob-driven so a future
  nightly job reuses it unchanged.

Knobs:

* ``COMIX_PERF_QUERY`` — search query (default: "Nevermore"). Re-pointed
  off "Forgotten Field" in debug comix-concurrent-download-520 (#166):
  that query's top-3 releases included Chapter 23, whose CDN origin
  (j24n.wowpic5.store/i4/) is degraded and fails ~55% of page fetches at
  any concurrency. "Nevermore"'s newest chapters are served by the
  first-party WebToon CDN and download cleanly.
* ``COMIX_PERF_MULTI_CHAPTERS`` — chapter count to download serially
  (default 3 — first + two more; raise for stronger averaging).
* ``COMIX_PERF_PER_CHAPTER_BUDGET_SECONDS`` — steady-state wall-clock
  budget for chapters 2..N (default 22.0; sized for the largest chapter
  we've seen plus CI variance headroom).
* ``COMIX_PERF_FIRST_CHAPTER_BUDGET_SECONDS`` — cold budget for the
  first chapter under headed Chromium (default 40.0; covers the one-time
  reader-pipeline boot — see ``_FIRST_CHAPTER_COLD_BUDGET_SECONDS``).
* ``COMIX_PERF_BUDGET_OUTLIERS`` — how many over-budget chapters to
  tolerate before failing (default 1). comix image-CDN health is
  per-chapter, so a single degraded origin makes ONE chapter slow while
  ``resolving`` stays instant; a real sequential-download regression is
  systematic (multiple chapters + per-page drift). The tolerated outlier
  is also excluded from the per-page drift average.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from manga_gateway.app import create_app
from manga_gateway.config import Settings

pytestmark = pytest.mark.live  # excluded from the gate

# debug comix-concurrent-download-520 (#166): re-pointed off "Forgotten Field" —
# that query's top-3 releases included the degraded Chapter 23 (CDN origin
# j24n.wowpic5.store/i4/ fails ~55% of page fetches at ANY concurrency, including
# width=1, i.e. a genuinely broken upstream origin, not gateway load). "Nevermore"
# is a verified-healthy series whose newest chapters are served by the first-party
# WebToon CDN. CDN health is PER-CHAPTER, so if Nevermore's top chapters later
# rotate to a flaky origin this may need revisiting (override COMIX_PERF_QUERY).
_DEFAULT_QUERY = "Nevermore"
_TEST_API_KEY = "test-perf-comix-multi-key-DO-NOT-LOG-IN-PROD"

# Per-chapter budget — applied uniformly to every chapter in the run
# regardless of position. Post-#45 measurements: 10-page chapter
# ~11.4 s, 15-page chapter ~15.3 s. Issue #36 nightly (2026-05-31)
# saw a 15-page chapter land at 18.15 s on the Camoufox CI runner,
# tripping the old 18.0 budget by 0.15 s. 22.0 sits ~44 % above the
# largest observed wall-clock and well below the 60 s outer poll
# budget — leaves CI-variance headroom on the Azure-hosted runner
# without papering over a real regression.
# Issue #45 (2026-05-31): replaces the dead "cold 14.0 / warm 5.0" pair
# from the issue #23 design. Position-dependent budgets were a vestige
# of the persistent-reader-page hypothesis — there is no perf
# difference between chapter 1 and chapter N in the current gateway,
# only between SMALLER and LARGER chapters. One budget for any chapter,
# sized to the largest case, removes flakiness driven by search-result
# ordering (a 15-page chapter happening to land in position 1 would
# have broken the old tight cold budget).
_DEFAULT_PER_CHAPTER_BUDGET_SECONDS = 22.0

# Cold allowance for the FIRST chapter under HEADED Chromium (the datacenter/CI
# default since the cf-fingerprint-probe finding). Unlike the refuted #23
# gateway-warmth hypothesis above, this is a BROWSER-side one-time cost: the
# first chapter-reader download per browser session boots the comix reader
# pipeline (SPA + jsdefender VM bundle) once; chapters 2..N reuse it. Measured
# ~31s on the ubuntu-latest datacenter runner vs ~10-15s steady-state — and the
# cf-fingerprint-probe proved it is NOT Xvfb/headed/datacenter overhead (browser
# launch+warm 2.5s, page navs ~1s). Chapters 2..N keep the tight steady-state
# budget, which remains the real regression guard. Headless residential dev rarely
# trips this (the first chapter fits the 22s budget there), so this allowance only
# matters on the heavier headed path.
_FIRST_CHAPTER_COLD_BUDGET_SECONDS = 40.0

# Per-page drift guard. ``first_per_page = first_wall / first_pages`` is
# the baseline; ``later_per_page_avg = mean(wall / pages for chapters
# 2..N)``. The assertion ``later_per_page_avg ≤ first_per_page *
# _SUBSEQUENT_PER_PAGE_CEIL`` catches a state-leak regression where
# chapters AFTER the first start costing more per page than the first
# did (a real failure mode — solver state corruption, leaked browser
# resources, etc.). Post-#45 baseline: first 11.46/10 = 1.15 s/page;
# later avg (15.31/15 + 11.38/10)/2 = 1.08 s/page — so later costs
# ~0.94 x first per page. 1.30 leaves CI-variance headroom while
# catching a ~30 % per-page slowdown.
_SUBSEQUENT_PER_PAGE_CEIL = 1.30

# Absolute per-page floor for the drift guard. Since the comix-manifest-60s-timeout
# synthesis fix (#110), resolve is O(1) in pages, so per-page wall-clock is now
# tiny and dominated by image download — a first chapter can land at ~0.12 s/page.
# At that scale the relative 1.30x ceiling becomes ~0.16 s/page, where ordinary
# CI/network jitter on a later chapter (e.g. 0.16 s/page) trips a FALSE state-leak
# alarm. Floor the threshold at 0.20 s/page so the relative guard only governs
# runs where per-page cost is substantial; below that, absolute noise rules.
_SUBSEQUENT_PER_PAGE_FLOOR_S = 0.20

_DEFAULT_CHAPTERS = 3
_MIN_CHAPTERS = 2  # first + at least one more — anything less defeats the test

# Outer poll budget — well above the per-chapter budget so a regressed
# run still yields a measured number to report in the assertion, not a
# TimeoutError.
_TERMINAL_TIMEOUT_S = 60.0


def _query() -> str:
    return os.environ.get("COMIX_PERF_QUERY", _DEFAULT_QUERY)


def _positive_float(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # Reject inf/nan/non-positive (same hardening as test_comix_perf_live.py).
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def _per_chapter_budget_seconds() -> float:
    return _positive_float(
        "COMIX_PERF_PER_CHAPTER_BUDGET_SECONDS",
        _DEFAULT_PER_CHAPTER_BUDGET_SECONDS,
    )


def _first_chapter_cold_budget_seconds() -> float:
    return _positive_float(
        "COMIX_PERF_FIRST_CHAPTER_BUDGET_SECONDS",
        _FIRST_CHAPTER_COLD_BUDGET_SECONDS,
    )


def _chapter_count() -> int:
    raw = os.environ.get("COMIX_PERF_MULTI_CHAPTERS")
    if not raw:
        return _DEFAULT_CHAPTERS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_CHAPTERS
    if value < _MIN_CHAPTERS:
        return _MIN_CHAPTERS
    return value


def _headless() -> bool:
    return os.environ.get("COMIX_LIVE_HEADLESS", "1") != "0"


async def _poll_until_terminal(
    client: httpx.AsyncClient, job_id: str, *, timeout_s: float
) -> tuple[dict[str, Any], float, dict[str, float]]:
    """Poll ``GET /downloads`` until terminal; return job, elapsed, per-stage.

    Mirrors the helper in ``test_comix_perf_live.py`` (100 ms cadence,
    per-stage first-seen timestamps) so a multi-chapter regression points at
    the same bottleneck stage the single-submit guard does.
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


async def test_comix_multi_chapter_sequential_download(tmp_path: Path) -> None:
    """Sequential download of N chapters against one warm solver (#45).

    Search → submit the first N releases of the queried series serially →
    measure each download's wall-clock from ``POST /downloads`` to
    ``status: completed``. Asserts every chapter completes, every chapter
    lands under the per-chapter budget, and per-page wall-clock for
    chapters 2..N does not drift above first-chapter's per-page wall-clock
    by more than ``_SUBSEQUENT_PER_PAGE_CEIL``.

    The serial submit (vs queueing all N then polling) keeps the per-job
    measurement clean — ``fetch_via_browser`` is already serialized via
    ``_browser_lock`` so concurrency would just queue the same way, but
    interleaving status transitions would muddy the per-stage timing.
    """
    chapter_count = _chapter_count()
    per_chapter_budget = _per_chapter_budget_seconds()
    first_cold_budget = _first_chapter_cold_budget_seconds()

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
        # Await clearance before timing anything — the per-chapter budget
        # covers the post-warm() download path, not the initial Cloudflare
        # solve.
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
            # Search is OUTSIDE the timed window — the per-chapter budget
            # is the per-download wall-clock only.
            search = await client.post(
                "/api/v1/search",
                json={
                    "type": "chapter",
                    "query": _query(),
                    "sources": ["comix"],
                },
            )
            assert search.status_code == 200, (
                f"search failed: {search.status_code} {search.text[:400]}"
            )
            releases = search.json().get("releases") or []
            assert len(releases) >= chapter_count, (
                f"need >= {chapter_count} releases for query={_query()!r}; "
                f"got {len(releases)}. Override COMIX_PERF_QUERY with a "
                f"series that ships at least {chapter_count} chapters, or "
                f"lower COMIX_PERF_MULTI_CHAPTERS."
            )

            measurements: list[tuple[float, dict[str, float], int | None]] = []
            for idx, release in enumerate(releases[:chapter_count]):
                handle = release["downloadHandle"]
                wall_start = time.perf_counter()
                submit = await client.post(
                    "/api/v1/downloads",
                    json={"releaseHandle": handle, "sourceKey": "comix"},
                )
                assert submit.status_code == 200, (
                    f"chapter {idx + 1}/{chapter_count} submit failed: "
                    f"{submit.status_code} {submit.text[:400]}"
                )
                job_id = submit.json()["jobId"]

                job, _elapsed, stages = await _poll_until_terminal(
                    client, job_id, timeout_s=_TERMINAL_TIMEOUT_S
                )
                wall_elapsed = time.perf_counter() - wall_start
                assert job["status"] == "completed", (
                    f"chapter {idx + 1}/{chapter_count} did not complete: "
                    f"{job}; wall_elapsed={wall_elapsed:.2f}s stages={stages}"
                )
                measurements.append((wall_elapsed, stages, job.get("totalPages")))
                print(
                    f"\n[perf #45] chapter {idx + 1}/{chapter_count}: "
                    f"{wall_elapsed:.2f}s (pages={job.get('totalPages')})"
                )
                print(f"[perf #45] per-stage first-seen offsets: {stages}")

            walls = [m[0] for m in measurements]
            walls_avg = sum(walls) / len(walls)
            walls_max = max(walls)
            print(
                f"\n[perf #45] summary: per_chapter_avg={walls_avg:.2f}s "
                f"per_chapter_max={walls_max:.2f}s "
                f"(per_chapter_budget={per_chapter_budget:.2f}s)"
            )

            # Per-chapter budget. A SINGLE chapter blowing the budget is a known
            # per-chapter comix-CDN-origin flake: image-CDN health is per-chapter,
            # so a degraded origin makes one chapter's image fetch slow while
            # `resolving` stays instant (the comix-cdn-flaky-per-chapter class —
            # e.g. a 95-page chapter at 37s with resolving=0.001s is pure CDN
            # variance, not gateway orchestration). A real SEQUENTIAL-DOWNLOAD
            # regression is SYSTEMATIC — it slows MULTIPLE chapters and also shows
            # in the per-page drift guard below — so tolerate at most ONE
            # over-budget chapter (override COMIX_PERF_BUDGET_OUTLIERS) and fail on
            # two or more. Chapter 1 keeps its cold allowance.
            allowed_outliers = int(os.environ.get("COMIX_PERF_BUDGET_OUTLIERS", "1"))
            over_budget = [
                (
                    idx,
                    wall,
                    (first_cold_budget if idx == 1 else per_chapter_budget),
                    pages,
                    stages,
                )
                for idx, (wall, stages, pages) in enumerate(measurements, start=1)
                if wall >= (first_cold_budget if idx == 1 else per_chapter_budget)
            ]
            assert len(over_budget) <= allowed_outliers, (
                f"{len(over_budget)} of {chapter_count} chapters exceeded the "
                f"per-chapter budget (allowed CDN outliers={allowed_outliers}) — "
                f"systematic sequential-download regression (#45): "
                + "; ".join(
                    f"ch{i} {w:.2f}s>={b:.2f}s pages={p} stages={s}"
                    for i, w, b, p, s in over_budget
                )
            )
            # The single tolerated outlier (slowest over-budget chapter past the
            # first) is ALSO excluded from the per-page drift guard below, so one
            # CDN-degraded chapter cannot skew the later-chapter per-page average.
            later_over = [(i, w) for i, w, _b, _p, _s in over_budget if i >= 2]
            outlier_idx = max(later_over, key=lambda t: t[1])[0] if later_over else None
            if over_budget:
                print(
                    f"[perf #45] tolerated {len(over_budget)} CDN-outlier "
                    f"chapter(s) (<= allowed {allowed_outliers}); "
                    f"drift-guard excludes ch{outlier_idx}"
                )

            # Per-page drift guard — catches a state-leak regression where
            # chapters 2..N start costing more per page than the first did.
            # Post-#45 baseline: first ~1.15 s/page; later avg ~1.08 s/page
            # (later ≈ 0.94 x first). 1.30 leaves CI-variance headroom while
            # catching a ~30 % per-page slowdown across the run.
            first_wall, _first_stages, first_pages = measurements[0]
            assert first_pages and first_pages > 0, (
                f"first chapter reported totalPages={first_pages!r}; "
                f"per-page normalization undefined"
            )
            first_per_page = first_wall / first_pages
            later_per_page_values: list[float] = []
            for j, (wall, _stages, pages) in enumerate(measurements[1:], start=2):
                if j == outlier_idx:  # exclude the single tolerated CDN-outlier
                    continue
                if not pages or pages <= 0:
                    continue
                later_per_page_values.append(wall / pages)
            if not later_per_page_values:
                # Only happens when the sole later chapter WAS the tolerated CDN
                # outlier (e.g. 2 chapters, ch2 degraded) — there is nothing left to
                # assess drift against, so skip rather than fail (the budget guard
                # already tolerated it).
                print(
                    "[perf #45] per-page drift guard skipped — the only "
                    "later chapter was the tolerated CDN outlier"
                )
                return
            later_per_page_avg = sum(later_per_page_values) / len(later_per_page_values)
            # Relative state-leak guard, floored at an absolute 0.20 s/page so a
            # tiny post-synthesis baseline can't make ordinary jitter trip it.
            drift_threshold = max(
                first_per_page * _SUBSEQUENT_PER_PAGE_CEIL,
                _SUBSEQUENT_PER_PAGE_FLOOR_S,
            )
            assert later_per_page_avg <= drift_threshold, (
                f"later-chapter per-page avg {later_per_page_avg:.2f}s/page exceeds "
                f"threshold {drift_threshold:.2f}s/page (max of "
                f"{_SUBSEQUENT_PER_PAGE_CEIL:.2f}x first {first_per_page:.2f}s/page "
                f"and {_SUBSEQUENT_PER_PAGE_FLOOR_S:.2f}s/page floor); chapters past "
                f"the first pay per-page overhead the first avoided — solver state "
                f"may be leaking (#45)"
            )
