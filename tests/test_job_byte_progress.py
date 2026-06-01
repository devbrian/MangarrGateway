"""Byte-progress + ETA projection tests (WR-08, issue #10 — LOCKED Option 2).

The download wire must report a moving ``totalBytes``/``remainingBytes`` estimate
and an ``etaSeconds`` while a job is ``downloading`` (SAB/qBittorrent-style), and the
EXACT real total + ``remainingBytes:0`` + ``etaSeconds:null`` on completion. Queued
jobs stay ``0/0/null``. ``remainingBytes`` is never negative and no projection path
divides by zero.

These are OFFLINE unit tests mirroring ``tests/test_job_engine.py`` /
``tests/test_job_manager.py``: a real on-disk ``JobStore`` (tmp_path), a STUB source
for the end-to-end engine run, and direct ``JobManager._to_dto`` projection on
hand-built ``Job`` objects for the deterministic estimate-arithmetic / clamp /
divide-by-zero cases. No ``@pytest.mark.live`` tests, no network/credentials.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from manga_gateway.config import Settings
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore
from manga_gateway.jobs.engine import JobEngine
from manga_gateway.jobs.manager import JobManager
from manga_gateway.jobs.model import Job, JobStatus
from manga_gateway.jobs.store import JobStore, open_store

if TYPE_CHECKING:
    from manga_gateway.framework.context import SourceContext


# ─────────────────────────────── test helpers ───────────────────────────────


def _png_bytes(size: int = 2) -> bytes:
    """A tiny valid PNG that Pillow ``verify()`` accepts (byte length varies)."""
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _seconds_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _make_job(
    *,
    status: JobStatus = JobStatus.QUEUED,
    total_pages: int | None = None,
    downloaded_pages: int | None = None,
    total_bytes: int = 0,
    remaining_bytes: int = 0,
    downloaded_bytes: int = 0,
    download_started_at: str | None = None,
    job_id: str = "j_test",
    release_handle: str = "h_test",
    source_key: str = "stub",
) -> Job:
    return Job(
        job_id=job_id,
        release_handle=release_handle,
        source_key=source_key,
        title="Solo Leveling - Chapter 1 (en)",
        status=status,
        manga_id=42,
        output_format="cbz",
        chapter_id="11111111-2222-3333-4444-555555555555",
        language="en",
        total_pages=total_pages,
        downloaded_pages=downloaded_pages,
        total_bytes=total_bytes,
        remaining_bytes=remaining_bytes,
        output_path=None,
        message=None,
        created_at=_now(),
        updated_at=_now(),
        completed_at=None,
        downloaded_bytes=downloaded_bytes,
        download_started_at=download_started_at,
    )


class _StubSource:
    """A minimal source the engine dispatches to source-agnostically."""

    key = "stub"
    rate_limit_per_minute = 6000

    def __init__(
        self,
        *,
        manifest: list[str],
        image_map: dict[str, bytes],
    ) -> None:
        self._manifest = manifest
        self._image_map = image_map

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        return self._manifest

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        return self._image_map[url]


class _NullTransport:
    async def request(self, method: str, url: str, **kwargs: object) -> object:
        raise AssertionError("stub source must not perform real HTTP")

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


def _engine_for(source: _StubSource, store: JobStore, *, output_root: str) -> JobEngine:
    registry = SourceRegistry()
    registry._sources[source.key] = lambda: source  # type: ignore[assignment]
    settings = Settings(api_key="k", output_root=output_root, image_fetch_concurrency=4)
    return JobEngine(
        store=store,
        registry=registry,
        session=SessionManager(_NullTransport()),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        settings=settings,
    )


def _manager(store: JobStore) -> JobManager:
    """A JobManager whose engine is irrelevant — used only for ``_to_dto``."""
    settings = Settings(api_key="k", output_root="/tmp/out")
    return JobManager(
        store=store,
        registry=SourceRegistry(),
        session=SessionManager(_NullTransport()),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        settings=settings,
    )


# ────────────────── 1. end-to-end: exact total on complete ──────────────────


@pytest.mark.asyncio
async def test_engine_accumulates_exact_bytes_and_pins_total_on_complete(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png", "http://node/data/h/p2.png"]
        # Distinct payloads of KNOWN (differing) sizes.
        img1 = _png_bytes(2)
        img2 = _png_bytes(8)
        src = _StubSource(manifest=urls, image_map={urls[0]: img1, urls[1]: img2})
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        expected = len(img1) + len(img2)
        assert job.downloaded_bytes == expected
        assert job.total_bytes == expected  # pinned exact real total
        assert job.remaining_bytes == 0

        # The wire projection of the completed job reports the exact total + null ETA.
        dto = _manager(store)._to_dto(job)
        assert dto.total_bytes == expected
        assert dto.remaining_bytes == 0
        assert dto.eta_seconds is None
    finally:
        await store.close()


# ────────────────── 2. downloading estimate arithmetic + ETA ─────────────────


@pytest.mark.asyncio
async def test_downloading_projects_per_page_average_estimate_and_eta(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = _manager(store)
        # 2 of 4 pages, 2000 bytes so far → est_total = 2000/2*4 = 4000,
        # remaining = 4000 - 2000 = 2000; a positive elapsed → non-null ETA.
        job = _make_job(
            status=JobStatus.DOWNLOADING,
            total_pages=4,
            downloaded_pages=2,
            downloaded_bytes=2000,
            download_started_at=_seconds_ago(4.0),
        )
        dto = mgr._to_dto(job)
        assert dto.total_bytes == 4000
        assert dto.remaining_bytes == 2000
        assert dto.eta_seconds is not None
        assert dto.eta_seconds >= 0
    finally:
        await store.close()


# ────────── 2b. archiving keeps the live estimate (monotonic, no 0 glitch) ──────


@pytest.mark.asyncio
async def test_archiving_projects_live_estimate_not_stored_zero(
    tmp_path: Path,
) -> None:
    """ARCHIVING must not regress the counter to 0/0 (issue #10 / CodeRabbit).

    By ARCHIVING all pages are fetched but the engine has not yet pinned
    total_bytes (that is a COMPLETED-only write), so the stored value is still 0.
    A DOWNLOADING-only guard would briefly project 0/0 here and then jump to the
    exact total on completion. The projection must instead stay continuous:
    est_total == downloaded_bytes, remaining == 0.
    """
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = _manager(store)
        job = _make_job(
            status=JobStatus.ARCHIVING,
            total_pages=4,
            downloaded_pages=4,  # all fetched
            downloaded_bytes=4000,
            total_bytes=0,  # NOT yet pinned (pinned on COMPLETED) — must not leak
            remaining_bytes=0,
            download_started_at=_seconds_ago(4.0),
        )
        dto = mgr._to_dto(job)
        # est_total = 4000/4*4 = 4000 (== downloaded_bytes), remaining = 0.
        assert dto.total_bytes == 4000, "archiving must not regress to stored 0"
        assert dto.remaining_bytes == 0
        assert dto.eta_seconds is not None  # rate observed → not null during archiving
        assert dto.eta_seconds >= 0
    finally:
        await store.close()


# ────────────────── 3. negative-clamp (never below zero) ─────────────────────


@pytest.mark.asyncio
async def test_remaining_bytes_clamps_at_zero_when_average_overcounts(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = _manager(store)
        # Contrive downloaded_pages > total_pages so the per-page average
        # UNDER-counts the total (est_total < downloaded_bytes) and the un-clamped
        # remainder would go NEGATIVE — the only arrangement that actually
        # exercises the max(0, …) clamp (with pages == total_pages the clamp is
        # only ever fed 0 and would pass even if removed). This state shouldn't
        # arise in practice (the engine never advances downloaded_pages past
        # total_pages), but the clamp is the defensive guard against exactly that.
        job = _make_job(
            status=JobStatus.DOWNLOADING,
            total_pages=3,
            downloaded_pages=4,  # > total_pages → est_total < downloaded_bytes
            downloaded_bytes=9000,
            total_bytes=999,  # stale stored value must not leak through
            download_started_at=_seconds_ago(3.0),
        )
        dto = mgr._to_dto(job)
        # est_total = round(9000/4*3) = 6750; un-clamped remaining = 6750-9000 =
        # -2250 → clamped to 0.
        assert dto.total_bytes == 6750
        assert dto.remaining_bytes == 0
        assert dto.remaining_bytes >= 0
    finally:
        await store.close()


# ────────────────── 4. divide-by-zero / not-yet-usable guard ─────────────────


@pytest.mark.asyncio
async def test_downloading_with_zero_pages_projects_stored_zero_no_exception(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = _manager(store)
        # downloaded_pages == 0 (and total_pages None) → not usable → stored 0/0.
        job = _make_job(
            status=JobStatus.DOWNLOADING,
            total_pages=None,
            downloaded_pages=0,
            downloaded_bytes=0,
            download_started_at=_seconds_ago(2.0),
        )
        dto = mgr._to_dto(job)
        assert dto.total_bytes == 0
        assert dto.remaining_bytes == 0
        assert dto.eta_seconds is None
    finally:
        await store.close()


# ────────────────── 5. queued fallback (0/0/null) ───────────────────────────


@pytest.mark.asyncio
async def test_queued_job_projects_zero_zero_null(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = _manager(store)
        job = _make_job(status=JobStatus.QUEUED, total_bytes=0, remaining_bytes=0)
        dto = mgr._to_dto(job)
        assert dto.total_bytes == 0
        assert dto.remaining_bytes == 0
        assert dto.eta_seconds is None
    finally:
        await store.close()


# ────────────────── 6. ETA divide-by-zero guard ─────────────────────────────


@pytest.mark.asyncio
async def test_downloading_without_start_marker_projects_null_eta(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = _manager(store)
        # download_started_at None → no usable elapsed/rate → etaSeconds null,
        # but the byte estimate still projects (pages/bytes are usable).
        job = _make_job(
            status=JobStatus.DOWNLOADING,
            total_pages=4,
            downloaded_pages=2,
            downloaded_bytes=2000,
            download_started_at=None,
        )
        dto = mgr._to_dto(job)
        assert dto.total_bytes == 4000
        assert dto.remaining_bytes == 2000
        assert dto.eta_seconds is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_downloading_with_zero_bytes_projects_null_eta(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        mgr = _manager(store)
        # downloaded_bytes == 0 → not usable for the estimate (and the rate would
        # be zero) → stored 0/0 + null ETA, no exception.
        job = _make_job(
            status=JobStatus.DOWNLOADING,
            total_pages=4,
            downloaded_pages=2,
            downloaded_bytes=0,
            download_started_at=_seconds_ago(5.0),
        )
        dto = mgr._to_dto(job)
        assert dto.total_bytes == 0
        assert dto.remaining_bytes == 0
        assert dto.eta_seconds is None
    finally:
        await store.close()
