"""``POST/GET /downloads`` — the download surface (DL-01/04/05, HDL-02).

Thin, source-agnostic routes (SRC-01) that delegate to the lifespan-owned
``JobManager``. ``POST /downloads`` resolves the opaque ``releaseHandle`` to a
``ResolutionRecord`` via the gateway's own ``HandleStore`` (SEC-01: the gateway NEVER
fetches a client-supplied URL — ``downloadUrl`` is advisory and ignored for routing) and
submits a job; an expired/unknown handle returns a ``SubmitResponse{jobId:null}`` at 400
— NOT the ``Error`` envelope (HDL-02 / RESEARCH Anti-Patterns), never a 5xx.

``GET /downloads`` returns the in-memory projection (``{jobs:[DownloadJob]}``) cheaply,
with no disk/SQLite read per poll (DL-05); the page manifest never crosses the wire
(PKG-01/R6).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from ...deps import get_handle_store, get_job_manager

# Runtime imports: FastAPI resolves the route's Annotated[T, Depends(...)] types at
# import time (``from __future__ import annotations`` makes them strings), so the
# dependency types cannot live under TYPE_CHECKING (search.py / recent.py convention).
from ...handles.store import HandleStore
from ...jobs.manager import JobManager
from ...metrics.context import (
    stash_request_blob,
    stash_request_meta,
    stash_request_result,
)
from ...models.download import (
    DownloadJob,
    DownloadJobList,
    SubmitRequest,
    SubmitResponse,
)

router = APIRouter()


def _submit_body(resp: SubmitResponse) -> dict[str, object]:
    """Serialize a ``SubmitResponse`` keeping ``jobId`` but dropping null optionals.

    The contract's ``status`` enum is ``[queued, resolving]`` (NOT nullable) and
    ``jobId`` is required-but-nullable. ``exclude_none`` would also drop ``jobId`` on
    the rejection path, so we re-add it explicitly: a ``null`` ``status``/``message`` is
    omitted (contract-conformant) while ``jobId`` is always present (CTRT-01).
    """
    body = resp.model_dump(by_alias=True, exclude_none=True)
    body["jobId"] = resp.job_id  # required key, even when null (rejection path)
    return body


def _reject(message: str) -> JSONResponse:
    """A 400 ``SubmitResponse{jobId:null}`` (the contract's documented 400 shape)."""
    return JSONResponse(
        status_code=400,
        content=_submit_body(SubmitResponse(job_id=None, message=message)),
    )


@router.post("/downloads", operation_id="submitDownload")
async def submit_download(
    request: Request,
    handle_store: Annotated[HandleStore, Depends(get_handle_store)],
    job_manager: Annotated[JobManager, Depends(get_job_manager)],
) -> JSONResponse:
    """Resolve the handle and submit a job (DL-01); reject bad/expired handles (HDL-02).

    The body is parsed manually (not via a typed ``SubmitRequest`` parameter) so a
    malformed/invalid body returns the contract's documented 400 shape for THIS
    endpoint — a ``SubmitResponse{jobId:null}`` — instead of the generic ``Error``
    envelope FastAPI's validation handler would emit (CTRT-01: openapi documents the
    POST 400 as a SubmitResponse, HDL-02).

    Telemetry parity (260605-mnv): the parsed body (incl. ``chapterNumbers``) is
    stashed onto the umbrella ``request`` event via the existing enrichment seam, and
    the finer reject reason / idempotent-hit ride ``warnings_summary`` — both reuse
    ``stash_request_blob`` / ``stash_request_result``, NO new schema.
    """
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        # Body is unparseable → no model to dump; capture path/query only and convey
        # the finer reason via warnings_summary (out-of-contract telemetry only).
        stash_request_blob(
            method="POST",
            path=request.url.path,
            query_string=request.url.query,
            body=None,
        )
        stash_request_result(
            result_count=0,
            warnings_summary=[{"source_key": "_request", "code": "malformed_body"}],
        )
        return _reject("malformed request body")
    if not isinstance(payload, dict):
        stash_request_blob(
            method="POST",
            path=request.url.path,
            query_string=request.url.query,
            body=None,
        )
        stash_request_result(
            result_count=0,
            warnings_summary=[{"source_key": "_request", "code": "malformed_body"}],
        )
        return _reject("malformed request body")
    try:
        req = SubmitRequest.model_validate(payload)
    except ValidationError:
        # The model did not validate → no model_dump; convey invalid_request.
        stash_request_blob(
            method="POST",
            path=request.url.path,
            query_string=request.url.query,
            body=None,
        )
        stash_request_result(
            result_count=0,
            warnings_summary=[{"source_key": "_request", "code": "invalid_request"}],
        )
        return _reject("invalid submit request")

    # Stash the parsed body ONCE, BEFORE the early-return rejection paths below
    # (mirroring search.py's "stashed before the SRCH-05 early return so a 400 is
    # still reconstructable"). This carries chapterNumbers/mangaTitle/title/sourceKey/
    # outputFormat so the chapter number becomes visible in telemetry with zero schema
    # change. releaseHandle is a gateway-issued opaque token; redact_blob masks
    # denylisted keys at stash time and the body is capped at REQUEST_BLOB_BODY_CAP.
    stash_request_blob(
        method="POST",
        path=request.url.path,
        query_string=request.url.query,
        body=req.model_dump(by_alias=True),
    )

    record = handle_store.resolve(req.release_handle)
    if record is None:
        # Expired/unknown handle → SubmitResponse{jobId:null} at 400 (HDL-02), NOT the
        # {error:{code,message}} envelope (RESEARCH Anti-Patterns).
        stash_request_result(
            result_count=0,
            warnings_summary=[{"source_key": req.source_key, "code": "bad_handle"}],
        )
        return _reject("release no longer resolvable")
    # 260605-nqo: Mangarr's body sends title/mangaTitle/chapterNumbers null, so the
    # human-readable values live in the RESOLVED record — stash them onto the umbrella
    # request event (chapter_number Decimal float()'d at the telemetry boundary). Only
    # the success path has a record; rejection paths above add nothing.
    stash_request_meta(
        manga_title=record.manga_title,
        chapter_number=(
            float(record.chapter_number) if record.chapter_number is not None else None
        ),
    )
    job_id, status = await job_manager.submit(record, req)
    # SubmitResponse.status is the just-scheduled state (queued/resolving). An
    # idempotent return of an EXISTING live/completed job (DL-03) may carry any job
    # status, which is NOT a valid wire value here — surface only the scheduled states,
    # else omit the field (a null status would violate the non-nullable enum, CTRT-01).
    scheduled = status if status in ("queued", "resolving") else None
    if scheduled is None:
        # The manager returned an EXISTING job (DL-03 idempotent hit) rather than
        # scheduling a fresh one — convey that via warnings_summary. On the clean
        # new-submit path warnings_summary is OMITTED (parity: search omits it).
        stash_request_result(
            result_count=1,
            warnings_summary=[{"source_key": req.source_key, "code": "idempotent_hit"}],
        )
    return JSONResponse(
        content=_submit_body(SubmitResponse(job_id=job_id, status=scheduled))
    )


@router.get(
    "/downloads",
    operation_id="getDownloads",
    response_model=DownloadJobList,
    response_model_by_alias=True,
)
async def get_downloads(
    job_manager: Annotated[JobManager, Depends(get_job_manager)],
) -> DownloadJobList:
    """List live + finished jobs from the in-memory projection (DL-04/05).

    Reports ``result_count == len(jobs)`` on the umbrella ``request`` event (parity
    with search's merged result_count). The listing is computed ONCE over the
    in-memory projection — NO disk/SQLite read per poll (DL-05, poll-friendliness).
    """
    jobs = job_manager.list()
    stash_request_result(result_count=len(jobs), warnings_summary=[])
    return DownloadJobList(jobs=jobs)


@router.get(
    "/downloads/{job_id}",
    operation_id="getDownload",
    response_model=DownloadJob,
    response_model_by_alias=True,
)
async def get_download(
    job_id: str,
    request: Request,
    job_manager: Annotated[JobManager, Depends(get_job_manager)],
) -> DownloadJob:
    """Return one job (DL-06) or a 404 for an unknown id (Pitfall 8, issue #2).

    The 404 is raised as an ``HTTPException`` so ``errors.py``'s ``_http_exc``
    serializes it as the contract Error envelope
    ``{error:{code:not_found,message}}`` — never the ``code: internal`` envelope
    a 5xx would produce (T-03-14). Stashes a body-less ``request_blob`` for
    path/query reconstruction (260605-mnv); job-found vs 404 is already visible via
    the status code, so NO warnings_summary is added here.

    260605-nqo: also stashes the resolved manga_title/chapter_number read from the
    INTERNAL job (``job_manager.get`` — the wire DTO has no chapter_number). The 404
    path (job None) adds nothing.
    """
    stash_request_blob(
        method="GET",
        path=request.url.path,
        query_string=request.url.query,
        body=None,
    )
    internal = job_manager.get(job_id)
    job = job_manager.get_dto(job_id)
    if internal is None or job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    stash_request_meta(
        manga_title=internal.manga_title,
        chapter_number=internal.chapter_number,
    )
    return job


_TRUE_TOKENS = {"true", "1", "yes", "on"}


def _parse_delete_data(raw: str | None) -> bool:
    """Tolerantly coerce the ``deleteData`` query flag (T-02-08 precedent).

    The contract documents only 204/404 for ``removeDownload`` — a malformed
    ``deleteData`` (e.g. ``"null"``) must NOT surface an undocumented 400 (the
    ``/recent`` tolerant-parse discipline). Anything not a recognized truthy token
    falls back to ``False`` (the contract default — keep the files).
    """
    return raw is not None and raw.strip().lower() in _TRUE_TOKENS


@router.delete(
    "/downloads/{job_id}",
    operation_id="removeDownload",
    status_code=204,
)
async def remove_download(
    job_id: str,
    request: Request,
    job_manager: Annotated[JobManager, Depends(get_job_manager)],
    delete_data: Annotated[str | None, Query(alias="deleteData")] = None,
) -> Response:
    """Delete a job — 204 (DL-07), 404 for an unknown id (Pitfall 8).

    When ``deleteData`` is truthy the manager unlinks ONLY the job's own
    gateway-computed output + staging temps, never a client-supplied path (T-03-12).
    ``deleteData`` is parsed tolerantly so a malformed value never surfaces an
    undocumented 400 (only 204/404 are documented). Returns an empty 204 on success.

    Stashes a body-less ``request_blob`` (260605-mnv); ``deleteData`` rides
    ``request_blob.query_string`` for free, so NO bespoke field is added; job-found vs
    404 is visible via the status code, so NO warnings_summary is added here.

    260605-nqo: looks up the internal job BEFORE ``remove`` (remove deletes the row)
    and stashes its resolved manga_title/chapter_number. The 404 path adds nothing.
    """
    stash_request_blob(
        method="DELETE",
        path=request.url.path,
        query_string=request.url.query,
        body=None,
    )
    # MUST precede remove() — remove pops the projection / deletes the row.
    internal = job_manager.get(job_id)
    if internal is not None:
        stash_request_meta(
            manga_title=internal.manga_title,
            chapter_number=internal.chapter_number,
        )
    ok = await job_manager.remove(job_id, delete_data=_parse_delete_data(delete_data))
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return Response(status_code=204)
