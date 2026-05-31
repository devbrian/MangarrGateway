"""Promoted live-smoke helpers (D-48 / D-55).

Three helpers shared by every parametrized live-smoke module in this phase:

* ``_poll_until_terminal`` — drive ``GET /downloads`` until a job reaches a
  ``(completed | failed)`` terminal status, logging every transition.
* ``_assert_cbz_on_disk`` — sync-IO CBZ inspector (Pillow ``verify()`` per
  entry, > 1024 bytes total) — must be called from an async test via
  ``await asyncio.to_thread(...)`` (ASYNC ruff rule).
* ``live_client_for`` — ``@asynccontextmanager`` that boots a real app +
  lifespan, conditionally awaits ``solver.warm()`` per profile, and yields
  an authenticated ``httpx.AsyncClient``.

The helpers are extracted verbatim from ``tests/live/test_comix_e2e_live.py``
(the only live test that exercised the full search → handle → CBZ slice
prior to Phase 5) so existing behavior is preserved. Plan 03 wires conftest
fixtures around them; Plan 04 ships the five parametrized smoke modules
that import these.
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import schemathesis
from PIL import Image
from schemathesis import CheckContext
from schemathesis.core.transport import Response as SchemathesisResponse
from schemathesis.specs.openapi.checks import response_schema_conformance

from manga_gateway.app import create_app
from manga_gateway.config import Settings

from .profiles._base import LiveSmokeProfile

# Contract of record (D-07) — same path tests/test_contract.py:53 reads.
_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "manga-gateway.openapi.yaml"

# Build the schema once at module load — every parametrized smoke module
# shares this view via ``check_response_conforms``. ``base_url`` is purely
# for path resolution by the check; live HTTP traffic goes through
# ``live_client_for``'s ASGI transport (which prepends its own ``base_url``).
# See ``.planning/phases/05-.../SPIKE-schemathesis.md`` for the locked 4.x
# response_schema_conformance recipe (W-04). Centralized here per
# CodeRabbit PR #33 review so a config change can't drift between modules.
_schema = schemathesis.openapi.from_path(_CONTRACT_PATH)
_schema.config.update(base_url="http://localhost/api/v1")


def check_response_conforms(
    operation_path: str, method: str, response: httpx.Response
) -> None:
    """Apply schemathesis ``response_schema_conformance`` to a live response.

    Centralized recipe shared by every parametrized live smoke module so the
    schemathesis 4.x check context, config, and schema view stay coherent
    across the per-endpoint smokes (D-54 / CTRT-01). See ``SPIKE-schemathesis.md``.
    """
    op = _schema[operation_path][method]
    case = op.Case()
    # verify=False — the ASGITransport never establishes TLS.
    s_response = SchemathesisResponse.from_httpx(response, verify=False)
    ctx = CheckContext(
        override=None,
        auth=None,
        headers=None,
        config=_schema.config.checks,
        transport_kwargs=None,
    )
    response_schema_conformance(ctx, s_response, case)

# One canonical test-only API key for all parametrized live tests. The literal
# self-advertises as non-prod so a leaked log line cannot be confused with a
# real secret (Phase 5 RESEARCH Security Domain).
_TEST_API_KEY = "test-live-smoke-key-DO-NOT-LOG-IN-PROD"


async def _poll_until_terminal(
    client: httpx.AsyncClient, job_id: str, *, timeout_s: float
) -> dict[str, Any]:
    """Poll ``GET /downloads`` until the job reaches a terminal status.

    Logs every status transition so a stuck job can be diagnosed in real time.
    Promoted verbatim from ``tests/live/test_comix_e2e_live.py:68-102``.
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
                    f"[live poll {elapsed:5.1f}s] {last_status or '(submit)'} "
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

    Promoted verbatim from ``tests/live/test_comix_e2e_live.py:105-122``.
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


@asynccontextmanager
async def live_client_for(
    profile: LiveSmokeProfile, tmp_path: Path | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """Boot a real gateway app + lifespan + ``httpx.AsyncClient`` for a smoke test.

    Conditionally awaits ``app.state.solver.warm()`` (60s cap) when the profile
    declares ``needs_solver_warm=True`` — Comix needs the warm Cloudflare
    clearance before issuing requests, MangaDex doesn't.

    ``tmp_path`` defaults to a Windows-portable system temp path; Plan 04
    smoke modules pass pytest's ``tmp_path`` fixture explicitly so the default
    is rarely hit.

    Production ``cloudflare_headless`` default at ``src/manga_gateway/config.py``
    is ``True``, so the Comix profile's ``warm()`` runs headless and is safe on
    CI runners with no display (W-01). A per-source override knob is intentionally
    NOT plumbed here — revisit if a future source needs a non-default value.
    """
    if tmp_path is None:
        tmp_path = Path(tempfile.gettempdir()) / "live-smoke" / profile.source_key
    await asyncio.to_thread(tmp_path.mkdir, parents=True, exist_ok=True)

    settings = Settings(
        api_key=_TEST_API_KEY,
        output_root=str(tmp_path / "out"),
        db_path=str(tmp_path / "jobs.db"),
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        if profile.needs_solver_warm:
            await asyncio.wait_for(app.state.solver.warm(), timeout=60.0)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers={"X-Api-Key": _TEST_API_KEY},
            timeout=120.0,
        ) as client:
            yield client
