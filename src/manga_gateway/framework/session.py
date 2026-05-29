"""Shared secure-site session seam (R1).

Holds the single ``Transport`` by reference. The session that *finds* a release
is the session that *downloads* it — so search and download share this one
instance, built once in the lifespan. Phase 1 only establishes the seam.
"""

from __future__ import annotations

from .transport import Transport


class SessionManager:
    """Owns the one shared outbound transport for the whole process (R1)."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    @property
    def transport(self) -> Transport:
        return self._transport
