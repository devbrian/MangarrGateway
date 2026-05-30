"""Opt-in Comix end-to-end live test — full search → handle → CBZ on disk.

This is the only live test that drives the FULL gateway slice against the real
Comix site through a real Patchright browser + real Cloudflare AND a real CDN
image fetch + real CBZ packaging. It is marked ``@pytest.mark.live`` and
therefore EXCLUDED from ``uv run nox -s gate`` (the ``addopts = "-m 'not live'"``
in pyproject.toml).

What this proves (that the manifest-only smoke does not):

1. POST /search against the real Comix site returns real releases.
2. POST /downloads with a real release handle accepts it and returns a jobId.
3. The job advances through resolving → fetching → packaging → completed,
   exercising the WARM solver's Cloudflare bypass + browser-DOM manifest read
   + httpx CDN image fetch + Pillow validation + CBZ packaging — all the
   integration points the test_comix_slice deterministic tests stub out.
4. The output CBZ exists on disk, opens cleanly, contains page-image entries,
   and every entry is a valid image (Pillow decode without exception).

Reusability (D-43 / GH #17): path-agnostic (tmp_path), marker-gated, env-knob-
driven so a future nightly job reuses it unchanged. Bandwidth-aware: downloads
one chapter's pages (typically 10-25 MB) once per run.
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from manga_gateway.app import create_app
from manga_gateway.config import Settings

pytestmark = pytest.mark.live  # excluded from the gate (D-43)

# Default query targets "The Forgotten Field" — the same series the manifest
# smoke (test_comix_live.py) is pinned to and known to render under the
# canonical Swiper.js long-strip reader the .rpage-page extractor handles.
# Other series may use different reader variants (paginated vs long-strip,
# different CDN URL patterns); reader-variant coverage is tracked as a
# follow-up. Override via COMIX_E2E_QUERY when those land.
_DEFAULT_QUERY = "Forgotten Field"
_TEST_API_KEY = "test-e2e-comix-key-DO-NOT-LOG-IN-PROD"
# Generous timeout: real Cloudflare solve (~2s) + browser-DOM manifest read (~25s)
# + CDN image fetches (~12 pages × ~1-2 MB each, parallelised) + CBZ packaging.
# Comix's rate limit is ~10/min, so the image fetch is the dominant term.
_TERMINAL_TIMEOUT_S = 480.0
# Try the first N releases — some manga slots can be ad-only or empty chapters;
# loop forward so the test is robust to one bad release without re-running.
_MAX_RELEASES_TO_TRY = 3


def _query() -> str:
    return os.environ.get("COMIX_E2E_QUERY", _DEFAULT_QUERY)


def _headless() -> bool:
    return os.environ.get("COMIX_LIVE_HEADLESS", "1") != "0"


async def _poll_until_terminal(
    client: httpx.AsyncClient, job_id: str, *, timeout_s: float
) -> dict[str, Any]:
    """Poll ``GET /downloads`` until the job reaches a terminal status.

    Logs every status transition so a stuck job can be diagnosed in real time.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last_seen: dict[str, Any] | None = None
    last_status: str | None = None
    start = loop.time()
    while loop.time() < deadline:
        resp = await client.get("/api/v1/downloads")
        resp.raise_for_status()
        jobs = resp.json()["jobs"]
        job = next((j for j in jobs if j["jobId"] == job_id), None)
        if job is not None:
            last_seen = job
            if job["status"] != last_status:
                elapsed = loop.time() - start
                print(
                    f"[e2e poll {elapsed:5.1f}s] {last_status or '(submit)'} "
                    f"-> {job['status']}  pages={job.get('downloadedPages')}/"
                    f"{job.get('totalPages')}  bytes={job.get('totalBytes', 0)}"
                )
                last_status = job["status"]
            if job["status"] in ("completed", "failed"):
                terminal: dict[str, Any] = job
                return terminal
        await asyncio.sleep(1.0)
    raise AssertionError(
        f"job {job_id} did not terminate within {timeout_s}s; "
        f"last seen state: {last_seen}"
    )


def _assert_cbz_on_disk(cbz_path: Path) -> tuple[list[str], int]:
    """Sync-IO helper kept out of the async test body (ASYNC240).

    Returns ``(zip entry names, total bytes)`` for the test to log on success.
    Pillow ``verify()`` mirrors the framework's ``is_valid_image`` guard:
    truncated bytes / HTML error pages masquerading as images will raise.
    """
    assert cbz_path.exists(), f"CBZ not on disk: {cbz_path}"
    assert cbz_path.suffix == ".cbz", f"not a CBZ: {cbz_path}"
    size = cbz_path.stat().st_size
    assert size > 1024, f"CBZ suspiciously small ({size} bytes): {cbz_path}"
    with zipfile.ZipFile(cbz_path) as zf:
        names = sorted(zf.namelist())
        assert names, f"CBZ has no entries: {cbz_path}"
        for name in names:
            data = zf.read(name)
            Image.open(io.BytesIO(data)).verify()
    return names, size


async def test_comix_e2e_search_to_cbz(tmp_path: Path) -> None:
    """Full live slice: POST /search → POST /downloads → poll → assert CBZ."""
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
        # Production lifespan fires warm() as fire-and-forget so the gateway
        # boots even when Patchright is slow / broken. The live e2e test wants
        # a determined state before issuing requests, so we await warm here
        # — fetch_via_browser otherwise races the initial Cloudflare solve
        # (cookies not in the jar yet → new page tabs hit the challenge).
        solver = app.state.solver
        await asyncio.wait_for(solver.warm(), timeout=60.0)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers={"X-Api-Key": _TEST_API_KEY},
            timeout=120.0,
        ) as client:
            # 1. SEARCH — real Comix call through the gateway. ``sources=["comix"]``
            #    scopes the fan-out so MangaDex network access isn't part of the
            #    smoke (MangaDex is plaintext and was already proven in Phase 2).
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
            payload = search.json()
            releases = payload.get("releases") or []
            assert releases, (
                f"no releases returned for query={_query()!r}: {payload}; "
                f"if comix.to changed its catalog, override COMIX_E2E_QUERY"
            )
            warnings = payload.get("warnings") or []
            assert not any(w.get("sourceKey") == "comix" for w in warnings), (
                f"comix search emitted a warning: {warnings}"
            )

            # 2. SUBMIT — try the first N releases until one completes. Some chapter
            #    slots can be ad-only or rotted; we tolerate per-release failures.
            last_failure: dict[str, Any] | None = None
            for release in releases[:_MAX_RELEASES_TO_TRY]:
                handle = release["downloadHandle"]
                submit_body: dict[str, Any] = {
                    "releaseHandle": handle,
                    "sourceKey": "comix",
                }
                submit = await client.post("/api/v1/downloads", json=submit_body)
                assert submit.status_code == 200, (
                    f"submit failed: {submit.status_code} {submit.text[:400]}"
                )
                submit_json = submit.json()
                job_id = submit_json.get("jobId")
                assert job_id is not None, (
                    f"submit returned null jobId for handle {handle!r}: {submit_json}"
                )

                # 3. POLL — drive the full job through resolving → fetching →
                #    packaging → completed (or failed).
                job = await _poll_until_terminal(
                    client, job_id, timeout_s=_TERMINAL_TIMEOUT_S
                )
                if job["status"] != "completed":
                    last_failure = job
                    continue  # next release

                # 4. ASSERT CBZ ON DISK + VALIDATE CONTENTS — the full
                #    end-to-end outcome the manifest smoke could not prove.
                output_path = job.get("outputPath")
                assert output_path, f"completed job missing outputPath: {job}"
                cbz_path = Path(output_path)
                names, size = await asyncio.to_thread(_assert_cbz_on_disk, cbz_path)
                print(
                    f"\n[e2e] CBZ verified: {cbz_path.name} "
                    f"({size} bytes, {len(names)} entries)"
                )
                return  # SUCCESS

            raise AssertionError(
                f"none of the first {_MAX_RELEASES_TO_TRY} releases completed; "
                f"last terminal state: {last_failure}"
            )
