"""Opaque ``downloadHandle`` store (HDL-01/02, D-15/D-16/D-17).

A handle is a random ``secrets.token_urlsafe`` token carrying ZERO structure
(D-15, 128-bit CSPRNG — never guessable/sequential). It maps in an in-memory
``TTLCache`` (ttl 3600s = 60 min, HDL-02) to a :class:`ResolutionRecord` that stores
only STABLE ids (the MangaDex chapter UUID) plus the advisory snapshot already known
from search. In-memory only — handles do NOT survive a restart (D-16).

Principle (D-17 / Pitfall 6): store STABLE data, resolve VOLATILE tokens fresh. The
at-home ``baseUrl`` / cookies are NEVER stored here or embedded in the handle (HDL-01) —
Phase 3 resolves them fresh at grab time.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import Decimal

from cachetools import TTLCache

# 60-min handle TTL (HDL-02, >= the 30-min Mangarr interactive-search floor).
_HANDLE_TTL_SECONDS = 3600
_HANDLE_MAXSIZE = 10_000


@dataclass(frozen=True)
class ResolutionRecord:
    """The resolve target + advisory snapshot for a minted handle (D-17).

    Stores STABLE ids (``chapter_id`` = MangaDex chapter UUID) and the metadata
    already known from search. NEVER stores the volatile at-home ``baseUrl`` /
    cookies (HDL-01 / Pitfall 6) — those are resolved fresh in Phase 3.
    """

    source_key: str
    chapter_id: str
    language: str
    title: str
    manga_title: str | None
    chapter_number: Decimal | None
    volume: int | None
    scanlation_group: str | None
    page_count: int | None


class HandleStore:
    """In-memory opaque-handle → ``ResolutionRecord`` store (HDL-01/02)."""

    def __init__(
        self, ttl: int = _HANDLE_TTL_SECONDS, maxsize: int = _HANDLE_MAXSIZE
    ) -> None:
        self._cache: TTLCache[str, ResolutionRecord] = TTLCache(
            maxsize=maxsize, ttl=ttl
        )

    def mint(self, record: ResolutionRecord) -> str:
        """Mint an opaque handle for ``record`` and return it (D-15 CSPRNG token)."""
        handle = secrets.token_urlsafe(16)  # opaque, zero structure, 128-bit
        self._cache[handle] = record
        return handle

    def resolve(self, handle: str) -> ResolutionRecord | None:
        """Resolve a handle to its record, or ``None`` if unknown/expired (Phase 3)."""
        return self._cache.get(handle)
