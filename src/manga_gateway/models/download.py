"""Download-surface wire DTOs (DL-01/02, JOB-02 — openapi.yaml lines 282-321).

All three models extend the shared :class:`~manga_gateway.models.common.ApiModel`
base (single owner of ``populate_by_name=True``) and carry camelCase
``Field(alias=...)`` matching ``manga-gateway.openapi.yaml`` exactly — NEVER
re-declaring ``model_config`` (Pitfall 3); routes serialize with
``response_model_by_alias=True``.

Three DTOs:

* :class:`SubmitRequest` — ``POST /downloads`` body. ``downloadUrl`` is accepted but
  ADVISORY only: the gateway resolves the manifest from the ``releaseHandle`` and
  never fetches a client-supplied URL (SEC-01 SSRF guard).
* :class:`SubmitResponse` — ``jobId`` is a REQUIRED key whose VALUE is ``null`` on
  rejection (an unresolvable/expired handle).
* :class:`DownloadJob` — the queue/history projection. Its ``status`` keeps the FULL
  contract enum incl. ``warning``/``paused`` (D-29a) — broader than the strict
  internal :class:`~manga_gateway.jobs.model.JobStatus`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ApiModel


class SubmitRequest(ApiModel):
    """``POST /downloads`` request body (openapi.yaml SubmitRequest).

    Required: ``releaseHandle``, ``sourceKey``. ``downloadUrl`` is advisory and
    IGNORED for fetch routing (SEC-01) — the gateway resolves from the handle.
    """

    release_handle: str = Field(alias="releaseHandle")
    download_url: str | None = Field(default=None, alias="downloadUrl")
    source_key: str = Field(alias="sourceKey")
    title: str | None = None
    manga_id: int | None = Field(default=None, alias="mangaId")
    manga_title: str | None = Field(default=None, alias="mangaTitle")
    chapter_ids: list[int] | None = Field(default=None, alias="chapterIds")
    chapter_numbers: list[float] | None = Field(default=None, alias="chapterNumbers")
    output_format: Literal["cbz", "cbt", "folder"] = Field(
        default="cbz", alias="outputFormat"
    )


class SubmitResponse(ApiModel):
    """``POST /downloads`` response (openapi.yaml SubmitResponse).

    ``jobId`` is a required KEY; its VALUE is ``null`` on rejection (else the
    DownloadId). ``status`` is the just-scheduled state (``queued``/``resolving``).
    """

    job_id: str | None = Field(alias="jobId")
    status: Literal["queued", "resolving"] | None = None
    message: str | None = None


class DownloadJob(ApiModel):
    """Queue/history projection of a job (openapi.yaml DownloadJob).

    Required: ``jobId``, ``title``, ``status``. ``status`` keeps the FULL contract
    enum incl. ``warning``/``paused`` (D-29a) — broader than internal JobStatus.
    """

    job_id: str = Field(alias="jobId")
    title: str
    source_key: str | None = Field(default=None, alias="sourceKey")
    status: Literal[
        "queued",
        "resolving",
        "downloading",
        "archiving",
        "completed",
        "failed",
        "warning",
        "paused",
    ]
    total_bytes: int = Field(default=0, alias="totalBytes")
    remaining_bytes: int = Field(default=0, alias="remainingBytes")
    total_pages: int | None = Field(default=None, alias="totalPages")
    downloaded_pages: int | None = Field(default=None, alias="downloadedPages")
    eta_seconds: int | None = Field(default=None, alias="etaSeconds")
    output_path: str | None = Field(default=None, alias="outputPath")
    message: str | None = None
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")


class DownloadJobList(ApiModel):
    """``GET /downloads`` response (openapi.yaml ``{jobs: [DownloadJob]}``).

    The queue/history read model projected from the JobManager's in-memory dict
    (DL-04/05) — never a per-poll disk rescan.
    """

    jobs: list[DownloadJob]
