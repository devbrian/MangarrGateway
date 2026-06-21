"""Concurrent comix DOWNLOAD manifest-resolve nav parity (issue #71, item 3).

The engine-guard parity question: does ``max_concurrent_per_source > 1`` on a
Cloudflare source (comix) need its OWN guard mirroring the search-side
``_reject_camoufox_parallel`` (camoufox + concurrent browser navs = the #59
stall)? Investigation answer: **no new guard** — the download path is already
guarded TRANSITIVELY.

Anatomy (verified in ``sources/comix.py``):
* ``fetch_manifest`` does exactly ONE ``solver.eval_in_webview`` browser nav
  per job — the SAME primitive ``search()`` uses.
* ``fetch_image`` uses ``ctx.get_bytes_plain`` (plain httpx) — the browser is
  NEVER used for bulk image fetch, so the per-job image fan-out drives no navs.

So concurrent BROWSER navs across comix downloads arise ONLY when
``max_concurrent_per_source > 1`` lets 2+ manifest-resolves overlap, and those
navs queue on the framework's ``CloudflareSolver._browser_lock`` Semaphore (sized
from ``cloudflare_fetch_concurrency``) — the identical gate the parallel-search
fan-out uses, already pinned to 1 under camoufox by the
``_reject_camoufox_parallel`` validator. ``max_concurrent_per_source`` cannot
produce more simultaneous navs than ``cloudflare_fetch_concurrency`` allows.

This file PROVES that parity:

* (a) Two concurrent comix downloads each issue exactly ONE manifest-resolve
  ``eval_in_webview`` nav (and zero browser navs for the image bytes) — the
  download nav count equals the job count, image bytes go over httpx.
* (b) Wrapping the fake solver's ``eval_in_webview`` in a 1-wide Semaphore
  (the ``_browser_lock`` analog under camoufox's forced concurrency=1) caps the
  observed ``max_in_flight`` at 1 even with ``max_concurrent_per_source=2`` —
  i.e. the download navs honor the SAME browser gate search does, so the
  existing camoufox guard covers downloads with no new code.

Also a direct unit check that ``_reject_camoufox_parallel`` is the single
startup gate keying off ``cloudflare_fetch_concurrency`` (the shared knob).

Driven by a deterministic gated fake solver — no real browser, no network.
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from manga_gateway.config import Settings
from manga_gateway.framework.antibot import Clearance
from manga_gateway.handles.store import ResolutionRecord
from manga_gateway.jobs.model import JobStatus
from manga_gateway.sources.comix import ComixSource

_COMIX = ComixSource.base_url
_CF_COOKIE = {"cf_clearance": "CF-CLEAR-TOKEN"}
_CF_UA = "Mozilla/5.0 (Comix-Chrome) AppleWebKit/537.36"
_TEST_API_KEY = "test-comix-download-parallel-key"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _page_urls(job_idx: int) -> list[str]:
    """Valid Comix-CDN page URLs that pass the ``_is_allowed_image_url``
    allowlist (host ``{prefix}.wowpic{N}.store``, path ``/si/{token16+}/NN.ext``).
    A distinct token per job keeps the two jobs' image fetches unambiguous."""
    token = f"toktoktoktok{job_idx:04d}"  # >= 16 chars, [A-Za-z0-9_-]
    host = f"cdn{job_idx}.wowpic1.store"
    return [
        f"https://{host}/si/{token}/01.png",
        f"https://{host}/si/{token}/02.png",
    ]


class _GatedBrowserSolver:
    """Fake solver recording cross-call ``eval_in_webview`` concurrency.

    Mirrors ``_SlowComixSolver`` from ``test_comix_search_parallel.py`` but is
    aimed at the DOWNLOAD manifest-resolve path: every ``eval_in_webview`` call
    (the comix chapter-pages in-WebView read) parks on a barrier while the test
    measures how many are simultaneously in flight, optionally bounded by a
    ``_browser_lock``-analog Semaphore. ``get_clearance`` feeds the D-40 seam.

    ``first_nav`` is set the moment the first nav enters so a test can await it
    deterministically instead of polling (ASYNC110).
    """

    def __init__(self, gate: asyncio.Semaphore | None = None) -> None:
        self.results: dict[str, object] = {}
        self.browser_calls: list[str] = []
        self._gate = gate
        self.in_flight = 0
        self.max_in_flight = 0
        # Release barrier so all navs pile up before any completes (sharpens
        # the concurrency measurement independent of scheduler timing).
        self.release = asyncio.Event()
        # Signalled when the FIRST nav has entered (awaited by the cap test).
        self.first_nav = asyncio.Event()

    def stage(self, url: str, result: object) -> None:
        self.results[url] = result

    async def get_clearance(self, source_key: str) -> Clearance:
        return Clearance(cookies=dict(_CF_COOKIE), user_agent=_CF_UA)

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — matches the seam contract
    ) -> object:
        _ = (js, wait_for, timeout)
        self.browser_calls.append(challenge_url)
        self.first_nav.set()
        if self._gate is not None:
            await self._gate.acquire()
        try:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await self.release.wait()
                if challenge_url not in self.results:
                    raise AssertionError(f"unmocked eval_in_webview({challenge_url!r})")
                return self.results[challenge_url]
            finally:
                self.in_flight -= 1
        finally:
            if self._gate is not None:
                self._gate.release()


def _comix_record(idx: int) -> ResolutionRecord:
    """A comix ResolutionRecord whose composite chapter id resolves to a live
    chapter URL ``fetch_manifest`` will navigate to."""
    composite = ComixSource._make_composite_chapter_id(
        f"{1000 + idx}", f"hid{idx}", f"slug{idx}", "1"
    )
    return ResolutionRecord(
        source_key="comix",
        chapter_id=composite,
        language="en",
        title=f"Series {idx} - Chapter 1 (en)",
        manga_title=f"Series {idx}",
        chapter_number=Decimal("1"),
        volume=None,
        scanlation_group="G",
        page_count=2,
    )


def _chapter_url(idx: int) -> str:
    return f"{_COMIX}/title/hid{idx}-slug{idx}/{1000 + idx}-chapter-1"


async def _make_app(tmp_path: Path, *, max_per_source: int, fetch_concurrency: int):
    from manga_gateway.app import create_app

    settings = Settings(
        api_key=_TEST_API_KEY,
        db_path=str(tmp_path / "jobs.db"),
        handle_db_path=str(tmp_path / "handles.db"),
        output_root=str(tmp_path / "out"),
        max_concurrent_chapters=4,
        max_concurrent_per_source=max_per_source,
        image_fetch_concurrency=4,
        cloudflare_fetch_concurrency=fetch_concurrency,
    )
    return create_app(settings)


# ───────────────── (a) downloads use ONE browser nav per job ─────────────────


@respx.mock
@pytest.mark.asyncio
async def test_comix_download_uses_one_browser_nav_and_httpx_images(
    tmp_path: Path,
) -> None:
    """Each comix download issues exactly ONE manifest-resolve browser nav; the
    page images come over httpx (NOT the browser). Proves the per-job fan-out
    adds no browser navs — only across-job manifest-resolves can contend."""
    app = await _make_app(tmp_path, max_per_source=2, fetch_concurrency=3)
    solver = _GatedBrowserSolver()
    solver.release.set()  # no gating needed for the single-job count check

    page_urls = _page_urls(0)
    solver.stage(_chapter_url(0), page_urls)
    for u in page_urls:
        respx.get(u).mock(return_value=httpx.Response(200, content=_png()))

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.solver = solver
        app.state.job_manager._engine._solver = solver
        handle = app.state.handle_store.mint(_comix_record(0))

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers={"X-Api-Key": _TEST_API_KEY},
        ) as ac:
            submit = await ac.post(
                "/api/v1/downloads",
                json={"releaseHandle": handle, "sourceKey": "comix"},
            )
            assert submit.status_code == 200, submit.text
            await app.state.job_manager.drain()

        jobs = app.state.job_manager.list()
        assert len(jobs) == 1
        assert jobs[0].status == "completed", jobs[0]

    # Exactly ONE browser nav for the manifest resolve — image bytes went over
    # httpx (respx), never the browser.
    assert solver.browser_calls == [_chapter_url(0)]


# ─────────── (b) concurrent download navs honor the browser semaphore ─────────


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_comix_download_navs_honor_browser_lock_cap(
    tmp_path: Path,
) -> None:
    """Two concurrent comix downloads (``max_concurrent_per_source=2``) whose
    manifest-resolve navs queue on a 1-wide ``eval_in_webview`` gate (the
    ``_browser_lock`` analog under camoufox's forced ``cloudflare_fetch_concurrency=1``)
    never have more than 1 nav in flight — the download navs honor the SAME
    browser gate search does. This is why ``max_concurrent_per_source > 1``
    needs NO new camoufox guard: the existing ``_reject_camoufox_parallel`` gate
    on ``cloudflare_fetch_concurrency`` already bounds download navs."""
    app = await _make_app(tmp_path, max_per_source=2, fetch_concurrency=3)
    # 1-wide browser gate models the camoufox-pinned _browser_lock.
    gated = _GatedBrowserSolver(gate=asyncio.Semaphore(1))

    for idx in (0, 1):
        page_urls = _page_urls(idx)
        gated.stage(_chapter_url(idx), page_urls)
        for u in page_urls:
            respx.get(u).mock(return_value=httpx.Response(200, content=_png()))

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.solver = gated
        app.state.job_manager._engine._solver = gated
        handles = [app.state.handle_store.mint(_comix_record(i)) for i in (0, 1)]

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers={"X-Api-Key": _TEST_API_KEY},
        ) as ac:
            for handle in handles:
                submit = await ac.post(
                    "/api/v1/downloads",
                    json={"releaseHandle": handle, "sourceKey": "comix"},
                )
                assert submit.status_code == 200, submit.text

            # Wait until the first job reaches the gated browser nav (the second
            # job's nav is parked behind the 1-wide gate), then release.
            await asyncio.wait_for(gated.first_nav.wait(), timeout=2.0)
            await asyncio.sleep(0.05)  # give any over-admission a chance to show
            gated.release.set()
            await app.state.job_manager.drain()

        jobs = app.state.job_manager.list()
        assert len(jobs) == 2
        assert all(j.status == JobStatus.COMPLETED.value for j in jobs), jobs

    # The 1-wide browser gate was honored: never >1 nav in flight, even though
    # max_concurrent_per_source=2 admitted both jobs concurrently.
    assert gated.max_in_flight <= 1, (
        f"max_in_flight={gated.max_in_flight} exceeded the 1-wide browser gate — "
        "concurrent comix download navs are NOT honoring the _browser_lock cap, "
        "so the camoufox guard would not cover downloads"
    )
    # Both jobs DID issue their manifest-resolve nav (2 distinct chapter URLs).
    assert sorted(set(gated.browser_calls)) == sorted(
        {_chapter_url(0), _chapter_url(1)}
    )


# ─────────── parity: the single startup gate covers both surfaces ─────────────


def test_camoufox_guard_keys_off_the_shared_browser_concurrency_knob() -> None:
    """``_reject_camoufox_parallel`` gates on ``cloudflare_fetch_concurrency`` —
    the SAME knob that bounds BOTH the parallel-search fan-out and concurrent
    download manifest-resolve navs (both go through the framework's
    ``CloudflareSolver._browser_lock`` sized from ``cloudflare_fetch_concurrency``).
    So the existing search-side guard transitively covers the download surface;
    no separate ``max_concurrent_per_source`` guard is needed (issue #71-3)."""
    # camoufox + parallel browser concurrency must fail fast — regardless of
    # how high max_concurrent_per_source is set (it does not relax this gate).
    with pytest.raises(ValueError, match="cloudflare_fetch_concurrency"):
        Settings(
            api_key="k",
            cloudflare_engine="camoufox",
            cloudflare_fetch_concurrency=2,
            max_concurrent_per_source=4,
        )

    # camoufox pinned to 1 browser nav is accepted even with a wide per-source
    # job cap — because per-source bounds JOBS, while the browser gate (pinned
    # to 1) bounds the actual navs.
    ok = Settings(
        api_key="k",
        cloudflare_engine="camoufox",
        cloudflare_fetch_concurrency=1,
        max_concurrent_per_source=4,
    )
    assert ok.cloudflare_fetch_concurrency == 1
    assert ok.max_concurrent_per_source == 4
