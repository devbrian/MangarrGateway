"""Shared secure-site session seam (R1).

Holds the outbound ``Transport`` references. The session that *finds* a release
is the session that *downloads* it — so search and download share one
authenticated identity (cookies/clearance/UA travel per-request, see
``context.py`` ``_clearance_kwargs``), built once in the lifespan.

Two transports, ONE identity (debug pool-starves-search-cooldown, 2026-06-17):
``transport`` backs the search/recent fan-out; ``download_transport`` backs the
download surface's manifest + image fetches. They are SEPARATE ``httpx.AsyncClient``
pools so a large download backlog can never exhaust the connection pool the search
surface needs (which would trip every source's failure-cooldown). When
``download_transport`` is omitted it falls back to ``transport`` — a single shared
pool, the pre-split behaviour — so existing single-transport callers/tests are
unchanged.
"""

from __future__ import annotations

from .transport import Transport


class SessionManager:
    """Owns the outbound transports for the whole process (R1).

    ``transport`` = search/recent pool; ``download_transport`` = download pool
    (defaults to ``transport`` for the single-pool case).
    """

    def __init__(
        self, transport: Transport, download_transport: Transport | None = None
    ) -> None:
        self._transport = transport
        # Fall back to the search transport so a single-arg construction keeps the
        # historic single-shared-pool behaviour (R1) unchanged.
        self._download_transport = (
            download_transport if download_transport is not None else transport
        )

    @property
    def transport(self) -> Transport:
        """The search/recent (general) outbound transport."""
        return self._transport

    @property
    def download_transport(self) -> Transport:
        """The download-surface outbound transport (separate pool; falls back to
        :attr:`transport` when no distinct download transport was provided)."""
        return self._download_transport
