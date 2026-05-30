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
    """
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return _reject("malformed request body")
    if not isinstance(payload, dict):
        return _reject("malformed request body")
    try:
        req = SubmitRequest.model_validate(payload)
    except ValidationError:
        return _reject("invalid submit request")

    record = handle_store.resolve(req.release_handle)
    if record is None:
        # Expired/unknown handle → SubmitResponse{jobId:null} at 400 (HDL-02), NOT the
        # {error:{code,message}} envelope (RESEARCH Anti-Patterns).
        return _reject("release no longer resolvable")
    job_id, status = await job_manager.submit(record, req)
    # SubmitResponse.status is the just-scheduled state (queued/resolving). An
    # idempotent return of an EXISTING live/completed job (DL-03) may carry any job
    # status, which is NOT a valid wire value here — surface only the scheduled states,
    # else omit the field (a null status would violate the non-nullable enum, CTRT-01).
    scheduled = status if status in ("queued", "resolving") else None
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
    """List live + finished jobs from the in-memory projection (DL-04/05)."""
    return DownloadJobList(jobs=job_manager.list())


@router.get(
    "/downloads/{job_id}",
    operation_id="getDownload",
    response_model=DownloadJob,
    response_model_by_alias=True,
)
async def get_download(
    job_id: str,
    job_manager: Annotated[JobManager, Depends(get_job_manager)],
) -> DownloadJob:
    """Return one job (DL-06) or a 404 for an unknown id (Pitfall 8, issue #2).

    The 404 is raised as an ``HTTPException`` so ``errors.py``'s ``_http_exc``
    serializes it as the contract Error envelope
    ``{error:{code:not_found,message}}`` — never the ``code: internal`` envelope
    a 5xx would produce (T-03-14).
    """
    job = job_manager.get_dto(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
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
    job_manager: Annotated[JobManager, Depends(get_job_manager)],
    delete_data: Annotated[str | None, Query(alias="deleteData")] = None,
) -> Response:
    """Delete a job — 204 (DL-07), 404 for an unknown id (Pitfall 8).

    When ``deleteData`` is truthy the manager unlinks ONLY the job's own
    gateway-computed output + staging temps, never a client-supplied path (T-03-12).
    ``deleteData`` is parsed tolerantly so a malformed value never surfaces an
    undocumented 400 (only 204/404 are documented). Returns an empty 204 on success.
    """
    ok = await job_manager.remove(job_id, delete_data=_parse_delete_data(delete_data))
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return Response(status_code=204)
