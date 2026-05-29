"""Declarative ``Source`` base class (SRC-01/SRC-02, D-13).

Hybrid: a source declares static metadata as class attributes and overrides a few
small async hooks (``search`` / ``recent``). The framework owns ALL cross-cutting
infrastructure (networking, rate-limit, pagination, retry/backoff, session, solver)
via the injected :class:`~manga_gateway.framework.context.SourceContext` — so a
future source is ~30 lines with no networking glue (built for 50+ sources).

``source_cap()`` is the classmethod the registry calls (registry.caps()) to build
the per-source ``SourceCap`` advertised by ``GET /caps`` (CAPS-02).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..models.caps import AntibotLevel, SourceCap

if TYPE_CHECKING:
    from ..models.search import Release, SearchRequest
    from .context import SourceContext


class Source(ABC):
    """Abstract base every source subclasses (SRC-01).

    Declarative metadata (D-13) — overridden as class attributes per source:
    """

    key: str
    name: str
    base_url: str
    id_types: list[str]
    languages: list[str]
    rate_limit_per_minute: int
    antibot: AntibotLevel = "none"
    supports_search: bool = True
    supports_recent: bool = True

    @classmethod
    def source_cap(cls) -> SourceCap:
        """Build the ``SourceCap`` the registry advertises (CAPS-02)."""
        return SourceCap(
            key=cls.key,
            name=cls.name,
            enabled=True,
            supports_search=cls.supports_search,
            supports_recent=cls.supports_recent,
            id_types=cls.id_types,
            languages=cls.languages,
            rate_limit_per_minute=cls.rate_limit_per_minute,
            antibot=cls.antibot,
        )

    @abstractmethod
    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Resolve a search request into normalized releases (SRCH-01..07)."""
        ...

    @abstractmethod
    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        """Return newest-first recent releases (RCNT-01/02)."""
        ...
