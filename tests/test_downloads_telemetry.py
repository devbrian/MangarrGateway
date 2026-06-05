"""Download-surface telemetry-parity tests (260605-mnv).

Proves the four ``/downloads`` routes actually populate the umbrella ``request``
event end-to-end — at parity with the search/recent surfaces — by reusing the
EXISTING enrichment seam (``stash_request_blob`` / ``stash_request_result``). This
file does NOT duplicate the RequestBlob/MetricEvent lockstep drift assertions (those
live in ``tests/test_metrics_request_blob.py`` and stay the single source of truth);
it asserts no new schema is introduced — only that the downloads routes stash onto
the request event:

* POST /downloads → ``request_blob.body`` carries camelCase ``chapterNumbers``, even
  on the rejection/expired-handle path; the finer reject reason / idempotent hit ride
  ``warnings_summary`` (bad_handle / malformed_body / invalid_request), omitted on the
  clean new-submit path.
* GET /downloads → ``result_count == len(jobs)`` with no disk read.
* GET/DELETE /downloads/{id} → a body-less ``request_blob`` (``deleteData`` rides
  ``query_string`` for free).

Each assertion reads the request event back from ``app.state.metric_snapshot`` (the
on-disk ring, the system of record) after a flush, selecting the target event by its
``endpoint`` label and recency — NEVER by a hardcoded request_id (the middleware mints
ids from a process-global counter, so a full-suite run is not deterministically 1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI

from manga_gateway.app import create_app
from manga_gateway.config import Settings

from .conftest import TEST_API_KEY
from .test_downloads import _mint_handle, _mock_at_home, _poll_until

_BASE_URL = "http://testserver/api/v1"


@pytest.fixture
def tel_app(tmp_path: Path) -> FastAPI:
    """App with the disk ring store live (separate metrics/jobs DBs + tmp dirs).

    Mirrors ``test_metrics_endpoints.py::metrics_app`` so the on-disk ring store opens
    — degraded in-memory mode does NOT serve ring reads (260604-wm2).
    """
    return create_app(
        Settings(
            api_key=TEST_API_KEY,
            metrics_db_path=str(tmp_path / "metrics.db"),
            log_dir=str(tmp_path / "logs"),
            output_root=str(tmp_path / "out"),
            db_path=str(tmp_path / "jobs.db"),
        )
    )


@pytest_asyncio.fixture
async def tel_client(tel_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=tel_app)
    async with tel_app.router.lifespan_context(tel_app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_BASE_URL,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            yield ac


async def _read_request_event(app: FastAPI, endpoint: str) -> dict:
    """Return the most-recent ``request`` event for ``endpoint`` (flush first).

    Selects by the ``endpoint`` label + recency — never by request_id (the
    process-global counter is not deterministically 1 in a full-suite run).
    """
    ring = app.state.metric_snapshot
    await ring.flush()
    events = [
        e
        for e in await ring.recent_calls(50)
        if e["kind"] == "request" and e.get("endpoint") == endpoint
    ]
    assert events, f"no request event found for endpoint {endpoint!r}"
    # recent_calls is ordered newest-first (ORDER BY ts DESC) — take the most recent.
    return events[0]


# ─────────────────────────── POST blob carries chapterNumbers ────────────────


@respx.mock
@pytest.mark.asyncio
async def test_post_blob_carries_chapter_numbers(
    tel_app: FastAPI, tel_client: httpx.AsyncClient
) -> None:
    """A successful submit's request_blob carries camelCase chapterNumbers."""
    _mock_at_home(pages=2)
    handle = _mint_handle(tel_app)

    resp = await tel_client.post(
        "/downloads",
        json={
            "releaseHandle": handle,
            "sourceKey": "mangadex",
            "chapterNumbers": [12.5],
            "mangaTitle": "X",
        },
    )
    assert resp.status_code == 200

    ev = await _read_request_event(tel_app, "POST /downloads")
    blob = ev["request_blob"]
    assert blob is not None
    assert blob["method"] == "POST"
    # body is model_dump(by_alias=True) → camelCase chapterNumbers.
    assert blob["body"]["chapterNumbers"] == [12.5]


# ─────────────────────────── rejected submit still carries body + reason ─────


@pytest.mark.asyncio
async def test_rejected_submit_still_carries_body_and_reason(
    tel_app: FastAPI, tel_client: httpx.AsyncClient
) -> None:
    """An expired/unknown-handle submit (still a valid body) stays reconstructable
    AND conveys the finer bad_handle reason."""
    resp = await tel_client.post(
        "/downloads",
        json={
            "releaseHandle": "nope",
            "sourceKey": "mangadex",
            "chapterNumbers": [1],
        },
    )
    assert resp.status_code == 400

    ev = await _read_request_event(tel_app, "POST /downloads")
    blob = ev["request_blob"]
    assert blob is not None
    # The rejected submit's body is still reconstructable.
    assert blob["body"]["chapterNumbers"] == [1]
    assert ev["warnings_summary"] == [{"source_key": "mangadex", "code": "bad_handle"}]


# ─────────────────────────── malformed / invalid reason codes ────────────────


@pytest.mark.asyncio
async def test_malformed_body_reason_code(
    tel_app: FastAPI, tel_client: httpx.AsyncClient
) -> None:
    """A non-dict JSON body (a JSON array) conveys the malformed_body reason."""
    resp = await tel_client.post("/downloads", json=[1, 2])
    assert resp.status_code == 400

    ev = await _read_request_event(tel_app, "POST /downloads")
    assert ev["warnings_summary"] == [
        {"source_key": "_request", "code": "malformed_body"}
    ]
    # The unparseable body is captured as None (no model to dump).
    assert ev["request_blob"]["body"] is None


@pytest.mark.asyncio
async def test_invalid_request_reason_code(
    tel_app: FastAPI, tel_client: httpx.AsyncClient
) -> None:
    """A dict body that fails SubmitRequest validation conveys invalid_request."""
    # Missing the required releaseHandle → ValidationError.
    resp = await tel_client.post("/downloads", json={"sourceKey": "mangadex"})
    assert resp.status_code == 400

    ev = await _read_request_event(tel_app, "POST /downloads")
    assert ev["warnings_summary"] == [
        {"source_key": "_request", "code": "invalid_request"}
    ]
    assert ev["request_blob"]["body"] is None


# ─────────────────────────── clean submit omits warnings_summary ─────────────


@respx.mock
@pytest.mark.asyncio
async def test_clean_submit_omits_warnings_summary(
    tel_app: FastAPI, tel_client: httpx.AsyncClient
) -> None:
    """A clean new submit populates request_blob but OMITS warnings_summary."""
    _mock_at_home(pages=1)
    handle = _mint_handle(tel_app)

    resp = await tel_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    ev = await _read_request_event(tel_app, "POST /downloads")
    assert ev["request_blob"] is not None
    # warnings_summary is None/absent on the clean path (parity with search).
    assert not ev.get("warnings_summary")


# ─────────────────────────── GET list reports result_count ───────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_list_reports_result_count(
    tel_app: FastAPI, tel_client: httpx.AsyncClient
) -> None:
    """GET /downloads reports result_count == the number of jobs listed."""
    _mock_at_home(pages=1)
    handle = _mint_handle(tel_app)
    resp = await tel_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    await _poll_until(tel_client, job_id)

    listing = await tel_client.get("/downloads")
    n_jobs = len(listing.json()["jobs"])
    assert n_jobs >= 1

    ev = await _read_request_event(tel_app, "GET /downloads")
    assert ev["result_count"] == n_jobs


# ─────────────────────────── per-id routes stash a body-less blob ────────────


@respx.mock
@pytest.mark.asyncio
async def test_per_id_routes_stash_blob(
    tel_app: FastAPI, tel_client: httpx.AsyncClient
) -> None:
    """GET/DELETE /downloads/{id} stash a body-less request_blob; deleteData rides
    query_string for free on the DELETE."""
    _mock_at_home(pages=1)
    handle = _mint_handle(tel_app)
    resp = await tel_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "mangadex", "mangaId": 42},
    )
    job_id = resp.json()["jobId"]
    await _poll_until(tel_client, job_id)

    one = await tel_client.get(f"/downloads/{job_id}")
    assert one.status_code == 200
    get_ev = await _read_request_event(tel_app, "GET /downloads/{id}")
    get_blob = get_ev["request_blob"]
    assert get_blob is not None
    assert get_blob["method"] == "GET"
    assert get_blob["body"] is None

    deleted = await tel_client.delete(f"/downloads/{job_id}?deleteData=true")
    assert deleted.status_code == 204
    del_ev = await _read_request_event(tel_app, "DELETE /downloads/{id}")
    del_blob = del_ev["request_blob"]
    assert del_blob is not None
    assert del_blob["method"] == "DELETE"
    assert del_blob["body"] is None
    # deleteData rides the request_blob.query_string for free — no bespoke field.
    assert "deleteData=true" in del_blob["query_string"]
