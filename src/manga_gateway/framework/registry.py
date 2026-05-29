"""Source registry seam (SRC-01).

Declarative decorator-based registration so adding one of the planned 50+
sources is a small subclass + ``@registry.register("key")`` — never custom
networking/interface code. Empty in Phase 1 (no sources registered yet).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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

    def caps(self) -> list[Any]:
        """Return per-source capabilities. Phase 1: ``[]`` (no sources)."""
        return [cls.source_cap() for cls in self._sources.values()]
