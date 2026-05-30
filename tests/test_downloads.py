"""``POST/GET /downloads`` E2E tests (Task 3 — DL-01/04/05, HDL-02, PKG-01).

Drives the full core-value loop in-process: mint a ``downloadHandle`` in the app's
handle store, ``POST /downloads`` with it, then poll ``GET /downloads`` until the job
reaches ``completed`` with a CBZ on disk — MangaDex's at-home manifest + image bytes are
respx-mocked so no network is touched, and the manifest NEVER appears in any response
(PKG-01/R6).

A dedicated app/client fixture pair points the job store at a tmp DB and the output root
at a tmp dir so jobs are isolated per test and never write into the repo.
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI
from PIL import Image

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.handles.store import ResolutionRecord

from .conftest import BASE_URL, TEST_API_KEY

_MANGADEX = "https://api.mangadex.org"
_AT_HOME_NODE = "https://node.mangadex.network"
_CHAPTER_ID = "11111111-2222-3333-4444-555555555555"
_CHAPTER_HASH = "abc123hash"


def _exists(path: str) -> bool:
    """Sync existence check (avoids ASYNC240 in async test bodies)."""
    return os.path.exists(path)


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 128, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def dl_app(tmp_path: Path) -> FastAPI:
    """App with a tmp job store + output root so each test is isolated."""
    return create_app(
        Settings(
            api_key=TEST_API_KEY,
            db_path=str(tmp_path / "jobs.db"),
            output_root=str(tmp_path / "out"),
        )
    )


@pytest_asyncio.fixture
async def dl_client(dl_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            yield ac


def _mint_handle(app: FastAPI) -> str:
    """Mint a resolvable handle directly in the app's handle store (as search would)."""
    return app.state.handle_store.mint(
        ResolutionRecord(
            source_key="mangadex",
            chapter_id=_CHAPTER_ID,
            language="en",
            title="Solo Leveling - Chapter 1 (en)",
            manga_title="Solo Leveling",
            chapter_number=Decimal("1"),
            volume=None,
            scanlation_group="Team Lumikha",
            page_count=2,
        )
    )


def _mock_at_home(pages: int = 2) -> list[str]:
    """Mock the at-home manifest + page image hosts; return the page filenames."""
    filenames = [f"p{i}.png" for i in range(1, pages + 1)]
    respx.get(f"{_MANGADEX}/at-home/server/{_CHAPTER_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": "ok",
                "baseUrl": _AT_HOME_NODE,
                "chapter": {
                    "hash": _CHAPTER_HASH,
                    "data": filenames,
                    "dataSaver": filenames,
                },
            },
        )
    )
    for name in filenames:
        respx.get(f"{_AT_HOME_NODE}/data/{_CHAPTER_HASH}/{name}").mock(
            return_value=httpx.Response(200, content=_png_bytes())
        )
    return filenames


async def _poll_until(
    client: httpx.AsyncClient, job_id: str, *, timeout_s: float = 5.0
) -> dict:
    """Poll GET /downloads until the job is terminal (completed/failed) or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        resp = await client.get("/downloads")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        job = next((j for j in jobs if j["jobId"] == job_id), None)
        assert job is not None
        if job["status"] in ("completed", "failed"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not terminate within {timeout_s}s")


# ─────────────────────────── E2E happy path (DL-01/04) ──────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_submit_and_complete_writes_cbz(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=2)
    handle = _mint_handle(dl_app)

    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobId"] is not None
    assert body["jobId"].startswith("j_")
    assert body["status"] == "queued"

    job = await _poll_until(dl_client, body["jobId"])
    assert job["status"] == "completed"
    assert job["outputPath"] is not None
    assert _exists(job["outputPath"])
    with zipfile.ZipFile(job["outputPath"]) as zf:
        assert len(zf.namelist()) == 2


# ─────────────────────────── HDL-02 expired handle ─────────────────────────


@pytest.mark.asyncio
async def test_unknown_handle_returns_400_jobid_null(
    dl_client: httpx.AsyncClient,
) -> None:
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": "does-not-exist", "sourceKey": "mangadex"},
    )
    assert resp.status_code == 400
    body = resp.json()
    # A SubmitResponse with jobId:null — NOT the {error:...} envelope (HDL-02).
    assert "jobId" in body
    assert body["jobId"] is None
    assert "error" not in body
    assert body.get("message")


# ─────────────────────────── PKG-01/R6 no manifest leak ─────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_downloads_never_leaks_manifest(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=2)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    await _poll_until(dl_client, job_id)

    listing = await dl_client.get("/downloads")
    raw = listing.text.lower()
    # Manifest internals (baseUrl, the node host, page URLs, the hash) must NOT appear.
    assert "baseurl" not in raw
    assert "node.mangadex.network" not in raw
    assert _CHAPTER_HASH not in raw
    assert "manifest" not in raw
    # Only contract DownloadJob keys are present.
    job = next(j for j in listing.json()["jobs"] if j["jobId"] == job_id)
    allowed = {
        "jobId",
        "title",
        "sourceKey",
        "status",
        "totalBytes",
        "remainingBytes",
        "totalPages",
        "downloadedPages",
        "etaSeconds",
        "outputPath",
        "message",
        "createdAt",
        "updatedAt",
        "completedAt",
    }
    assert set(job).issubset(allowed)


# ─────────────────────────── DL-04 lists live + finished ────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_downloads_lists_finished_jobs(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=1)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    await _poll_until(dl_client, job_id)

    listing = await dl_client.get("/downloads")
    assert listing.status_code == 200
    payload = listing.json()
    assert "jobs" in payload
    assert any(j["jobId"] == job_id for j in payload["jobs"])


# ─────────────────────────── auth (existing global guard) ───────────────────


@pytest.mark.asyncio
async def test_submit_without_api_key_returns_401(dl_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=dl_app)
    async with dl_app.router.lifespan_context(dl_app):
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as ac:
            resp = await ac.post(
                "/downloads",
                json={"releaseHandle": "x", "sourceKey": "mangadex"},
            )
    assert resp.status_code == 401


# ─────────────────────────── DL-03 idempotency-by-existence (D-27) ───────────


@pytest.mark.asyncio
async def test_resubmit_live_handle_returns_same_job(dl_app: FastAPI) -> None:
    """DL-03: a second submit of a still-live handle returns the SAME jobId."""
    from manga_gateway.models.download import SubmitRequest  # noqa: PLC0415

    async with dl_app.router.lifespan_context(dl_app):
        manager = dl_app.state.job_manager
        handle = _mint_handle(dl_app)
        record = dl_app.state.handle_store.resolve(handle)
        req = SubmitRequest(releaseHandle=handle, sourceKey="mangadex", mangaId=42)
        # Hold the global semaphore so the first job stays queued/live for the
        # second submit — exercising the live-handle path (not the file path).
        await manager._global_sem.acquire()
        try:
            first_id, _ = await manager.submit(record, req)
            second_id, _status = await manager.submit(record, req)
            assert second_id == first_id  # same live job, no duplicate row
        finally:
            manager._global_sem.release()
        await manager.drain()


@respx.mock
@pytest.mark.asyncio
async def test_resubmit_completed_with_file_returns_same_job(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    """DL-03: resubmit of a completed handle whose CBZ exists returns the same id."""
    _mock_at_home(pages=2)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    first_id = resp.json()["jobId"]
    job = await _poll_until(dl_client, first_id)
    assert job["status"] == "completed"
    assert _exists(job["outputPath"])

    # Resubmit the same handle: the output file still exists → same jobId, no new job.
    resp2 = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    assert resp2.status_code == 200
    assert resp2.json()["jobId"] == first_id


@respx.mock
@pytest.mark.asyncio
async def test_resubmit_after_file_removed_mints_fresh_job(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    """D-27 re-grab: once the completed output is gone, resubmit mints a NEW job."""
    _mock_at_home(pages=2)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    first_id = resp.json()["jobId"]
    job = await _poll_until(dl_client, first_id)
    os.unlink(job["outputPath"])  # the file is gone → handle re-grabbable

    _mock_at_home(pages=2)
    resp2 = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    assert resp2.status_code == 200
    assert resp2.json()["jobId"] != first_id  # a fresh job


@respx.mock
@pytest.mark.asyncio
async def test_idempotency_does_at_most_one_stat_per_submit(
    dl_app: FastAPI, dl_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-05: the idempotency check is one os.path.exists per submit, not a loop."""
    _mock_at_home(pages=1)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    first_id = resp.json()["jobId"]
    await _poll_until(dl_client, first_id)

    import manga_gateway.jobs.manager as mgr_mod  # noqa: PLC0415

    calls = {"n": 0}
    real_exists = os.path.exists

    def _counting_exists(p: str) -> bool:
        calls["n"] += 1
        return real_exists(p)

    monkeypatch.setattr(mgr_mod.os.path, "exists", _counting_exists)
    resp2 = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    assert resp2.json()["jobId"] == first_id
    assert calls["n"] == 1  # exactly ONE stat for the idempotency check


# ─────────────────────────── DL-07 remove() (manager) ───────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_remove_deletes_row_and_projection(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=1)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    await _poll_until(dl_client, job_id)

    manager = dl_app.state.job_manager
    ok = await manager.remove(job_id, delete_data=False)
    assert ok is True
    assert manager.get(job_id) is None
    assert await manager._store.get(job_id) is None


@respx.mock
@pytest.mark.asyncio
async def test_remove_delete_data_unlinks_output(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=1)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    job = await _poll_until(dl_client, job_id)
    out = job["outputPath"]
    assert _exists(out)

    manager = dl_app.state.job_manager
    ok = await manager.remove(job_id, delete_data=True)
    assert ok is True
    assert not _exists(out)  # the job's own output file is unlinked


@pytest.mark.asyncio
async def test_remove_unknown_returns_false(dl_app: FastAPI) -> None:
    async with dl_app.router.lifespan_context(dl_app):
        manager = dl_app.state.job_manager
        assert await manager.remove("j_nope", delete_data=True) is False


# ─────────────────────────── DL-06 GET /downloads/{id} ──────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_single_download_returns_job(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=1)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    await _poll_until(dl_client, job_id)

    one = await dl_client.get(f"/downloads/{job_id}")
    assert one.status_code == 200
    body = one.json()
    assert body["jobId"] == job_id
    assert body["status"] == "completed"


@pytest.mark.asyncio
async def test_get_single_download_unknown_returns_404(
    dl_client: httpx.AsyncClient,
) -> None:
    resp = await dl_client.get("/downloads/j_does-not-exist")
    assert resp.status_code == 404
    # Pitfall 8 / issue #2: a genuine 404, wrapped in the contract Error envelope
    # with code ``not_found`` — never ``code: 'internal'``.
    assert resp.json()["error"]["code"] == "not_found"


# ─────────────────────────── DL-07 DELETE /downloads/{id} ────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_delete_download_with_delete_data_unlinks_file(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=1)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    job = await _poll_until(dl_client, job_id)
    out = job["outputPath"]
    assert _exists(out)

    deleted = await dl_client.delete(f"/downloads/{job_id}?deleteData=true")
    assert deleted.status_code == 204
    assert deleted.content == b""  # no body
    assert not _exists(out)
    # The job is gone from the listing.
    listing = await dl_client.get("/downloads")
    assert all(j["jobId"] != job_id for j in listing.json()["jobs"])


@respx.mock
@pytest.mark.asyncio
async def test_delete_download_without_delete_data_keeps_file(
    dl_app: FastAPI, dl_client: httpx.AsyncClient
) -> None:
    _mock_at_home(pages=1)
    handle = _mint_handle(dl_app)
    resp = await dl_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    job = await _poll_until(dl_client, job_id)
    out = job["outputPath"]

    deleted = await dl_client.delete(f"/downloads/{job_id}")
    assert deleted.status_code == 204
    assert _exists(out)  # deleteData defaults false → file kept (D-28)


@pytest.mark.asyncio
async def test_delete_download_unknown_returns_404(
    dl_client: httpx.AsyncClient,
) -> None:
    resp = await dl_client.delete("/downloads/j_unknown")
    assert resp.status_code == 404


# ─────────────────────────── PLAT-03 restart staging sweep ──────────────────


@pytest.mark.asyncio
async def test_startup_sweeps_orphan_staging_temp(tmp_path: Path) -> None:
    """PLAT-03: a stray ``*.cbz.tmp`` is swept on startup; completed output survives."""
    out_root = tmp_path / "out"
    manga_dir = out_root / "manga-42"
    manga_dir.mkdir(parents=True)
    orphan = manga_dir / "tmpcrash.cbz.tmp"  # crash-left staging temp
    orphan.write_bytes(b"partial")
    completed = manga_dir / "Solo Leveling.cbz"  # a real finished archive
    completed.write_bytes(b"done")

    app = create_app(
        Settings(
            api_key=TEST_API_KEY,
            db_path=str(tmp_path / "jobs.db"),
            output_root=str(out_root),
        )
    )
    async with app.router.lifespan_context(app):
        pass

    assert not orphan.exists()  # orphan staging temp swept
    assert completed.exists()  # completed output untouched
