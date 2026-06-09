"""Live: two CONCURRENT comix downloads complete (issue #71, item 5 — nightly).

The live counterpart to the in-process concurrency witnesses. Where
``test_comix_perf_multi_live.py`` downloads N chapters SERIALLY, this test
submits two chapters and lets them run CONCURRENTLY through the real
``JobEngine`` against a real warm Patchright/Chromium solver — exercising the
production parallel-download path end to end:

* ``max_concurrent_chapters`` >= 2 and ``max_concurrent_per_source`` >= 2 so
  both comix jobs are admitted at once (the default per-source cap is 1, which
  would serialize them — this test deliberately widens it);
* both manifest-resolve browser navs contend on the same
  ``CloudflareSolver._browser_lock = Semaphore(cloudflare_fetch_concurrency)``
  the parallel-search fan-out uses (default 3 under patchright);
* both jobs must reach ``completed`` and produce a valid CBZ on disk.

Like the rest of ``tests/live/`` this is ``@pytest.mark.live`` (excluded from
the nox gate, ``-m 'not live'``), awaits ``solver.warm()`` before timing, and
is env-knob-driven so a nightly job reuses it unchanged.

Knobs:
* ``COMIX_CONCURRENT_QUERY`` — search query (default "Nevermore", same as the
  perf live tests so it resolves a series with >= 2 chapters). Re-pointed off
  "Forgotten Field" in debug comix-concurrent-download-520 (#166): that query's
  top releases included Chapter 23, whose CDN origin (j24n.wowpic5.store/i4/) is
  degraded and fails ~55% of page fetches at any concurrency. "Nevermore"'s
  newest chapters are served by the first-party WebToon CDN and download cleanly.
* ``COMIX_CONCURRENT_TIMEOUT_SECONDS`` — terminal-poll budget per job
  (default 90.0 — generous; both jobs share the warm solver + browser gate).
* ``COMIX_LIVE_HEADLESS`` — "0" to run headed (datacenter/CI fingerprint path).
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from manga_gateway.app import create_app
from manga_gateway.config import Settings

from ._helpers import _assert_cbz_on_disk, _poll_until_terminal

pytestmark = pytest.mark.live  # excluded from the gate

# debug comix-concurrent-download-520 (#166): re-pointed off "Forgotten Field" —
# that query's top releases included the degraded Chapter 23 (CDN origin
# j24n.wowpic5.store/i4/ fails ~55% of page fetches at ANY concurrency, including
# width=1, i.e. a genuinely broken upstream origin, not gateway load). "Nevermore"
# is a verified-healthy series whose newest chapters are served by the first-party
# WebToon CDN. CDN health is PER-CHAPTER, so if Nevermore's top chapters later
# rotate to a flaky origin this may need revisiting (override COMIX_CONCURRENT_QUERY).
_DEFAULT_QUERY = "Nevermore"
_TEST_API_KEY = "test-comix-concurrent-live-key-DO-NOT-LOG-IN-PROD"
_DEFAULT_TIMEOUT_S = 90.0
_CONCURRENT_JOBS = 2


def _query() -> str:
    return os.environ.get("COMIX_CONCURRENT_QUERY", _DEFAULT_QUERY)


def _timeout_seconds() -> float:
    raw = os.environ.get("COMIX_CONCURRENT_TIMEOUT_SECONDS")
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_TIMEOUT_S
    return value


def _headless() -> bool:
    return os.environ.get("COMIX_LIVE_HEADLESS", "1") != "0"


async def test_two_comix_downloads_run_concurrently(tmp_path: Path) -> None:
    """Submit two comix chapters, let them run concurrently, assert both complete.

    Both jobs are submitted back-to-back (no wait between submits) and then
    polled concurrently to terminal. With ``max_concurrent_per_source=2`` the
    JobManager admits both at once; the real engine runs both lifecycles
    overlapping, their manifest-resolve navs sharing the warm browser gate.
    Success criterion: both jobs ``completed`` with a valid CBZ on disk.
    """
    output_root = tmp_path / "out"
    await asyncio.to_thread(output_root.mkdir, parents=True, exist_ok=True)

    settings = Settings(
        api_key=_TEST_API_KEY,
        output_root=str(output_root),
        db_path=str(tmp_path / "jobs.db"),
        cloudflare_headless=_headless(),
        # Widen the caps so both comix jobs are admitted concurrently (the
        # default per-source cap of 1 would serialize them — this test is about
        # the parallel path).
        max_concurrent_chapters=_CONCURRENT_JOBS,
        max_concurrent_per_source=_CONCURRENT_JOBS,
    )
    app = create_app(settings)

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        solver = app.state.solver
        # #196: warm ONLY comix (the source under test), not the whole solver.
        # solver.warm() eager-solves EVERY cloudflare key — incl. kagane.to,
        # which never clears in CI and burns its full 60s (#197/#198) — blowing
        # this outer ceiling and dragging comix red. get_clearance("comix")
        # solves just this host.
        await asyncio.wait_for(solver.get_clearance("comix"), timeout=60.0)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers={"X-Api-Key": _TEST_API_KEY},
            timeout=120.0,
        ) as client:
            search = await client.post(
                "/api/v1/search",
                json={"type": "chapter", "query": _query(), "sources": ["comix"]},
            )
            assert search.status_code == 200, (
                f"search failed: {search.status_code} {search.text[:400]}"
            )
            releases = search.json().get("releases") or []
            assert len(releases) >= _CONCURRENT_JOBS, (
                f"need >= {_CONCURRENT_JOBS} releases for query={_query()!r}; "
                f"got {len(releases)}. Override COMIX_CONCURRENT_QUERY with a "
                f"series shipping at least {_CONCURRENT_JOBS} chapters."
            )

            # Submit both back-to-back — distinct handles ⇒ no idempotent collapse.
            job_ids: list[str] = []
            for release in releases[:_CONCURRENT_JOBS]:
                body = {
                    "releaseHandle": release["downloadHandle"],
                    "sourceKey": "comix",
                }
                submit = await client.post("/api/v1/downloads", json=body)
                assert submit.status_code == 200, (
                    f"submit failed: {submit.status_code} {submit.text[:400]}"
                )
                job_id = submit.json().get("jobId")
                assert job_id is not None, f"null jobId: {submit.json()}"
                job_ids.append(job_id)

            assert len(set(job_ids)) == _CONCURRENT_JOBS, (
                f"expected {_CONCURRENT_JOBS} distinct jobIds, got {job_ids}"
            )

            # Poll both to terminal CONCURRENTLY (they are running overlapping).
            timeout_s = _timeout_seconds()
            results: list[dict[str, Any]] = await asyncio.gather(
                *(
                    _poll_until_terminal(client, jid, timeout_s=timeout_s)
                    for jid in job_ids
                )
            )

            for jid, job in zip(job_ids, results, strict=True):
                assert job["status"] == "completed", (
                    f"concurrent job {jid} did not complete: {job}"
                )
                output_path = job.get("outputPath")
                assert output_path, f"completed job {jid} missing outputPath: {job}"
                names, size = await asyncio.to_thread(
                    _assert_cbz_on_disk, Path(output_path)
                )
                print(
                    f"\n[live #71 concurrent] job {jid} CBZ verified: "
                    f"{Path(output_path).name} ({size} bytes, {len(names)} entries)"
                )
