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

import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..models.caps import AntibotLevel, SourceCap

if TYPE_CHECKING:
    from decimal import Decimal

    from ..models.search import Release, SearchRequest
    from .context import SourceContext
    from .health import SourceHealth


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
    # D-39: the encrypted-response decrypt scheme dispatched via framework/decrypt.py.
    # None = non-encrypted; a concrete source registers + names a scheme at module
    # load time (e.g. ``@register_scheme("<name>") + decrypt_scheme = "<name>"``).
    decrypt_scheme: str | None = None
    # Optional Cloudflare-clearance URL the framework solver navigates to so the
    # site issues a ``cf_clearance`` cookie bound to the bypassed session. Set on
    # any ``antibot="cloudflare*"`` source; ignored otherwise. The application
    # wiring (app.py lifespan) reads this off the first cloudflare-gated source
    # and passes it to ``CloudflareSolver`` so the framework solver itself need
    # not know any source-specific URL by name.
    cloudflare_challenge_url: str | None = None
    # D-01: the httpx session-prep style the framework resolves into a SessionPrep
    # provider (framework/session_prep.py). ``None`` = no bootstrap (MangaDex/Comix
    # byte-for-byte unchanged); ``"csrf-bootstrap"`` = GET an HTML page, harvest the
    # meta csrf-token + PHPSESSID, and inject X-CSRF-Token + a Cookie on every
    # /api/v1 POST (MangaBall). The app wiring (app.py lifespan) maps this name onto
    # a CsrfBootstrap instance, mirroring how cloudflare_challenge_url feeds the solver.
    session_prep: str | None = None
    supports_search: bool = True
    supports_recent: bool = True
    # D-30 / WR-02 per-source override: max concurrent download JOBS for THIS source.
    # ``None`` = use the global ``settings.max_concurrent_per_source`` default. A source
    # the rate-limit probe shows tolerates parallel chapter downloads sets this
    # explicitly (e.g. mangadot=3); the job manager clamps it to the global bound.
    # Sources with a parallelism constraint (e.g. comix CF-nav) leave it None.
    max_concurrent_jobs: int | None = None

    @staticmethod
    def chapter_matches(req: SearchRequest, chapter_number: Decimal | None) -> bool:
        """Shared whole-number / floor chapter-match predicate (locked 1+2+3+4).

        ONE filter consumed identically by all four sources — no source re-implements
        the floor logic. It is a pure predicate: never mutates, never raises.

        Gate OFF (pass-through, returns ``True`` for everything):
          * ``req.type != "chapter"`` — a ``type=manga`` search is untouched.
          * ``req.chapter is None`` — a chapterless ``type=chapter`` search is untouched.

        Gate ON (``req.type == "chapter"`` and ``req.chapter is not None``):
          * ``chapter_number is None`` → ``False`` (locked 3: a release with no parsed
            chapter cannot match a requested chapter).
          * else keep iff ``floor(chapter_number) == floor(req.chapter)`` (locked 1+2:
            the whole-number / floor family — ``179`` and ``179.5`` both floor to ``179``,
            so each returns the full ``179.x`` family; it is a floor match, not exact).

        ``req.chapter`` is ``float`` and ``chapter_number`` is ``Decimal``; ``math.floor``
        returns ``int`` for both, so the comparison is ``int == int`` (mypy-strict clean,
        no cast / no ``# type: ignore``).
        """
        if req.type != "chapter" or req.chapter is None:
            return True
        if chapter_number is None:
            return False
        return math.floor(chapter_number) == math.floor(req.chapter)

    @classmethod
    def source_cap(cls, health: SourceHealth | None = None) -> SourceCap:
        """Build the ``SourceCap`` the registry advertises (CAPS-02).

        ``enabled`` is dynamic (D-38): it reflects the live per-source breaker when
        a ``health`` is supplied, and defaults to ``True`` when none is (MangaDex /
        no-anti-bot sources keep their unchanged always-enabled behavior).
        """
        return SourceCap(
            key=cls.key,
            name=cls.name,
            enabled=(health.is_enabled if health else True),
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

    @abstractmethod
    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered page URLs, INTERNALLY (PKG-01/R6).

        The manifest (page URLs / any fresh at-home token) is resolved at grab time and
        NEVER returned to a caller — only the gateway's own engine consumes it. Declared
        abstractly so the job engine dispatches source-agnostically (SRC-01).
        """
        ...

    @abstractmethod
    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes through the shared session (PKG-02)."""
        ...
