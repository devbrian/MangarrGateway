"""Concrete source subclasses (the only source-specific code; SRC-01).

``register_builtin_sources(registry)`` registers every built-in source into a
``SourceRegistry`` instance — invoked once in the app lifespan. Importing a source
module runs its ``@registry.register(...)`` decorator, so registration is keyed to
the instance passed here (the registry decorator binds to an INSTANCE, not a module
singleton).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..framework.registry import SourceRegistry


def register_builtin_sources(
    registry: SourceRegistry, disabled: Collection[str] | None = None
) -> None:
    """Register all built-in sources into ``registry`` (called in the lifespan).

    ``disabled`` is an optional collection of source keys to SKIP (matched
    case-insensitively). A disabled source is never registered, so it is absent
    from ``/caps``, never searched, and never added to the CF solver warm set
    (#198/#202 — keeps a source that cannot clear Cloudflare here from creating a
    startup warm storm). Default ``None`` registers EVERY built-in source, so all
    offline tests that expect the full set stay green.
    """
    from .atsumaru import AtsumaruSource
    from .comix import ComixSource
    from .kagane import KaganeSource
    from .mangaball import MangaBallSource
    from .mangadex import MangaDexSource
    from .mangadot import MangadotSource
    from .mangafire import MangaFireSource
    from .weebcentral import WeebCentralSource

    disabled_keys = {key.lower() for key in (disabled or ())}
    builtin: tuple[tuple[str, type], ...] = (
        ("mangadex", MangaDexSource),
        ("comix", ComixSource),
        ("mangaball", MangaBallSource),
        ("mangadot", MangadotSource),
        ("atsumaru", AtsumaruSource),
        ("weebcentral", WeebCentralSource),
        ("kagane", KaganeSource),
        ("mangafire", MangaFireSource),
    )
    for key, cls in builtin:
        if key in disabled_keys:
            continue
        registry.register(key)(cls)
