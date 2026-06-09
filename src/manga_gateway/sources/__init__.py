"""Concrete source subclasses (the only source-specific code; SRC-01).

``register_builtin_sources(registry)`` registers every built-in source into a
``SourceRegistry`` instance — invoked once in the app lifespan. Importing a source
module runs its ``@registry.register(...)`` decorator, so registration is keyed to
the instance passed here (the registry decorator binds to an INSTANCE, not a module
singleton).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..framework.registry import SourceRegistry


def register_builtin_sources(registry: SourceRegistry) -> None:
    """Register all built-in sources into ``registry`` (called in the lifespan)."""
    from .atsumaru import AtsumaruSource
    from .comix import ComixSource
    from .kagane import KaganeSource
    from .mangaball import MangaBallSource
    from .mangadex import MangaDexSource
    from .mangadot import MangadotSource
    from .weebcentral import WeebCentralSource

    registry.register("mangadex")(MangaDexSource)
    registry.register("comix")(ComixSource)
    registry.register("mangaball")(MangaBallSource)
    registry.register("mangadot")(MangadotSource)
    registry.register("atsumaru")(AtsumaruSource)
    registry.register("weebcentral")(WeebCentralSource)
    registry.register("kagane")(KaganeSource)
