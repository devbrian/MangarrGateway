"""Anti-bot solver seam (BOT-01).

Kept tiny so the Patchright -> Camoufox escalation (Phase 4) is a config flip.
Phase 1 uses ``NoopSolver`` (MangaDex et al. need no challenge solving).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class Clearance:
    """Captured anti-bot clearance: cookies + the UA they were issued for."""

    cookies: dict[str, str]
    user_agent: str


@runtime_checkable
class AntiBotSolver(Protocol):
    """Resolves a per-source anti-bot challenge into a reusable clearance."""

    async def get_clearance(self, source_key: str) -> Clearance | None:
        """Return clearance for ``source_key``, or ``None`` if none needed."""
        ...


class NoopSolver:
    """Default solver: no challenge to solve (BOT-01 Phase 1 default)."""

    async def get_clearance(self, source_key: str) -> None:
        return None
