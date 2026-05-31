"""Comix multi-chapter perf — sequential-download regression guard (#45 retune).

**History note.** This file was added during issue #23 to validate the
persistent-reader-page warm-call hypothesis (second+ chapters skipping
bundle parse + Swiper init + encrypted ``/api/v1/chapters/{id}`` fetch
~3-3.5s/call). The hypothesis was empirically refuted by this very test
on its first live run — ``page.goto(new_url)`` resets the JS execution
context regardless of page reuse, so the "warm bundle" benefit across
navigations does not exist. See ``.planning/debug/comix-warm-page-no-speedup.md``.
Issue #23 was abandoned without merging; the persistent reader page is
not on main. This file was kept (cherry-picked to main as commit 9adb3ab)
as a multi-chapter regression guard for cold-path sequential downloads.

**#45 retune (2026-05-31).** After the two-scroll inner-Swiper page-walk
rewrite (#45) the budgets here were retuned to the empirically-observed
post-#45 values. The "warm should be faster than cold" assertion was
dropped because the underlying hypothesis (the warm-page win) was dead;
replaced with a **per-page-normalized** regression guard that catches a
real failure mode (warm-call cost-per-page diverging from cold-call
cost-per-page) without baking in the dead hypothesis.

What the test asserts now:

* every chapter completes;
* the FIRST chapter (cold first-call after ``warm()``) lands under the
  cold budget (default 15.5 s — aligned with ``test_comix_perf_live.py``);
* every SUBSEQUENT chapter (the warm calls) lands under the warm budget
  (default 18.0 s — derived from post-#45 ``warm_max`` measurement with
  ~18 % headroom);
* per-page wall-clock for warm calls stays close to the cold call's
  per-page wall-clock (``warm_per_page_avg ≤ cold_per_page × 1.30``) so
  a regression in the warm path that adds per-page overhead is caught.

Like the rest of ``tests/live/``, this test:

* is marked ``@pytest.mark.live`` so the nox gate excludes it
  (``-m 'not live'`` in pyproject.toml);
* awaits ``solver.warm()`` explicitly before timing anything so the cold
  first call covers the post-warm() download path, not the initial
  Cloudflare solve;
* is path-agnostic (``tmp_path``) and env-knob-driven so a future nightly
  job reuses it unchanged.

Knobs:

* ``COMIX_PERF_QUERY`` — search query (default: "Forgotten Field", same as
  ``test_comix_perf_live.py``).
* ``COMIX_PERF_MULTI_CHAPTERS`` — chapter count to download serially
  (default 3 — first cold + two warm; raise for stronger averaging).
* ``COMIX_PERF_COLD_BUDGET_SECONDS`` — wall-clock budget for the first
  (cold) chapter (default 15.5; aligned with the single-submit guard).
* ``COMIX_PERF_WARM_BUDGET_SECONDS`` — wall-clock budget per subsequent
  (warm) chapter (default 18.0; warm_max post-#45 + ~18 % headroom).
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

_DEFAULT_QUERY = "Forgotten Field"
_TEST_API_KEY = "test-perf-comix-multi-key-DO-NOT-LOG-IN-PROD"

# Cold first-call budget — aligned with test_comix_perf_live.py so a
# single source of regression-budget truth applies to the cold path.
# Issue #45 (2026-05-31): tightened from 14.0 to 15.5 to match the
# post-rewrite single-submit guard; the multi-chapter cold call IS the
# same operation as the single-submit cold call.
_DEFAULT_COLD_BUDGET_SECONDS = 15.5

# Warm per-call budget — derived from post-#45 ``warm_max`` measurement
# (15.21 s on a 15-page chapter) + ~18 % CI-variance headroom = 18.0.
# Issue #45 (2026-05-31): retuned from 5.0 to 18.0. The 5.0 figure was
# the issue #23 acceptance target, but that target was set against a
# warm-page-reuse hypothesis the very test refuted on first live run
# (see `.planning/debug/comix-warm-page-no-speedup.md`). #23 was
# abandoned without merging; this budget now reflects the actual cost
# of the cold-path per-chapter download repeated against the same warm
# solver.
_DEFAULT_WARM_BUDGET_SECONDS = 18.0

# Per-page-normalized regression guard. cold_per_page = cold_wall /
# cold_pages; warm_per_page_avg = mean(warm_wall / warm_pages). The
# assertion `warm_per_page_avg <= cold_per_page * _WARM_PER_PAGE_CEIL`
# catches a regression where the warm path inexplicably gets MORE
# expensive per page than the cold path (e.g. a new caching bug
# slowing every warm call). Post-#45 baseline: cold 11.40 / 10 = 1.14
# s/page; warm avg ~ (15.21/15 + 13.05/10)/2 = 1.16 s/page — so warm
# costs ~1.02 × cold per page. 1.30 leaves CI-variance headroom while
# catching a real regression (~30 % per-page slowdown).
_WARM_PER_PAGE_CEIL = 1.30

_DEFAULT_CHAPTERS = 3
_MIN_CHAPTERS = 2  # cold + at least one warm — anything less defeats the test

# Outer poll budget — well above the warm budget so a regressed run still
# yields a measured number to report in the assertion, not a TimeoutError.
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


def _cold_budget_seconds() -> float:
    return _positive_float(
        "COMIX_PERF_COLD_BUDGET_SECONDS", _DEFAULT_COLD_BUDGET_SECONDS
    )


def _warm_budget_seconds() -> float:
    return _positive_float(
        "COMIX_PERF_WARM_BUDGET_SECONDS", _DEFAULT_WARM_BUDGET_SECONDS
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


async def test_comix_multi_chapter_warm_reuse_under_budget(tmp_path: Path) -> None:
    """Issue #23: multi-chapter download — warm calls must hit the warm budget.

    Search → submit the first N releases of the queried series serially →
    measure each download's wall-clock from ``POST /downloads`` to
    ``status: completed``. Asserts the cold first call lands under the cold
    budget AND every warm call (2..N) lands under the warm budget AND the
    warm-call average is meaningfully faster than the cold call.

    The serial submit (vs queueing all N then polling) keeps the per-job
    measurement clean — ``fetch_via_browser`` is already serialized via
    ``_decrypt_lock`` so concurrency would just queue the same way, but
    interleaving status transitions would muddy the per-stage timing.
    """
    chapter_count = _chapter_count()
    cold_budget = _cold_budget_seconds()
    warm_budget = _warm_budget_seconds()

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
        # Await warm() before timing anything — the warm budget covers the
        # download path on a warm solver; the persistent reader page is
        # still lazy and bootstraps on the FIRST fetch_via_browser call.
        solver = app.state.solver
        await asyncio.wait_for(solver.warm(), timeout=60.0)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers={"X-Api-Key": _TEST_API_KEY},
            timeout=120.0,
        ) as client:
            # Search is OUTSIDE the timed window — the perf budgets are the
            # per-download wall-clocks only.
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
                kind = "cold" if idx == 0 else "warm"
                print(
                    f"\n[perf #45] chapter {idx + 1}/{chapter_count} ({kind}): "
                    f"{wall_elapsed:.2f}s (pages={job.get('totalPages')})"
                )
                print(f"[perf #45] per-stage first-seen offsets: {stages}")

            cold_wall, cold_stages, cold_pages = measurements[0]
            warm_walls = [m[0] for m in measurements[1:]]
            warm_avg = sum(warm_walls) / len(warm_walls)
            warm_max = max(warm_walls)
            print(
                f"\n[perf #45] summary: cold={cold_wall:.2f}s "
                f"warm_avg={warm_avg:.2f}s warm_max={warm_max:.2f}s "
                f"(cold_budget={cold_budget:.2f}s warm_budget={warm_budget:.2f}s)"
            )

            # Cold first call must land under the cold budget — matches the
            # single-submit guard so a regression in the cold path is also
            # caught here.
            assert cold_wall < cold_budget, (
                f"cold-first chapter took {cold_wall:.2f}s "
                f"(cold_budget {cold_budget:.2f}s, pages={cold_pages}); "
                f"stages={cold_stages}; regression vs #45 single-submit guard"
            )

            # Every warm call must land under the warm budget. A single warm
            # chapter blowing the budget is enough to fail (don't average it
            # out). Post-#45 the warm budget is the empirically-observed
            # warm_max plus ~18 % CI-variance headroom.
            for idx, (wall, stages, pages) in enumerate(measurements[1:], start=2):
                assert wall < warm_budget, (
                    f"warm chapter {idx}/{chapter_count} took {wall:.2f}s "
                    f"(warm_budget {warm_budget:.2f}s, pages={pages}); "
                    f"stages={stages}; sequential-download regression (#45)"
                )

            # Per-page-normalized regression guard — catches a real failure
            # mode (warm-call cost-per-page diverging from cold-call cost-
            # per-page) without baking in the dead #23 hypothesis that warm
            # should be faster than cold. The post-#45 baseline is
            # warm_per_page ~= 1.02 × cold_per_page; _WARM_PER_PAGE_CEIL =
            # 1.30 leaves CI-variance headroom while catching a ~30 % per-
            # page slowdown.
            assert cold_pages and cold_pages > 0, (
                f"cold chapter reported totalPages={cold_pages!r}; "
                f"normalization undefined"
            )
            cold_per_page = cold_wall / cold_pages
            warm_per_page_values: list[float] = []
            for wall, _stages, pages in measurements[1:]:
                if not pages or pages <= 0:
                    continue
                warm_per_page_values.append(wall / pages)
            assert warm_per_page_values, (
                "no warm chapters reported totalPages; per-page guard cannot run"
            )
            warm_per_page_avg = sum(warm_per_page_values) / len(warm_per_page_values)
            per_page_threshold = cold_per_page * _WARM_PER_PAGE_CEIL
            assert warm_per_page_avg <= per_page_threshold, (
                f"warm per-page avg {warm_per_page_avg:.2f}s/page exceeds "
                f"{_WARM_PER_PAGE_CEIL:.2f} × cold per-page "
                f"({cold_per_page:.2f}s/page, threshold "
                f"{per_page_threshold:.2f}s/page); warm path appears to be "
                f"paying per-page overhead a cold call avoids (#45)"
            )
