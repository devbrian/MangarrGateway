"""Parametrized live full-cycle download smoke.

Covers D-51 + DL-01..07 + HDL-02 + JOB-01 + PKG-01..04.

Absorbs the full-cycle pattern from ``tests/live/test_comix_e2e_live.py``
(deleted by this plan) and parametrizes it across every registered source via
``REGISTERED_KEYS`` (D-47). Each run drives the full slice end-to-end:

1. ``POST /search`` with ``profile.default_query`` against the live upstream.
2. ``POST /downloads`` with the returned ``releaseHandle`` (loops over the
   first ``profile.max_releases_to_try`` releases — some slots are ad-only or
   empty, so a single per-release failure is tolerated).
3. ``GET /downloads/{id}`` immediately after submit (DL-06 single-job lookup).
4. ``GET /downloads`` poll until terminal via ``_poll_until_terminal``
   (timeout = ``profile.download_timeout_s``, attached as
   ``pytest.mark.timeout`` by the conftest hook — D-55).
5. ``_assert_cbz_on_disk`` validates the produced CBZ (PKG-01..04).
6. ``DELETE /downloads/{id}`` returns 204 (D-51 DELETE phase — new vs the
   absorbed ``test_comix_e2e_live.py`` which did NOT exercise DELETE).

A second test (``test_submit_is_idempotent_on_handle``) re-submits the same
handle twice in a row and asserts both submits returned the same non-null
``jobId`` (HDL-02 idempotency, D-27).

Schemathesis ``response_schema_conformance`` is applied to the submit and
single-job GET responses per the locked ``SPIKE-schemathesis.md`` recipe
(D-54 / CTRT-01 live / W-04).

W-03 symmetry: this module and ``test_fixture_drift.py`` both use the same
``live_client_for(profile)`` harness so the autouse
``_restore_real_cloudflare_warm`` interacts with ONE solver per test —
single lifespan path, single failure mode.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ._helpers import (
    _assert_cbz_on_disk,
    _poll_until_terminal,
    check_response_conforms,
    live_client_for,
)
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile

pytestmark = [
    pytest.mark.live,
    pytest.mark.parametrize("source_key", REGISTERED_KEYS),
]


async def test_download_full_cycle(
    source_key: str, profile: LiveSmokeProfile, tmp_path: Path
) -> None:
    """Full D-51 cycle: search → submit → get → poll → CBZ → delete.

    Tolerates per-release failure within ``profile.max_releases_to_try``
    (some chapter slots are ad-only / rotted). Raises on the loop exhausting
    without a single completion.
    """
    async with live_client_for(profile, tmp_path=tmp_path) as client:
        # 1. SEARCH — real upstream through the gateway.
        search = await client.post(
            "/api/v1/search",
            json={
                "type": "chapter",
                "query": profile.default_query,
                "sources": [source_key],
            },
        )
        assert search.status_code == 200, (
            f"{source_key}: search failed: {search.status_code} {search.text[:400]}"
        )
        payload = search.json()
        releases = payload.get("releases") or []
        assert releases, (
            f"{source_key}: no releases for query={profile.default_query!r}; "
            f"if the upstream catalog changed, revise the profile's "
            f"default_query"
        )

        # 2. SUBMIT — try the first N releases until one completes.
        last_failure: dict[str, Any] | None = None
        for release in releases[: profile.max_releases_to_try]:
            handle = release["downloadHandle"]
            submit_body: dict[str, Any] = {
                "releaseHandle": handle,
                "sourceKey": source_key,
            }
            submit = await client.post("/api/v1/downloads", json=submit_body)
            assert submit.status_code == 200, (
                f"{source_key}: submit failed: {submit.status_code} {submit.text[:400]}"
            )
            # D-54: submit response conforms to OpenAPI.
            check_response_conforms("/downloads", "POST", submit)

            submit_json = submit.json()
            job_id = submit_json.get("jobId")
            assert job_id is not None, (
                f"{source_key}: submit returned null jobId for handle "
                f"{handle!r}: {submit_json}"
            )

            # 3. GET single job (DL-06) — issued BEFORE the poll so the
            #    one-shot lookup is exercised even if the job advances
            #    quickly to terminal.
            get_resp = await client.get(f"/api/v1/downloads/{job_id}")
            assert get_resp.status_code == 200, (
                f"{source_key}: GET /downloads/{job_id} failed: "
                f"{get_resp.status_code} {get_resp.text[:400]}"
            )
            # D-54: single-job GET response conforms to OpenAPI.
            check_response_conforms("/downloads/{jobId}", "GET", get_resp)

            # 4. POLL — drive the job to terminal (timeout from D-55 marker).
            job = await _poll_until_terminal(
                client, job_id, timeout_s=profile.download_timeout_s
            )
            if job["status"] != "completed":
                last_failure = job
                continue  # next release

            # 5. ASSERT CBZ on disk + validate entries.
            output_path = job.get("outputPath")
            assert output_path, f"{source_key}: completed job missing outputPath: {job}"
            cbz_path = Path(output_path)
            names, size = await asyncio.to_thread(_assert_cbz_on_disk, cbz_path)
            print(
                f"\n[live {source_key}] CBZ verified: {cbz_path.name} "
                f"({size} bytes, {len(names)} entries)"
            )

            # 6. DELETE phase (D-51) — new vs the absorbed e2e test.
            delete_resp = await client.delete(f"/api/v1/downloads/{job_id}")
            assert delete_resp.status_code == 204, (
                f"{source_key}: DELETE /downloads/{job_id} returned "
                f"{delete_resp.status_code}, expected 204: "
                f"{delete_resp.text[:400]}"
            )
            return  # SUCCESS

        raise AssertionError(
            f"{source_key}: none of the first {profile.max_releases_to_try} "
            f"releases completed; last terminal state: {last_failure}"
        )


async def test_submit_is_idempotent_on_handle(
    source_key: str, profile: LiveSmokeProfile, tmp_path: Path
) -> None:
    """Re-submitting the same handle returns the same jobId (HDL-02 / D-27).

    Submits the SAME ``releaseHandle`` twice in immediate succession. The
    gateway MUST return the same non-null ``jobId`` for both submits
    (idempotency on handle, not on submit body — Mangarr re-submits on
    retry).
    """
    async with live_client_for(profile, tmp_path=tmp_path) as client:
        # Search to obtain a fresh handle (handles are gateway-issued
        # tokens — opaque to the test, valid for >= 30 min per the
        # contract's HDL-01 TTL).
        search = await client.post(
            "/api/v1/search",
            json={
                "type": "chapter",
                "query": profile.default_query,
                "sources": [source_key],
            },
        )
        assert search.status_code == 200, (
            f"{source_key}: search failed: {search.status_code} {search.text[:400]}"
        )
        releases = search.json().get("releases") or []
        assert releases, (
            f"{source_key}: no releases for idempotency test "
            f"(query={profile.default_query!r})"
        )
        handle = releases[0]["downloadHandle"]
        body: dict[str, Any] = {
            "releaseHandle": handle,
            "sourceKey": source_key,
        }

        first = await client.post("/api/v1/downloads", json=body)
        assert first.status_code == 200, (
            f"{source_key}: first submit failed: {first.status_code} {first.text[:400]}"
        )
        first_id = first.json().get("jobId")
        assert first_id is not None, (
            f"{source_key}: first submit returned null jobId: {first.json()}"
        )

        second = await client.post("/api/v1/downloads", json=body)
        assert second.status_code == 200, (
            f"{source_key}: second submit failed: "
            f"{second.status_code} {second.text[:400]}"
        )
        second_id = second.json().get("jobId")
        assert second_id == first_id, (
            f"{source_key}: HDL-02 idempotency violated — first jobId="
            f"{first_id!r}, second jobId={second_id!r} for the same "
            f"handle (D-27 requires same jobId)"
        )
