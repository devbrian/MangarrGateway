"""Download wire-DTO + config-knob tests (Task 3 — DL-01/02, JOB-01/02, D-11/29a/30/31).

The three DTOs (``SubmitRequest``/``SubmitResponse``/``DownloadJob``) must match
``manga-gateway.openapi.yaml`` lines 282-321 field-for-field: camelCase aliases on
the wire, snake_case Python attributes, correct required/nullable semantics, and the
FULL status enum (incl. ``warning``/``paused``, kept per D-29a) on ``DownloadJob``.

The config tests assert the three new ops knobs (``max_concurrent_chapters``,
``image_fetch_concurrency``, ``db_path``) ship with the right defaults and honor the
``GATEWAY_`` env override path (D-11) — same treatment as host/port/output_root, NOT
the api_key exclusion.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from manga_gateway.config import load_settings
from manga_gateway.models.download import (
    DownloadJob,
    SubmitRequest,
    SubmitResponse,
)


def test_submit_request_parses_camelcase_and_defaults() -> None:
    req = SubmitRequest.model_validate(
        {
            "releaseHandle": "h-123",
            "sourceKey": "mangadex",
            "mangaId": 7,
            "outputFormat": "cbz",
        }
    )
    assert req.release_handle == "h-123"
    assert req.source_key == "mangadex"
    assert req.manga_id == 7
    # outputFormat defaults to cbz when omitted.
    assert (
        SubmitRequest.model_validate(
            {"releaseHandle": "h", "sourceKey": "s"}
        ).output_format
        == "cbz"
    )


def test_submit_request_requires_handle_and_source() -> None:
    with pytest.raises(ValidationError):
        SubmitRequest.model_validate({"sourceKey": "mangadex"})
    with pytest.raises(ValidationError):
        SubmitRequest.model_validate({"releaseHandle": "h-123"})


def test_submit_request_download_url_optional() -> None:
    req = SubmitRequest.model_validate(
        {"releaseHandle": "h", "sourceKey": "s", "downloadUrl": "ignored://x"}
    )
    # downloadUrl is accepted but advisory only (IGNORED for fetch routing, SEC-01).
    assert req.download_url == "ignored://x"
    no_url = SubmitRequest.model_validate({"releaseHandle": "h", "sourceKey": "s"})
    assert no_url.download_url is None


def test_submit_response_emits_null_jobid_key() -> None:
    body = SubmitResponse(job_id=None).model_dump(by_alias=True)
    # jobId is a required KEY whose VALUE is null on rejection (contract).
    assert "jobId" in body
    assert body["jobId"] is None


def test_submit_response_round_trips() -> None:
    resp = SubmitResponse(job_id="j_abc", status="queued", message=None)
    body = resp.model_dump(by_alias=True)
    assert body["jobId"] == "j_abc"
    assert body["status"] == "queued"


def test_download_job_emits_full_camelcase_keyset() -> None:
    job = DownloadJob(
        job_id="j_1",
        title="Chapter 1",
        status="downloading",
        created_at="2026-05-29T00:00:00+00:00",
        updated_at="2026-05-29T00:01:00+00:00",
    )
    body = job.model_dump(by_alias=True)
    for key in (
        "jobId",
        "title",
        "status",
        "totalBytes",
        "remainingBytes",
        "totalPages",
        "downloadedPages",
        "etaSeconds",
        "outputPath",
        "createdAt",
        "updatedAt",
        "completedAt",
    ):
        assert key in body, f"missing wire key {key}"
    # Numeric defaults per contract.
    assert body["totalBytes"] == 0
    assert body["remainingBytes"] == 0


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "resolving",
        "downloading",
        "archiving",
        "completed",
        "failed",
        "warning",
        "paused",
    ],
)
def test_download_job_accepts_full_status_enum(status: str) -> None:
    # The wire DTO keeps the FULL enum incl. warning/paused (D-29a) — broader than
    # the strict internal JobStatus.
    job = DownloadJob(
        job_id="j",
        title="t",
        status=status,  # type: ignore[arg-type]
        created_at="2026-05-29T00:00:00+00:00",
        updated_at="2026-05-29T00:00:00+00:00",
    )
    assert job.status == status


def test_download_job_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        DownloadJob(
            job_id="j",
            title="t",
            status="exploded",  # type: ignore[arg-type]
            created_at="2026-05-29T00:00:00+00:00",
            updated_at="2026-05-29T00:00:00+00:00",
        )


def test_settings_concurrency_and_db_path_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('api_key = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8")
    settings = load_settings(cfg)
    assert settings.max_concurrent_chapters == 8  # D-30 (raised 3->8 for throughput)
    assert settings.image_fetch_concurrency == 6  # D-31
    assert settings.cloudflare_fetch_concurrency == 3  # chromium-default flip
    assert settings.db_path == "gateway.db"


def test_settings_env_overrides_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('api_key = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8")
    monkeypatch.setenv("GATEWAY_MAX_CONCURRENT_CHAPTERS", "9")
    monkeypatch.setenv("GATEWAY_IMAGE_FETCH_CONCURRENCY", "12")
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY", "8")
    monkeypatch.setenv("GATEWAY_DB_PATH", "/var/lib/gw.db")
    settings = load_settings(cfg)
    # Ops knobs honor the env override path (D-11) — same as host/port/output_root.
    assert settings.max_concurrent_chapters == 9
    assert settings.image_fetch_concurrency == 12
    assert settings.cloudflare_fetch_concurrency == 8
    assert settings.db_path == "/var/lib/gw.db"
