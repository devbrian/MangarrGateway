"""Parallel image-fetch behavior of the ``JobEngine`` (issue #71, items 1 + 4).

The download-side analog of ``tests/test_comix_search_parallel.py``'s
``max_in_flight`` witness. ``JobEngine._fetch_pages_once`` fans every page out
under an ``asyncio.Semaphore(image_fetch_concurrency)`` inside an
``asyncio.TaskGroup`` and writes each result into ``results[index]`` BY INDEX,
so the per-page slot is stable regardless of fetch-completion order
(``engine.py`` ~line 247). Before #71 the engine tests only counted
``image_fetch_count`` and checked CBZ entry COUNT / sorted-names — a regression
to a sequential ``for`` loop, or a wrong ``results[...]`` slot, passed silently.

These tests close that gap:

* (1) **Parallelism witness** — a stub ``fetch_image`` that records cross-coro
  ``max_in_flight`` proves at least 2 fetches are simultaneously in-flight at
  ``image_fetch_concurrency >= 2``; a sequential regression drops it to 1.
* (1b) **Semaphore cap** — with more pages than the concurrency knob, observed
  ``max_in_flight`` never exceeds ``image_fetch_concurrency`` (the
  ``Semaphore`` is honored), and DOES saturate it (not serialized to 1).
* (4) **Content-correctness** — each page returns per-page-UNIQUE bytes with
  jittered delays so fetches complete OUT of launch order; the published CBZ
  has exactly ``totalPages`` entries, entry k decodes to page k's bytes, no
  duplicates, no drops, correct reading order — proving the index-addressed
  slot mapping is not just count-correct but content-correct.

Driven by a real on-disk ``JobStore`` + a stub source registered into a fresh
``SourceRegistry`` — no HTTP, no browser, deterministic.
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from manga_gateway.config import Settings
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore
from manga_gateway.jobs.engine import JobEngine
from manga_gateway.jobs.model import Job, JobStatus
from manga_gateway.jobs.store import JobStore, open_store

if TYPE_CHECKING:
    from manga_gateway.framework.context import SourceContext


# ─────────────────────────────── test helpers ───────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _unique_png(seed: int) -> bytes:
    """A valid PNG whose pixel colour is a deterministic function of ``seed``.

    Used by the content-correctness test so a decoded CBZ entry can be mapped
    back to the page index it MUST have come from — a swap/dup/drop in the
    parallel slot mapping changes which colour lands in which entry.
    """
    # Spread the seed across the three channels so even small page counts get
    # visually-distinct, unambiguous colours.
    r = (seed * 37) % 256
    g = (seed * 73 + 11) % 256
    b = (seed * 151 + 29) % 256
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (r, g, b)).save(buf, format="PNG")
    return buf.getvalue()


def _decode_color(content: bytes) -> tuple[int, int, int]:
    img = Image.open(io.BytesIO(content)).convert("RGB")
    return img.getpixel((0, 0))  # type: ignore[return-value]


def _make_job(source_key: str = "stub") -> Job:
    return Job(
        job_id="j_parallel",
        release_handle="h_parallel",
        source_key=source_key,
        title="Solo Leveling - Chapter 1 (en)",
        status=JobStatus.QUEUED,
        manga_id=42,
        output_format="cbz",
        chapter_id="11111111-2222-3333-4444-555555555555",
        language="en",
        total_pages=None,
        downloaded_pages=None,
        total_bytes=0,
        remaining_bytes=0,
        output_path=None,
        message=None,
        created_at=_now(),
        updated_at=_now(),
        completed_at=None,
    )


class _NullTransport:
    async def request(self, method: str, url: str, **kwargs: object) -> object:
        raise AssertionError("stub source must not perform real HTTP")

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


class _ConcurrencyWitnessSource:
    """Stub source that records cross-coro image-fetch concurrency.

    ``fetch_image`` increments an in-flight counter, sleeps a per-URL delay,
    then returns the staged bytes — so a parallel fan-out drives
    ``max_in_flight`` above 1 while a sequential regression pins it at 1. The
    per-URL delay map lets a test stagger completion order independently of
    launch order (the content-correctness witness).
    """

    key = "stub"
    rate_limit_per_minute = 6000

    def __init__(
        self,
        *,
        image_map: dict[str, bytes],
        delays: dict[str, float] | None = None,
    ) -> None:
        self._manifest = list(image_map.keys())
        self._image_map = image_map
        self._delays = delays or {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.fetch_count = 0

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        return list(self._manifest)

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        self.fetch_count += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delays.get(url, 0.0))
            return self._image_map[url]
        finally:
            self.in_flight -= 1


def _engine_for(
    source: _ConcurrencyWitnessSource,
    store: JobStore,
    *,
    output_root: str,
    image_fetch_concurrency: int,
) -> JobEngine:
    registry = SourceRegistry()
    registry._sources[source.key] = lambda: source  # type: ignore[assignment]
    settings = Settings(
        api_key="k",
        output_root=output_root,
        image_fetch_concurrency=image_fetch_concurrency,
    )
    return JobEngine(
        store=store,
        registry=registry,
        session=SessionManager(_NullTransport()),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        settings=settings,
    )


# ───────────────────── (1) image-fetch parallelism witness ──────────────────


@pytest.mark.asyncio
async def test_image_fetch_runs_in_parallel(tmp_path: Path) -> None:
    """At ``image_fetch_concurrency >= 2`` the per-page fetches overlap.

    Six pages each sleeping 0.05 s: parallel ⇒ ``max_in_flight >= 2`` and a
    wall-clock well under the 0.30 s sequential sum. A regression to a
    sequential ``for`` loop pins ``max_in_flight`` at 1 and the assertion fails
    directly (not merely on wall-clock jitter — the search-side witness pattern).
    """
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = [f"http://node/data/h/p{i}.png" for i in range(6)]
        image_map = {u: _unique_png(i) for i, u in enumerate(urls)}
        src = _ConcurrencyWitnessSource(
            image_map=image_map,
            delays=dict.fromkeys(urls, 0.05),
        )
        engine = _engine_for(
            src, store, output_root=str(tmp_path / "out"), image_fetch_concurrency=6
        )
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        assert job.total_pages == 6
        assert job.downloaded_pages == 6
        # The witness: at least two fetches were simultaneously in-flight. A
        # sequential loop would never exceed 1.
        assert src.max_in_flight >= 2, (
            f"only {src.max_in_flight} concurrent image fetches — engine likely "
            "regressed to a sequential loop"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_image_fetch_concurrency_is_capped_by_the_semaphore(
    tmp_path: Path,
) -> None:
    """``image_fetch_concurrency`` is the real ceiling on in-flight fetches.

    Eight pages with a concurrency knob of 3: observed ``max_in_flight`` must
    never exceed 3 (the ``asyncio.Semaphore`` is honored) AND must reach 3
    (it is not accidentally serialized) — the download-side analog of the
    search ``_browser_lock`` cap test.
    """
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        cap = 3
        urls = [f"http://node/data/h/p{i}.png" for i in range(8)]
        image_map = {u: _unique_png(i) for i, u in enumerate(urls)}
        src = _ConcurrencyWitnessSource(
            image_map=image_map,
            delays=dict.fromkeys(urls, 0.05),
        )
        engine = _engine_for(
            src, store, output_root=str(tmp_path / "out"), image_fetch_concurrency=cap
        )
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        assert src.max_in_flight <= cap, (
            f"max_in_flight={src.max_in_flight} exceeded image_fetch_concurrency="
            f"{cap} — the per-job semaphore is not bounding the fan-out"
        )
        assert src.max_in_flight == cap, (
            f"max_in_flight={src.max_in_flight} — expected to saturate the "
            f"concurrency cap of {cap} with eight concurrent pages"
        )
    finally:
        await store.close()


# ───────────────────── (4) content-correctness of the fan-out ────────────────


@pytest.mark.asyncio
async def test_parallel_fan_out_is_content_correct_under_skewed_delays(
    tmp_path: Path,
) -> None:
    """Page k lands in CBZ entry k regardless of fetch-completion order (#71-4).

    Each page returns per-page-UNIQUE bytes (a distinct PNG colour) and the
    delays are skewed so the LAST page completes FIRST and the first completes
    LAST — fetches finish badly out of launch order. The published CBZ must
    still have exactly ``totalPages`` entries, in reading order, with entry k
    decoding to page k's unique colour: no duplicates, no drops, no swaps.

    Run at ``image_fetch_concurrency > 1`` so the slots are filled by genuinely
    overlapping coroutines — this is the test that would catch a
    "parallelized but silently wrong" ``results[...]`` mapping bug that every
    count-based / sorted-names smoke test passes.
    """
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        n = 7
        urls = [f"http://node/data/h/p{i}.png" for i in range(n)]
        image_map = {u: _unique_png(i) for i, u in enumerate(urls)}
        # Skew: page 0 slowest, page n-1 fastest → completion order is the
        # reverse of launch order.
        delays = {u: 0.005 * (n - i) for i, u in enumerate(urls)}
        src = _ConcurrencyWitnessSource(image_map=image_map, delays=delays)
        engine = _engine_for(
            src, store, output_root=str(tmp_path / "out"), image_fetch_concurrency=n
        )
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        assert job.output_path is not None
        assert job.total_pages == n
        # Confirm the fan-out really overlapped (otherwise the order-correctness
        # claim is vacuous — a sequential loop is trivially in order).
        assert src.max_in_flight >= 2, (
            "fan-out did not overlap — content-correctness under out-of-order "
            "completion is not actually being exercised"
        )

        with zipfile.ZipFile(job.output_path) as zf:
            names = zf.namelist()
            # Exactly totalPages entries, in reading order, no dups/drops.
            assert len(names) == n
            assert names == sorted(names)
            assert len(set(names)) == n
            entry_bytes = [zf.read(name) for name in names]

        # Entry k (1-indexed in the archive, so names[k] is page k) must decode
        # to page k's unique colour — the index-addressed slot mapping is
        # content-correct, not just count-correct.
        expected_colors = [_decode_color(_unique_png(i)) for i in range(n)]
        actual_colors = [_decode_color(b) for b in entry_bytes]
        assert actual_colors == expected_colors, (
            "CBZ entry colours do not match page order — the parallel fan-out "
            f"mis-mapped pages (swap/dup/drop). expected={expected_colors} "
            f"actual={actual_colors}"
        )
        # No page's bytes appear twice (a dup that happened to stay in order
        # would still be wrong).
        assert len(set(entry_bytes)) == n
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_parallel_fan_out_strict_fail_isolates_to_the_job(
    tmp_path: Path,
) -> None:
    """One page failing mid-fan-out fails THIS job strictly, writes no CBZ.

    The per-job analog of the search-side per-candidate isolation: a single
    page raising a non-403 ``SourceError`` inside the concurrent ``TaskGroup``
    ends the job ``failed`` (D-29 strict, no partial CBZ) — the other in-flight
    fetches are cancelled, not published.
    """
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = [f"http://node/data/h/p{i}.png" for i in range(5)]
        image_map = {u: _unique_png(i) for i, u in enumerate(urls)}

        # Wrap fetch_image so page index 2 raises after a tiny delay, while the
        # others would succeed — proving the failure is isolated and strict.
        class _OneBadPageSource(_ConcurrencyWitnessSource):
            async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
                self.fetch_count += 1
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                try:
                    await asyncio.sleep(0.01)
                    if url == urls[2]:
                        raise SourceError("source_unavailable", "page blew up")
                    return self._image_map[url]
                finally:
                    self.in_flight -= 1

        src = _OneBadPageSource(image_map=image_map)
        engine = _engine_for(
            src, store, output_root=str(tmp_path / "out"), image_fetch_concurrency=5
        )
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        assert job.message
        # No partial CBZ published anywhere under the output root (D-29 strict).
        out_root = tmp_path / "out"
        cbz_found = any(
            name.endswith(".cbz")
            for _dir, _subdirs, files in os.walk(out_root)
            for name in files
        )
        assert not cbz_found
    finally:
        await store.close()
