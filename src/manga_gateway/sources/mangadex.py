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
from typing import TYPE_CHECKING, Any, NamedTuple

from ..framework.base import Source
from ..framework.enum_cache import Enumeration
from ..framework.errors import SourceError
from ..framework.relevance import _normalize, prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# Bound a title search's candidate manga. The count is mode-invariant: interactive
# no longer widens the fan-out (#162). Exact-match queries already collapse to ~1
# candidate after the #126 prune regardless of the ceiling, so 5 is the single
# source of truth for both modes (it is still referenced by the prune/cap below).
_DEFAULT_MANGA_CANDIDATES = 5
# MangaDex page-size ceiling for the chapter feed.
_MAX_FEED_LIMIT = 100
# Defensive page-count cap for a single feed walk: 50 pages x 100 rows = 5000
# chapters per manga per walk — well above the largest real series; the cap exists
# so a pathological/looping feed can never spin unbounded.
_MAX_FEED_PAGES = 50


class _MangaCandidate(NamedTuple):
    """One title-search candidate carried through the #126/#139 prune.

    ``titles`` is ``[main_title, *alt_titles]`` (None/empty entries already
    dropped) — the exact shape ``prune_candidates(keys=...)`` scores over.
    """

    manga_id: str
    titles: list[str]


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
            # ID-first path: a single, already-resolved candidate. No prune —
            # one candidate in, one out (mirrors comix's _fetch_manga_by_id).
            manga = await self._fetch_manga_by_id(mangadex_id, ctx)
            manga_ids = [manga["id"]] if manga else []
        else:
            count = _DEFAULT_MANGA_CANDIDATES

            async def _resolve_fn() -> list[_MangaCandidate]:
                found = await self._search_manga_titles(req.query or "", count, ctx)
                # Prune obviously-irrelevant candidates BEFORE the per-candidate
                # chapter-feed fan-out (#126): an exact-match query enumerates only
                # the one correct series; ambiguous queries fan out to ``count`` (the
                # prune falls back to the historic ``candidates[:count]``). ``count``
                # is mode-invariant at 5 (#162) — interactive no longer widens it.
                # Each candidate is scored over its MAIN *or* any ALTERNATE/native
                # title (#139) — a query matching only the native name still prunes
                # to it.
                return prune_candidates(
                    found,
                    req.query or "",
                    keys=lambda c: c.titles,
                    cap=count,
                )

            # Layer 1 (D-01): cache the title→candidate-id resolution so a repeat
            # chapter search on the same (query, languages) skips the /manga?title=
            # call entirely (genuinely zero upstream calls on a HIT). The ID-first
            # branch above caches NEITHER layer (D-02 — already one resolved
            # candidate). The key normalizes the query the SAME way the relevance
            # scorer does so punctuation/case variants collapse onto one entry.
            # WR-01: the search mode no longer affects candidate width — ``count`` is
            # mode-invariant at 5 (#162), so the resolved candidate set is identical
            # whether or not ``req.interactive`` is set. The resolve key is therefore
            # mode-agnostic ``(source_key, normalized_query, languages)`` with NO
            # candidate-count discriminator, so a mode flip (interactive↔non-
            # interactive) for a warmed query is a HIT, not a deliberate MISS — there
            # is no width difference to reconcile, so no wrong-width list can be served.
            resolve_key = ctx.cached_resolve_key(_normalize(req.query or ""), languages)
            candidates: list[_MangaCandidate] = await ctx.cached_resolve(
                resolve_key, _resolve_fn
            )
            # 260605-e9a deliverable 5: how many manga we deep-enumerate (HIT too).
            ctx.candidates_enumerated = len(candidates)
            manga_ids = [c.manga_id for c in candidates]

        releases: list[Release] = []
        for manga_id in manga_ids:
            # Layer 2 (CACHE-02/03): cache the COMPLETE, paginate-all chapter feed per
            # (manga_id, languages) — keyed on the series + language ONLY, NEVER the
            # per-request walk window. This mirrors comix/mangaball (IN-02): a repeat
            # search for the SAME series but a DIFFERENT chapter/type/offset is a cache
            # HIT served from the one cached feed (re-filtered post-cache), not a fresh
            # ``/chapter`` walk. The earlier ``(stop_floor, offset)`` window key made
            # every differently-targeted repeat a MISS, so a series with zero matching
            # results re-hit ``/chapter`` on every Mangarr chapter search while comix &
            # mangaball served the cached feed (debug ``mangadex-search-not-cached``).
            # The walk is ALWAYS paginate-all, so the cached Enumeration is complete
            # (``exhausted=True``) and answers any chapter/offset query. A HIT costs no
            # upstream call and no rate-limit token — the limiter lives inside
            # ``_fetch_chapter_feed`` (via ``get_json``), below the cache check.
            enum_key = ctx.cached_enumerate_key(manga_id, languages)

            async def _feed_fn(_mid: str = manga_id) -> Enumeration:
                chapters = await self._walk_chapter_feed(_mid, languages, ctx)
                return self._build_enumeration(chapters)

            enum = await ctx.cached_enumerate(enum_key, _feed_fn)
            # CACHE-02: the chapter_matches floor filter + the req.offset window are
            # applied AFTER the cache (at the enumeration boundary), exactly like comix
            # — never baked into the cache key. Under type=chapter+chapter this keeps
            # only the requested whole-number/floor family (gate-off type=manga =
            # pass-through, #144); a floor-family member on a later feed page is never
            # missed because the WHOLE feed is cached. req.limit truncation stays at the
            # route (search.py), so the source still must NOT pre-truncate to req.limit.
            matched = [
                rel
                for chapter in enum.items
                if (rel := self._to_release(manga_id, chapter, ctx)) is not None
                and self.chapter_matches(req, rel.chapter_number)
            ]
            releases.extend(matched[req.offset :] if req.offset else matched)
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

    # ───────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a FRESH at-home manifest into ordered page URLs (PKG-01/R6).

        Calls ``GET {base_url}/at-home/server/{chapter_id}`` (shares the 300/min
        api.mangadex.org limiter via ``get_json``), then constructs
        ``{baseUrl}/data/{hash}/{filename}`` for each entry of ``chapter.data``: the
        full-quality array, already in reading order (Pitfall 5). The ``baseUrl`` is
        read fresh per call and NEVER stored (~15-min TTL, D-17/D-32). The ``/report``
        endpoint is intentionally NOT called in v1 (D-32/D-32a).
        """
        data = await ctx.get_json(f"{self.base_url}/at-home/server/{chapter_id}")
        base_url = data.get("baseUrl")  # FRESH ~15-min TTL: NEVER store (D-17/D-32)
        chapter = data.get("chapter")
        chapter_hash = chapter.get("hash") if isinstance(chapter, dict) else None
        filenames = chapter.get("data") if isinstance(chapter, dict) else None
        # Guard a malformed at-home response (WR-06): a typed SourceError surfaces as a
        # contract warning instead of a raw KeyError/TypeError leaking from the source.
        # Every filename must be a non-empty string, else a bad entry yields an invalid
        # page URL that only fails much later during image fetch (CR).
        if (
            not base_url
            or not chapter_hash
            or not isinstance(filenames, list)
            or not all(isinstance(name, str) and name for name in filenames)
        ):
            raise SourceError("source_unavailable", "malformed at-home manifest")
        # full-quality, in reading order (not dataSaver)
        return [f"{base_url}/data/{chapter_hash}/{name}" for name in filenames]

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes via the shared session (PKG-02).

        Delegates to ``ctx.get_bytes``: bounded by the per-job semaphore (Plan 03),
        NOT the per-source limiter (image host is not API-rate-limited; D-31).
        """
        return await ctx.get_bytes(url)

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
    ) -> list[_MangaCandidate]:
        """Title-fallback search → candidates carrying ``[main, *alt_titles]``.

        Each ``/manga`` search item's ``attributes`` already carries ``title``
        and ``altTitles``; we extract them here (#139) so the #126 prune can score
        every candidate over its native/alt names, not just the main title. Items
        without a usable ``id`` are skipped (unchanged from the old ID-only path).
        """
        data = await ctx.get_json(
            f"{self.base_url}/manga",
            title=query,
            limit=limit,
            **{"includes[]": ["cover_art"]},
        )
        candidates: list[_MangaCandidate] = []
        for m in data.get("data", []):
            if not isinstance(m, dict):
                continue
            manga_id = m.get("id")
            if not manga_id:
                continue
            candidates.append(
                _MangaCandidate(str(manga_id), self._search_item_titles(m))
            )
        return candidates

    @classmethod
    def _search_item_titles(cls, manga: dict[str, Any]) -> list[str]:
        """``[main_title, *alt_titles]`` from a ``/manga`` search item (#139).

        Be defensive: ``attributes`` may be missing, ``title`` is a localized
        dict, ``altTitles`` is a list of single-key localized dicts — malformed
        / empty entries are skipped (behavior-neutral, default ``[]``).
        """
        attrs = manga.get("attributes")
        if not isinstance(attrs, dict):
            return []
        titles: list[str] = []
        main = cls._pick_localized_title(attrs.get("title"))
        if main:
            titles.append(main)
        for alt in attrs.get("altTitles") or []:
            value = cls._pick_localized_title(alt)
            if value:
                titles.append(value)
        return titles

    @staticmethod
    def _pick_localized_title(localized: Any) -> str | None:
        """First non-empty value of a MangaDex localized-title dict, else None."""
        if not isinstance(localized, dict):
            return None
        for value in localized.values():
            if value:
                return str(value)
        return None

    async def _walk_chapter_feed(
        self,
        manga_id: str,
        languages: list[str],
        ctx: SourceContext,
    ) -> list[dict[str, Any]]:
        """PAGINATE-ALL walk of the chapter-ordered feed; return the collected rows.

        The feed is ordered ``order[chapter]=asc`` and walked from offset 0 until it
        is exhausted (a short page, or the upstream ``total`` is reached), hard-bounded
        by ``_MAX_FEED_PAGES`` so a pathological/looping feed can never spin unbounded.
        The walk returns the COMPLETE feed — no per-request early-stop or offset window
        — so the cached enumeration can answer any chapter/offset query; the
        ``chapter_matches`` floor filter + the offset window run in ``search`` AFTER the
        cache (CACHE-02), exactly like comix/mangaball. The walker does NOT filter.
        """
        collected: list[dict[str, Any]] = []
        offset = 0
        for _ in range(_MAX_FEED_PAGES):
            page, total = await self._fetch_chapter_feed(
                manga_id, languages, _MAX_FEED_LIMIT, offset, ctx
            )
            collected.extend(page)
            # Exhaustion: a short page is the last page.
            if len(page) < _MAX_FEED_LIMIT:
                break
            offset += _MAX_FEED_LIMIT
            # Second exhaustion bound when the upstream total is known.
            if total is not None and offset >= total:
                break
        return collected

    async def _fetch_chapter_feed(
        self,
        manga_id: str,
        languages: list[str],
        limit: int,
        offset: int,
        ctx: SourceContext,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Fetch ONE chapter-ordered feed page + the upstream page total.

        Returns ``(page, total)`` where ``page`` is the dict rows of this
        ``limit``/``offset`` slice and ``total`` is the upstream ``data.total``
        coerced to ``int | None`` (``None`` when absent / non-int — some fake test
        contexts omit it). The walker uses ``total`` as a second exhaustion bound.
        """
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
        # Keep only well-shaped rows: a dict whose ``attributes`` is absent or a
        # dict. A malformed row like ``{"attributes": []}`` would otherwise pass
        # the top-level dict check and then crash on ``.get(...)`` in
        # ``_build_enumeration`` / ``_to_release`` (WR-06 defensive parsing).
        page = [
            c
            for c in chapters
            if isinstance(c, dict)
            and (c.get("attributes") is None or isinstance(c.get("attributes"), dict))
        ]
        raw_total = data.get("total")
        total = raw_total if isinstance(raw_total, int) else None
        return page, total

    def _build_enumeration(self, chapters: list[dict[str, Any]]) -> Enumeration:
        """Wrap the FULLY-WALKED chapter feed in a COMPLETE :class:`Enumeration`.

        ``_walk_chapter_feed`` always paginate-all walks the whole feed, so the cached
        enumeration is always ``exhausted=True`` — it covers any chapter/offset query,
        which is why the Layer-2 cache can key on ``(manga_id, languages)`` ALONE and
        re-filter post-cache (no per-request walk window). ``chapter_numbers`` carries
        every parsed ``Decimal`` chapter (rows with an unparseable/absent chapter are
        skipped) so ``Enumeration.covers_floor`` stays well-defined for any caller.
        """
        chapter_numbers = tuple(
            d
            for c in chapters
            if isinstance((attrs := c.get("attributes")), dict)
            and (d := self._parse_decimal(attrs.get("chapter"))) is not None
        )
        return Enumeration(
            items=chapters,
            chapter_numbers=chapter_numbers,
            exhausted=True,
            requested_limit=len(chapters),
        )

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self, manga_id: str, chapter: dict[str, Any], ctx: SourceContext
    ) -> Release | None:
        # ``_fetch_chapter_feed`` admits malformed ``{"attributes": None}`` rows; such
        # a row carries no chapter data, so DROP it rather than mint a degenerate
        # handle with ``chapter_number=None`` (CodeRabbit). A ``.get(..., {})`` default
        # would not catch this — the key is present with a ``None`` value.
        attrs = chapter.get("attributes")
        if not isinstance(attrs, dict):
            return None
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
