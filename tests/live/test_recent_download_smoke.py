"""Parametrized live ``/recent → /downloads`` full-cycle smoke.

Issue #42 closes the gap left by ``test_download_smoke.py``: the existing
full-cycle smoke sources its ``downloadHandle`` from ``POST /search``, which
exercises the search-path resolution shape but NEVER hits the deferred-id
branch in ``ComixSource.fetch_manifest``. This module re-runs the same
search→submit→poll→CBZ shape but with the handle minted by ``GET /recent``
so the ``numeric_id == "DEFERRED"`` branch is the only one that can satisfy
the chain end-to-end (issue #42 spike #2: deferred chapter-id resolution).

Per-source autodiscovery (D-47) so any source advertising
``supportsRecent: true`` is exercised on the same guarantee — Comix is the
motivating case but MangaDex (real chapter-level recent feed) passes the
chain unchanged, which is the desired symmetry.

Sources that advertise ``supportsRecent: false`` in their live ``/caps``
response are skipped — same generic guard ``test_recent_smoke.py`` uses
(spec-conformant; no faked data).
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


async def test_recent_download_full_cycle(
    source_key: str, profile: LiveSmokeProfile, tmp_path: Path
) -> None:
    """Full /recent → submit → poll → CBZ cycle (issue #42 deferred path).

    Mirrors ``test_download_smoke::test_download_full_cycle`` but sources
    the handle from ``GET /recent`` instead of ``POST /search`` so the
    deferred-id branch in ``ComixSource.fetch_manifest`` is exercised
    end-to-end against live upstream + a real Patchright Cloudflare
    clearance.

    Tolerates per-release failure within ``profile.max_releases_to_try``
    (a top-of-recent slot may be a deleted/ad-only chapter). Raises only
    if no release in that window completes.
    """
    async with live_client_for(profile, tmp_path=tmp_path) as client:
        # Honor the source's own /caps declaration (spec-conformant skip).
        # Comix flips to True under #42; the skip remains for any future
        # source advertising supportsRecent=false.
        caps_resp = await client.get("/api/v1/caps")
        assert caps_resp.status_code == 200, (
            f"{source_key}: GET /caps failed: {caps_resp.status_code} "
            f"{caps_resp.text[:400]}"
        )
        caps = caps_resp.json().get("sources") or []
        cap = next((s for s in caps if s.get("key") == source_key), None)
        assert cap is not None, (
            f"{source_key}: missing from /caps sources — caps-advertisement regression"
        )
        if cap.get("supportsRecent") is False:
            pytest.skip(
                f"{source_key}: supportsRecent=false in /caps — no public "
                f"recent feed; nothing to exercise."
            )

        # 1. RECENT — fetch live recent feed.
        recent = await client.get(
            "/api/v1/recent",
            params={"sources": source_key, "limit": 10},
        )
        assert recent.status_code == 200, (
            f"{source_key}: GET /recent failed: "
            f"{recent.status_code} {recent.text[:400]}"
        )
        releases = recent.json().get("releases") or []
        assert releases, f"{source_key}: /recent returned no releases"

        # 2. SUBMIT — try the first N recent releases until one completes.
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
            check_response_conforms("/downloads", "POST", submit)

            submit_json = submit.json()
            job_id = submit_json.get("jobId")
            assert job_id is not None, (
                f"{source_key}: submit returned null jobId for handle "
                f"{handle!r}: {submit_json}"
            )

            # 3. POLL until terminal (timeout from profile / D-55 marker).
            job = await _poll_until_terminal(
                client, job_id, timeout_s=profile.download_timeout_s
            )
            if job["status"] != "completed":
                last_failure = job
                continue  # next release in the recent window

            # 4. ASSERT CBZ on disk (exercises the full deferred-resolve
            #    path: recent handle → fetch_manifest → DEFERRED branch
            #    → _series_chapters via Patchright → resolve to real
            #    numeric chapter id → fetch_via_browser → CBZ).
            output_path = job.get("outputPath")
            assert output_path, f"{source_key}: completed job missing outputPath: {job}"
            cbz_path = Path(output_path)
            names, size = await asyncio.to_thread(_assert_cbz_on_disk, cbz_path)
            print(
                f"\n[live recent→download {source_key}] CBZ verified: "
                f"{cbz_path.name} ({size} bytes, {len(names)} entries)"
            )
            return  # SUCCESS

        raise AssertionError(
            f"{source_key}: none of the first {profile.max_releases_to_try} "
            f"recent releases completed; last terminal state: {last_failure}"
        )
