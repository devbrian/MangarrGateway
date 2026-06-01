"""Concurrent download JOBS through the REAL ``JobEngine`` (issue #71, item 2).

``tests/test_job_manager.py`` proves the scheduler caps (``max_concurrent_chapters``
global + ``max_concurrent_per_source`` per-source) against a FAKE ``_CountingEngine``
whose ``run()`` just bumps a counter — it proves the SEMAPHORE MATH but never the
REAL engine running concurrently. This file closes that gap: the ``JobManager`` drives
its real ``JobEngine`` against a stub SOURCE (registered into a fresh
``SourceRegistry``), so multiple full ``resolving → downloading → archiving →
completed`` lifecycles overlap for real, and we assert:

* (a) **Real-engine overlap** — N jobs on distinct handles, global cap C, a stub
  source gated so a job parks mid-fetch: at most C jobs reach the fetch stage at
  once; releasing the gate lets every job complete and publish a real CBZ.
* (b) **Per-source cap below global** — same-source jobs are additionally bounded by
  ``max_concurrent_per_source`` even when the global cap is wider (WR-02), measured
  on the REAL engine's fetch stage rather than a fake counter.
* (c) **Per-job failure isolation** — one job whose source raises mid-fetch ends
  ``failed`` (no partial CBZ) while every sibling job still completes; one job's
  failure never poisons another (the engine's per-job isolation contract, D-29).

Each stub job has a SINGLE-page manifest so the per-page ``fetch_image`` counter
equals the number of jobs concurrently in the fetch stage — making the
job-level cap (``max_concurrent_chapters`` / ``max_concurrent_per_source``)
directly observable (the per-page fan-out is a separate concern, witnessed in
``test_job_engine_parallel.py``).

Real on-disk ``JobStore`` (tmp_path), stub source, no HTTP / no browser.
"""

from __future__ import annotations

import asyncio
import io
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from manga_gateway.config import Settings
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore, ResolutionRecord
from manga_gateway.jobs.manager import JobManager
from manga_gateway.jobs.store import JobStore, open_store
from manga_gateway.models.download import SubmitRequest

if TYPE_CHECKING:
    from manga_gateway.framework.context import SourceContext


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 128, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _req(release_handle: str, source_key: str = "stub") -> SubmitRequest:
    return SubmitRequest(
        releaseHandle=release_handle,
        sourceKey=source_key,
        mangaId=None,
        outputFormat="cbz",
    )


def _record(idx: int) -> ResolutionRecord:
    return ResolutionRecord(
        source_key="stub",
        chapter_id=f"chap-{idx}",
        language="en",
        title=f"Series {idx} - Chapter 1 (en)",
        manga_title=f"Series {idx}",
        chapter_number=Decimal("1"),
        volume=None,
        scanlation_group="G",
        page_count=1,
    )


class _NullTransport:
    async def request(self, method: str, url: str, **kwargs: object) -> object:
        raise AssertionError("stub source must not perform real HTTP")

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _GatedFetchSource:
    """Stub source whose image fetch parks on a shared gate until released.

    Each job's manifest is a SINGLE page, so the per-page ``in_fetch`` counter
    equals the number of JOBS simultaneously past the global/per-source
    semaphores — making the job-level cap directly observable. Every concurrent
    job's engine reaches the fetch stage and blocks on the same
    ``asyncio.Event``; releasing it lets each engine finish and publish a CBZ.

    A per-URL ``fail_chapter_markers`` set makes the matching job's fetch raise
    — the per-job-isolation witness.
    """

    key = "stub"
    rate_limit_per_minute = 6000

    # Class-level shared state so every engine-constructed instance shares the
    # same gate/counters (the engine instantiates the registered source fresh
    # per job via ``cls()``).
    gate: asyncio.Event
    in_fetch: int
    peak_in_fetch: int
    fail_chapter_markers: set[str]

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        # ONE page; embed the chapter_id so the fail-marker check can key on it.
        return [f"http://node/{chapter_id}/p1.png"]

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        type(self).in_fetch += 1
        type(self).peak_in_fetch = max(type(self).peak_in_fetch, type(self).in_fetch)
        try:
            await type(self).gate.wait()
            if any(marker in url for marker in type(self).fail_chapter_markers):
                raise SourceError("source_unavailable", "gated fetch failure")
            return _png()
        finally:
            type(self).in_fetch -= 1


def _reset_gated_source() -> type[_GatedFetchSource]:
    _GatedFetchSource.gate = asyncio.Event()
    _GatedFetchSource.in_fetch = 0
    _GatedFetchSource.peak_in_fetch = 0
    _GatedFetchSource.fail_chapter_markers = set()
    return _GatedFetchSource


async def _make_manager(
    store: JobStore,
    *,
    max_concurrent: int,
    max_concurrent_per_source: int,
    output_root: str,
) -> JobManager:
    settings = Settings(
        api_key="k",
        output_root=output_root,
        db_path="unused-in-this-test.db",
        max_concurrent_chapters=max_concurrent,
        max_concurrent_per_source=max_concurrent_per_source,
        image_fetch_concurrency=4,
    )
    registry = SourceRegistry()
    registry._sources["stub"] = _reset_gated_source()  # type: ignore[assignment]
    return JobManager(
        store=store,
        registry=registry,
        session=SessionManager(_NullTransport()),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        settings=settings,
    )


async def _wait_until(predicate: object, *, timeout_s: float = 2.0) -> None:
    """Poll ``predicate()`` until truthy or timeout (avoids fixed sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached within timeout")


def _count_cbz(out_root: Path) -> int:
    return sum(
        1
        for _dir, _subdirs, files in os.walk(out_root)
        for name in files
        if name.endswith(".cbz")
    )


# ─────────────────── (a) real-engine overlap bounded by global cap ───────────


@pytest.mark.asyncio
async def test_real_engine_jobs_overlap_bounded_by_global_cap(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        cap = 2
        mgr = await _make_manager(
            store,
            max_concurrent=cap,
            max_concurrent_per_source=cap,  # don't let per-source be the binding cap
            output_root=str(tmp_path / "out"),
        )
        src = _GatedFetchSource  # the shared class

        # Submit 4 jobs on distinct handles AND distinct chapter ids so they map
        # to different output files (different handles ⇒ no idempotent collapse).
        for i in range(4):
            await mgr.submit(_record(i), _req(f"h{i}"))

        # Let tasks fill the global semaphore and park in fetch_image.
        await _wait_until(lambda: src.in_fetch >= cap)
        await asyncio.sleep(0.05)  # give any over-admission a chance to show
        # The real engine never has more than ``cap`` jobs in the fetch stage.
        assert src.peak_in_fetch <= cap, (
            f"peak_in_fetch={src.peak_in_fetch} exceeded the global cap {cap} — "
            "the real engine overran max_concurrent_chapters"
        )
        # And it DID overlap up to the cap (not serialized to 1).
        assert src.peak_in_fetch == cap

        # Release the gate — every job completes and publishes a real CBZ.
        src.gate.set()
        await mgr.drain()

        jobs = mgr.list()
        assert len(jobs) == 4
        assert all(j.status == "completed" for j in jobs), jobs
        # Four distinct CBZ files actually written.
        assert _count_cbz(tmp_path / "out") == 4
    finally:
        await store.close()


# ─────────────── (b) per-source cap binds below global on the real engine ────


@pytest.mark.asyncio
async def test_real_engine_per_source_cap_binds_below_global(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(
            store,
            max_concurrent=4,  # wide global
            max_concurrent_per_source=1,  # tight per-source (WR-02)
            output_root=str(tmp_path / "out"),
        )
        src = _GatedFetchSource

        for i in range(4):
            await mgr.submit(_record(i), _req(f"h{i}"))

        await _wait_until(lambda: src.in_fetch >= 1)
        await asyncio.sleep(0.05)
        # Per-source cap of 1 keeps the real engine at one fetch despite global=4.
        assert src.peak_in_fetch == 1, (
            f"peak_in_fetch={src.peak_in_fetch} — per-source cap of 1 should "
            "serialize same-source jobs even with global=4 (WR-02)"
        )

        src.gate.set()
        await mgr.drain()
        assert all(j.status == "completed" for j in mgr.list())
    finally:
        await store.close()


# ───────────────── (c) per-job failure isolation on the real engine ──────────


@pytest.mark.asyncio
async def test_real_engine_one_job_failure_does_not_poison_others(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = await _make_manager(
            store,
            max_concurrent=4,
            max_concurrent_per_source=4,
            output_root=str(tmp_path / "out"),
        )
        src = _GatedFetchSource
        # The job whose chapter id contains "chap-2" fails mid-fetch.
        src.fail_chapter_markers = {"chap-2"}

        for i in range(4):
            await mgr.submit(_record(i), _req(f"h{i}"))

        await _wait_until(lambda: src.in_fetch >= 1)
        src.gate.set()
        await mgr.drain()

        jobs = {j.title: j.status for j in mgr.list()}
        assert len(jobs) == 4
        # The bad job failed; the other three completed — failure is isolated.
        assert jobs["Series 2 - Chapter 1 (en)"] == "failed"
        for i in (0, 1, 3):
            assert jobs[f"Series {i} - Chapter 1 (en)"] == "completed", jobs
        # Exactly three CBZ files written (the failed job published none — D-29).
        assert _count_cbz(tmp_path / "out") == 3
    finally:
        await store.close()
