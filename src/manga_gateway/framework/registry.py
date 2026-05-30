"""Source registry seam (SRC-01).

Declarative decorator-based registration so adding one of the planned 50+
sources is a small subclass + ``@registry.register("key")`` — never custom
networking/interface code. Empty in Phase 1 (no sources registered yet).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .health import SourceHealth


class SourceRegistry:
    """Maps a ``sourceKey`` to its source class via a registration decorator."""

    def __init__(self) -> None:
        self._sources: dict[str, type[Any]] = {}

    def register(self, key: str) -> Callable[[type[Any]], type[Any]]:
        """Decorator: register a source class under ``key``."""

        def deco(cls: type[Any]) -> type[Any]:
            self._sources[key] = cls
            return cls

        return deco

    def get(self, key: str) -> type[Any] | None:
        return self._sources.get(key)

    def keys(self) -> list[str]:
        """Return all registered source keys (omit a ``sources`` filter = all)."""
        return list(self._sources.keys())

    def items(self) -> list[tuple[str, type[Any]]]:
        """Return ``(key, source_class)`` pairs for every registered source.

        Used by the application wiring to inspect per-source metadata (e.g.
        ``cls.antibot``) without hardcoding source keys — keeps source-specific
        knowledge in the source class, not in the framework or app shell.
        """
        return list(self._sources.items())

    def caps(self, health_map: dict[str, SourceHealth] | None = None) -> list[Any]:
        """Return per-source capabilities, threading per-source health (D-38).

        Each source's ``source_cap(health=...)`` reads its live breaker so a tripped
        source reports ``enabled=False``. ``health_map=None`` (or a missing key) keeps
        ``enabled=True`` — the MangaDex / no-anti-bot regression.
        """
        return [
            cls.source_cap(health=health_map.get(key) if health_map else None)
            for key, cls in self._sources.items()
        ]
