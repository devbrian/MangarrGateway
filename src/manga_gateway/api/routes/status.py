"""``GET /status`` — output roots + capabilities (STAT-01, operationId getStatus).

Contract-shaped ``StatusResponse``: ``isLocalhost`` is computed from the configured
bind host, ``outputRootFolders`` reports the gateway-determined output root (SEC-02 —
never client-supplied), ``version`` is the single source-of-truth gateway version, and
``capabilities`` advertises pause/formats/concurrency (placeholders per RESEARCH A6).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ... import __version__
from ...config import Settings
from ...deps import get_settings
from ...models.status import StatusCapabilities, StatusResponse

router = APIRouter()

# Hosts that mean "bound to the loopback interface" (AUTH-02).
_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}


@router.get("/status", response_model=StatusResponse, response_model_by_alias=True)
async def get_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> StatusResponse:
    """Return the gateway status (localhost flag, output roots, version, caps).

    ``maxConcurrentChapters`` reports the effective D-30 global from config (STAT-01);
    ``removesCompletedDownloads`` stays false — the gateway keeps output until an
    explicit DELETE (D-28). ``isLocalhost``/``outputRootFolders`` are gateway-determined
    (SEC-02), never client-supplied.
    """
    return StatusResponse(
        is_localhost=settings.host in _LOCALHOST_HOSTS,
        output_root_folders=[settings.output_root],
        version=__version__,
        capabilities=StatusCapabilities(
            max_concurrent_chapters=settings.max_concurrent_chapters,
        ),
    )
