"""MangaDex source — the first declarative source (SRC-05).

Subclasses :class:`~manga_gateway.framework.base.Source`, declaring D-13 metadata as
class attributes and overriding only the ``search``/``recent`` hooks. ALL networking,
rate-limiting, retry, and session sharing live in the injected ``ctx`` — this module
is just MangaDex param shaping + response parsing (built for 50+ sources).

Search is the two-step Torznab-faithful flow (D-22): ``ids.mangadexId`` present →
direct ``GET /manga/{id}``; else ``GET /manga?title=`` then enumerate the top
candidate's chapter feed with reference expansion (``includes[]``) to avoid N+1
(Pitfall 2). Each chapter UPLOAD is one ``Release`` with a distinct ``guid`` (D-21,
no pre-merge). Chapter numbers are parsed via ``Decimal`` (Pitfall 1, SRCH-06).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from ..framework.base import Source
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# Bound a title search's candidate manga; interactive widens it (D-22).
_DEFAULT_MANGA_CANDIDATES = 5
_INTERACTIVE_MANGA_CANDIDATES = 15
# MangaDex page-size ceiling for the chapter feed.
_MAX_FEED_LIMIT = 100


class MangaDexSource(Source):
    """MangaDex (api.mangadex.org v5) — antibot none, NoopSolver (SRC-05)."""

    key = "mangadex"
    name = "MangaDex"
    base_url = "https://api.mangadex.org"
    id_types = ["mangadexId"]
    # Seeded common languages; the live SourceCap could be widened from
    # availableTranslatedLanguages. BCP-47-ish, passed through (RESEARCH).
    languages = ["en", "es", "es-la", "fr", "de", "pt-br", "ru", "ja", "ko", "zh"]
    # Documented global ceiling 5 req/s -> 300/min (RESEARCH A4); conservative.
    rate_limit_per_minute = 300
    antibot = "none"

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """ID-first or title-fallback resolution → chapter-feed enumeration (D-22)."""
        languages = req.languages or ["en"]
        mangadex_id = self._extract_id(req)

        if mangadex_id is not None:
            manga = await self._fetch_manga_by_id(mangadex_id, ctx)
            manga_ids = [manga["id"]] if manga else []
        else:
            count = (
                _INTERACTIVE_MANGA_CANDIDATES
                if req.interactive
                else _DEFAULT_MANGA_CANDIDATES
            )
            manga_ids = await self._search_manga_titles(req.query or "", count, ctx)

        releases: list[Release] = []
        feed_limit = min(req.limit or _MAX_FEED_LIMIT, _MAX_FEED_LIMIT)
        for manga_id in manga_ids:
            chapters = await self._fetch_chapter_feed(
                manga_id, languages, feed_limit, req.offset, ctx
            )
            for chapter in chapters:
                rel = self._to_release(manga_id, chapter, ctx)
                if rel is not None:
                    releases.append(rel)
        return releases

    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        """Newest-first recent chapters across all manga (RCNT-01/02).

        Phase 2 scaffold: the full ``/recent`` route lands in a later plan; the hook
        exists so the contract is honored and the framework stays source-agnostic.
        """
        params: dict[str, Any] = {
            "order[readableAt]": "desc",
            "includes[]": ["manga", "scanlation_group"],
            "limit": min(limit or _MAX_FEED_LIMIT, _MAX_FEED_LIMIT),
        }
        if languages:
            params["translatedLanguage[]"] = languages
        if since:
            params["publishAtSince"] = since
        data = await ctx.get_json(f"{self.base_url}/chapter", **params)
        releases: list[Release] = []
        for chapter in data.get("data", []):
            manga_id = self._relationship_id(chapter, "manga") or ""
            rel = self._to_release(manga_id, chapter, ctx)
            if rel is not None:
                releases.append(rel)
        return releases

    # ─────────────────────────── MangaDex fetch helpers ──────────────────────────

    @staticmethod
    def _extract_id(req: SearchRequest) -> str | None:
        if not req.ids:
            return None
        value = req.ids.get("mangadexId")
        return str(value) if value else None

    async def _fetch_manga_by_id(
        self, manga_id: str, ctx: SourceContext
    ) -> dict[str, Any] | None:
        data = await ctx.get_json(
            f"{self.base_url}/manga/{manga_id}", **{"includes[]": ["cover_art"]}
        )
        manga = data.get("data")
        return manga if isinstance(manga, dict) else None

    async def _search_manga_titles(
        self, query: str, limit: int, ctx: SourceContext
    ) -> list[str]:
        data = await ctx.get_json(
            f"{self.base_url}/manga",
            title=query,
            limit=limit,
            **{"includes[]": ["cover_art"]},
        )
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict)]

    async def _fetch_chapter_feed(
        self,
        manga_id: str,
        languages: list[str],
        limit: int,
        offset: int,
        ctx: SourceContext,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "manga": manga_id,
            "translatedLanguage[]": languages,
            "includes[]": ["manga", "scanlation_group"],
            "order[chapter]": "asc",
            "limit": limit,
            "offset": offset,
        }
        data = await ctx.get_json(f"{self.base_url}/chapter", **params)
        chapters = data.get("data", [])
        return [c for c in chapters if isinstance(c, dict)]

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self, manga_id: str, chapter: dict[str, Any], ctx: SourceContext
    ) -> Release | None:
        attrs = chapter.get("attributes", {})
        # Skip off-site chapters this phase (Open Q 2) — Phase 3 resolution would
        # fail; note carried forward for Phase 3.
        if attrs.get("externalUrl"):
            return None

        chapter_id = chapter.get("id")
        if not chapter_id:
            return None

        raw_chapter = attrs.get("chapter")
        chapter_number = self._parse_decimal(raw_chapter)
        volume = self._parse_int(attrs.get("volume"))
        language = attrs.get("translatedLanguage") or "en"
        page_count = self._parse_int(attrs.get("pages"))
        publish_date = attrs.get("readableAt") or attrs.get("publishAt") or ""

        manga_title = self._manga_title(chapter, language)
        group = self._relationship_name(chapter, "scanlation_group")

        title = self._build_title(
            manga_title or "Unknown",
            raw_chapter,
            volume=volume,
            language=language,
            group=group,
        )
        # D-21: chapter UUID guarantees per-upload uniqueness across groups.
        guid = f"mangadex:{manga_id}:ch-{raw_chapter or '?'}:{language}:{chapter_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=chapter_id,
                language=language,
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=volume,
                scanlation_group=group,
                page_count=page_count,
            )
        )

        return Release(
            guid=guid,
            title=title,
            source_key=self.key,
            download_handle=handle,
            publish_date=publish_date,
            manga_title=manga_title,
            chapter_number=chapter_number,
            volume=volume,
            language=language,
            scanlation_group=group,
            page_count=page_count,
            ids={"mangadexChapterId": chapter_id, "mangadexMangaId": manga_id},
        )

    @staticmethod
    def _build_title(
        manga_title: str,
        chapter: str | None,
        *,
        volume: int | None,
        language: str | None,
        group: str | None,
    ) -> str:
        """D-18 title template — MUST stay MangaParser-parseable (REL-02)."""
        parts = [manga_title, "-"]
        if volume is not None:
            parts.append(f"Vol. {volume}")
        parts.append(f"Chapter {chapter}" if chapter else "Chapter ?")
        if language:
            parts.append(f"({language})")
        if group:
            parts.append(f"[{group}]")
        return " ".join(parts)

    def _manga_title(self, chapter: dict[str, Any], language: str) -> str | None:
        """Pick a manga title: requested-lang → en → first → altTitles (RESEARCH)."""
        manga_attrs = self._relationship_attrs(chapter, "manga")
        if manga_attrs is None:
            return None
        titles = manga_attrs.get("title") or {}
        if isinstance(titles, dict):
            for key in (language, "en"):
                if titles.get(key):
                    return str(titles[key])
            if titles:
                return str(next(iter(titles.values())))
        for alt in manga_attrs.get("altTitles") or []:
            if isinstance(alt, dict) and alt.get("en"):
                return str(alt["en"])
        return None

    @staticmethod
    def _relationship_attrs(
        chapter: dict[str, Any], rel_type: str
    ) -> dict[str, Any] | None:
        for rel in chapter.get("relationships", []):
            if isinstance(rel, dict) and rel.get("type") == rel_type:
                attrs = rel.get("attributes")
                return attrs if isinstance(attrs, dict) else None
        return None

    @classmethod
    def _relationship_name(cls, chapter: dict[str, Any], rel_type: str) -> str | None:
        attrs = cls._relationship_attrs(chapter, rel_type)
        if attrs is None:
            return None
        name = attrs.get("name")
        return str(name) if name else None

    @staticmethod
    def _relationship_id(chapter: dict[str, Any], rel_type: str) -> str | None:
        for rel in chapter.get("relationships", []):
            if isinstance(rel, dict) and rel.get("type") == rel_type:
                rid = rel.get("id")
                return str(rid) if rid else None
        return None

    @staticmethod
    def _parse_decimal(raw: Any) -> Decimal | None:
        """Parse the MangaDex chapter STRING to Decimal (Pitfall 1, SRCH-06)."""
        if raw is None or raw == "":
            return None
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _parse_int(raw: Any) -> int | None:
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
