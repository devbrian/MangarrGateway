"""Atsumaru source — clean-JSON ``none``-prep + a Typesense title search (SRC-01).

Atsumaru (``https://atsu.moe``) is a **MangaDex-class** source: a public JSON
backend with no anti-bot challenge on the read/search endpoints, no response
encryption, and plain same-origin ``.webp`` page images. It adds **zero
networking glue** — every outbound call is ``ctx.get_json`` / ``ctx.get_bytes``
(SRC-01/SRC-02).

Structurally it mirrors MangaBall's **two-call title→chapter fan-out** (the title
search is title-only, so each candidate is deep-enumerated for its chapters), but
it drops MangaBall's ``csrf-bootstrap`` prep entirely — the endpoints answer cold,
with any User-Agent, no cookie/CSRF/auth (live recon 2026-06-08). So:
``antibot = "none"``, ``decrypt_scheme = None``, ``session_prep = None``.

ENDPOINT SHAPES (live-recon-pinned, 2026-06-08):

* base: ``https://atsu.moe``
* search (title): ``GET /collections/manga/documents/search`` — a Typesense proxy
  (the search-only API key is injected server-side, so NO credential is needed).
  Params ``q`` + ``query_by=title,englishTitle,otherNames,authors`` (+ weights /
  typos / ``include_fields`` / ``filter_by=hidden:!=true``). Returns
  ``{found, hits:[{document:{id,title,englishTitle,…}}]}`` — TITLE-ONLY (a hit is a
  series, not a chapter), so ``search`` deep-enumerates each candidate's chapters.
* chapter enumeration: ``GET /api/manga/allChapters?mangaId=`` →
  ``{chapters:[{id,scanlationMangaId,title,number,createdAt(epoch-ms),index,
  pageCount,progress}]}``. The COMPLETE feed (verified 1184/1184 for One Piece),
  newest-first, every row carrying a stable chapter ``id`` + a ``createdAt`` date —
  so this is the enumeration source for BOTH ``search`` and ``recent`` (DIRECT mint,
  no Comix-style ``:DEFERRED`` late-bind: the id is always present).
* recent: ``GET /api/infinite/recentlyUpdated?page=&types=`` →
  ``{items:[{id,title,type,…}]}`` — a TITLE-ONLY newest-updated feed (no embedded
  chapter), so ``recent`` fans out a bounded ``allChapters`` per title and mints the
  newest chapter.
* manifest: ``GET /api/read/chapter?mangaId=&chapterId=`` →
  ``{readChapter:{id,title,scanlationMangaId,pages:[{image:"/static/pages/{scan}/
  {chapter}/{N}.webp",number,…}]}}``. The page-image paths are read VERBATIM from the
  ``pages[].image`` field (array order = page order), joined onto ``base_url`` and
  SSRF-allowlisted — NEVER reconstructed (RECON §4 / CLAUDE.md SSRF). ``read/chapter``
  Zod-requires a non-empty ``mangaId`` param (the VALUE is not re-validated, but we
  always pass the real one), so the resolve unit is the COMPOSITE ``{mangaId}:
  {chapterId}`` — see :meth:`fetch_manifest`.
* image: plain httpx ``GET`` of each absolute ``https://atsu.moe/static/pages/...``
  ``.webp`` (no ``Referer`` needed — a bare GET returns 200, live-verified).

guid (D-08): ``atsumaru:{mangaId}:ch-{number}:{chapterId}``. Atsumaru is a
single-language (English) aggregator with one upload per chapter, so — unlike
MangaBall — no language/group segment is needed for uniqueness; the chapter ``id``
already guarantees it.

Anti-bot caveat (D-12): the ``antibot = "none"`` classification was made from a
residential IP. A datacenter IP MAY trip a managed Cloudflare challenge (atsu.moe
fronts Cloudflare passively); the escalation is the existing one-attribute flip
(``antibot = "cloudflare"`` + ``cloudflare_challenge_url``) — no glue change, since
the framework already owns the clearance path.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from ..framework.base import Source
from ..framework.enum_cache import Enumeration
from ..framework.errors import SourceError
from ..framework.relevance import _normalize, prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# Typesense search params (live frontend defaults, 2026-06-08). The keyword rides
# ``q``; the rest mirror the site's own XHR so server-side relevance matches what a
# user sees. ``filter_by=hidden:!=true`` drops unlisted series; ``include_fields``
# is the minimal projection ``search`` needs (id + the names the #126 prune scores).
_SEARCH_QUERY_BY = "title,englishTitle,otherNames,authors"
_SEARCH_QUERY_BY_WEIGHTS = "4,3,2,1"
_SEARCH_NUM_TYPOS = "1,1,1,1"
_SEARCH_INCLUDE_FIELDS = "id,title,englishTitle,otherNames"
_SEARCH_FILTER_BY = "hidden:!=true"
_SEARCH_PER_PAGE = 12

# search() deep-enumerates at most this many title candidates (mirrors MangaBall's
# GAP-1 lock). Each candidate is a full allChapters fan-out, so the count is held
# fixed regardless of ``req.interactive``.
_DEFAULT_TITLE_CANDIDATES = 5
# Bounds the per-candidate allChapters fan-out in search() (mirrors MangaBall's
# ``_CHAPTERS_FANOUT_CONCURRENCY``): at most this many chapter-listing fetches run
# concurrently, collapsing wall-clock while staying under the per-source budget.
_CHAPTERS_FANOUT_CONCURRENCY = 6
# recent() fans out one allChapters call per recently-updated title; bound the title
# count so a single recent poll can never balloon into dozens of full-feed fetches.
_RECENT_TITLE_CAP = 20
# The content types the recent feed advertises (atsu.moe spells it "Manwha").
_RECENT_TYPES = "Manga,Manwha,Manhua"

# SSRF allowlist for the extracted page-image URLs (T-07-07/T-07-09, CLAUDE.md).
# Unlike MangaBall the CDN host is FIXED (page images are same-origin
# ``atsu.moe/static/pages/...``), so the host is pinned. The page path namespace is
# ``/static/`` and the extension is an image one; both are stable invariants — the
# per-page filename (``{N}.webp``) is NOT over-pinned beyond the extension.
_ATSUMARU_IMAGE_HOSTS = frozenset({"atsu.moe", "www.atsu.moe"})
_ATSUMARU_IMG_PATH_RE = re.compile(
    r"^/static/[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)


def _ms_to_iso(raw: Any) -> str | None:
    """Convert an Atsumaru ``createdAt`` epoch-millis value → RFC3339 ``date-time``.

    The contract's ``Release.publishDate`` is ``format: date-time`` (RFC3339, ``T``
    separator). Atsumaru's ``allChapters`` rows carry ``createdAt`` as integer
    epoch-milliseconds (e.g. ``1780084797472``). Returns the aware-UTC ISO string,
    or ``None`` for a missing/unparseable value so the caller can fall back.
    """
    if raw is None or raw == "":
        return None
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, UTC).isoformat()


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` is an Atsumaru same-origin page image (SSRF allowlist).

    Belt-and-suspenders defence on every extracted manifest URL before the framework
    fetches it (T-07-07/T-07-09). The page-image host is fixed (``atsu.moe``) and the
    path namespace is ``/static/`` with an image extension; we reject non-HTTPS
    schemes, off-host URLs, path traversal, and any path outside that namespace. We
    validate the ``posixpath.normpath``-resolved path (httpx normalizes ``..`` before
    issuing the request — CR-01) and reject any literal ``..`` segment outright.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return (
        parsed.scheme == "https"
        and host in _ATSUMARU_IMAGE_HOSTS
        and bool(_ATSUMARU_IMG_PATH_RE.match(norm_path))
    )


class AtsumaruSource(Source):
    """Atsumaru (atsu.moe) — antibot none, no session prep, Typesense title search.

    A MangaDex-class clean-JSON source whose only structural wrinkle is the
    title-only search (a Typesense proxy returns series, not chapters), so ``search``
    deep-enumerates each candidate's ``allChapters`` feed — the MangaBall two-call
    fan-out shape, minus the CSRF bootstrap. Zero networking glue (module docstring).
    """

    key = "atsumaru"
    name = "Atsumaru"
    base_url = "https://atsu.moe"
    # Title-search only — Atsumaru exposes no external metadata-id namespace we mint
    # against (its own ``mangabaka_id`` link is not a Mangarr search id; SRCH-07).
    id_types: list[str] = []
    # Single-language (English) aggregator — no per-chapter language metadata exists.
    languages = ["en"]
    # Conservative start (recon Open Q): the read/search endpoints answered cold with
    # no throttling observed on a residential IP, but the real Cloudflare-fronted
    # ceiling is unprobed. 120/min is a safe v1 floor; live-tune with the rate-limit
    # probe harness (scripts/probe_rate_limits.py) before raising it.
    rate_limit_per_minute = 120
    antibot = "none"
    decrypt_scheme = None
    session_prep = None
    supports_search = True
    supports_recent = True

    # ─────────────────────────────── search ──────────────────────────────────

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-chapter Releases (SRCH-01..07, D-08).

        Two-call live flow (mirrors MangaBall's GAP-1 lock): the Typesense search is
        TITLE-ONLY, so ``search`` deep-enumerates the first
        ``_DEFAULT_TITLE_CANDIDATES`` (relevance-pruned) title candidates via a
        per-candidate ``allChapters`` GET. For each candidate the complete chapter
        feed is filtered by :meth:`chapter_matches`, ordered NEWEST-FIRST, sliced to
        ``req.limit``, and ONLY THEN minted (GAP-2 mint-after-slice — a long title
        like One Piece carries 1000+ chapters, and minting per row would evict the
        returned releases' own handles from the store). ZERO networking glue — both
        calls are ``ctx.get_json`` (SRC-01/02).
        """
        if not self._language_wanted(req.languages):
            return []

        async def _resolve_fn() -> list[dict[str, Any]]:
            body = await ctx.get_json(
                f"{self.base_url}/collections/manga/documents/search",
                q=req.query or "",
                query_by=_SEARCH_QUERY_BY,
                query_by_weights=_SEARCH_QUERY_BY_WEIGHTS,
                num_typos=_SEARCH_NUM_TYPOS,
                include_fields=_SEARCH_INCLUDE_FIELDS,
                filter_by=_SEARCH_FILTER_BY,
                per_page=_SEARCH_PER_PAGE,
            )
            hits = body.get("hits")
            documents = [
                doc
                for hit in (hits if isinstance(hits, list) else [])
                if isinstance(hit, dict)
                and isinstance((doc := hit.get("document")), dict)
            ]
            # Prune obviously-irrelevant candidates BEFORE the per-candidate fan-out
            # (#126), scoring over the main OR any alternate/native name (#139). An
            # exact-match query enumerates only the one correct title; ambiguous
            # queries fall back to ``[:_DEFAULT_TITLE_CANDIDATES]``.
            return prune_candidates(
                documents,
                req.query or "",
                keys=lambda d: [
                    d.get("title"),
                    d.get("englishTitle"),
                    *_other_names(d),
                ],
                cap=_DEFAULT_TITLE_CANDIDATES,
            )

        # Layer 1 (D-01): cache the title→pruned-candidate resolution so a repeat
        # chapter search on the same (query, languages) skips the Typesense call.
        candidates: list[dict[str, Any]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(req.query or ""), req.languages or []),
            _resolve_fn,
        )
        ctx.candidates_enumerated = len(candidates)
        per_candidate_limit = req.limit or 50
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(manga_id: str, manga_title: str) -> list[Release]:
            # Layer 2 (CACHE-02/03): cache the UNFILTERED per-candidate allChapters
            # feed per (manga_id). The semaphore + the GET live INSIDE ``_enum_fn`` so
            # a cache HIT acquires neither the fan-out slot nor a rate-limit token.
            async def _enum_fn() -> Enumeration:
                async with sem:
                    listing = await ctx.get_json(
                        f"{self.base_url}/api/manga/allChapters", mangaId=manga_id
                    )
                chapters = listing.get("chapters")
                rows = chapters if isinstance(chapters, list) else []
                return Enumeration(
                    items=rows,
                    chapter_numbers=tuple(
                        d
                        for c in rows
                        if isinstance(c, dict)
                        and (d := self._parse_decimal(c.get("number"))) is not None
                    ),
                    exhausted=True,  # allChapters is the COMPLETE feed
                    requested_limit=per_candidate_limit,
                )

            # IN-02: the feed does not depend on ``languages`` (single-language site),
            # so key on ``[]`` — every request for one title shares the cached feed.
            enum = await ctx.cached_enumerate(
                ctx.cached_enumerate_key(manga_id, []), _enum_fn
            )
            return self._chapters_to_releases(
                enum.items, manga_id, manga_title, per_candidate_limit, ctx, req
            )

        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for doc in candidates:
            manga_id = doc.get("id")
            if not manga_id:
                continue
            manga_title = str(doc.get("title") or doc.get("englishTitle") or "Unknown")
            tasks.append(_fetch_candidate(str(manga_id), manga_title))

        # gather (not TaskGroup): re-raise the FIRST child SourceError UNCHANGED so
        # fanout.py classifies it as the source's own failure (mirror MangaBall).
        results = await asyncio.gather(*tasks)
        releases: list[Release] = []
        for chunk in results:
            releases.extend(chunk)
        return releases

    def _chapters_to_releases(
        self,
        chapters: list[Any],
        manga_id: str,
        manga_title: str,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one candidate's ``allChapters`` → per-chapter Releases (GAP-2).

        Filtered by :meth:`chapter_matches`, ordered NEWEST-FIRST by ``index`` (the
        reliable monotonic chapter ordinal), sliced to ``limit``, THEN minted — handle
        count per candidate is bounded by ``limit`` so the returned releases' handles
        always survive the store cap.
        """
        rows: list[tuple[int, dict[str, Any]]] = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            if not chapter.get("id"):
                continue  # no resolve unit
            number = self._parse_decimal(chapter.get("number"))
            if not self.chapter_matches(req, number):
                continue
            rows.append((self._parse_int(chapter.get("index")) or 0, chapter))
        rows.sort(key=lambda row: row[0], reverse=True)  # newest-first
        releases: list[Release] = []
        for _index, chapter in rows[:limit]:  # mint AFTER slice (GAP-2)
            rel = self._to_release(manga_id, manga_title, chapter, ctx)
            if rel is not None:
                releases.append(rel)
        return releases

    # ─────────────────────────────── recent ──────────────────────────────────

    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        """Newest-updated titles → DIRECT newest-chapter releases (RCNT-01/02).

        GETs ``/api/infinite/recentlyUpdated`` (TITLE-ONLY — no embedded chapter), then
        fans out a bounded ``allChapters`` per title (newest-first) and mints the
        newest chapter as a DIRECT release (the chapter id is always present, so no
        ``:DEFERRED`` late-bind). The route applies the authoritative newest-first sort
        + ``since`` cut; the source-side ``since`` is intentionally left to the route
        (IN-01). Zero networking glue — every call is ``ctx.get_json`` (SRC-02).
        """
        if not self._language_wanted(languages):
            return []
        body = await ctx.get_json(
            f"{self.base_url}/api/infinite/recentlyUpdated",
            page=1,
            types=_RECENT_TYPES,
        )
        items = body.get("items")
        titles = [
            it
            for it in (items if isinstance(items, list) else [])
            if isinstance(it, dict)
        ]

        # Bound the per-title fan-out: never more than the requested limit or the cap.
        bound = min(_RECENT_TITLE_CAP, limit or _RECENT_TITLE_CAP)
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _newest_release(manga_id: str, manga_title: str) -> Release | None:
            async with sem:
                listing = await ctx.get_json(
                    f"{self.base_url}/api/manga/allChapters", mangaId=manga_id
                )
            chapters = listing.get("chapters")
            rows = chapters if isinstance(chapters, list) else []
            newest = next(
                (c for c in rows if isinstance(c, dict) and c.get("id")), None
            )
            if newest is None:
                return None
            return self._to_release(manga_id, manga_title, newest, ctx)

        tasks: list[Coroutine[Any, Any, Release | None]] = []
        for title in titles[:bound]:
            manga_id = title.get("id")
            if not manga_id:
                continue
            manga_title = str(title.get("title") or "Unknown")
            tasks.append(_newest_release(str(manga_id), manga_title))

        results = await asyncio.gather(*tasks)
        return [rel for rel in results if rel is not None]

    # ─────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a COMPOSITE ``{mangaId}:{chapterId}`` → ordered page URLs (PKG-01).

        Both ``search`` and ``recent`` mint the composite ``{mangaId}:{chapterId}`` as
        the ``ResolutionRecord.chapter_id`` because ``read/chapter`` Zod-requires the
        ``mangaId`` param. GETs ``/api/read/chapter?mangaId=&chapterId=``, reads the
        ordered ``readChapter.pages[].image`` paths VERBATIM (array order = page
        order), joins each onto ``base_url`` and SSRF-allowlists it — the host/path are
        never reconstructed (RECON §4 / CLAUDE.md SSRF). The extracted count is guarded
        against the chapter's declared ``pages`` when known (``ctx.expected_pages``,
        IN-03, mirror ``mangadex.fetch_manifest``).
        """
        manga_id, _, real_chapter_id = chapter_id.partition(":")
        if not manga_id or not real_chapter_id:
            raise SourceError(
                "source_unavailable",
                f"malformed atsumaru chapter id {chapter_id!r} (want mangaId:chapter)",
            )
        body = await ctx.get_json(
            f"{self.base_url}/api/read/chapter",
            mangaId=manga_id,
            chapterId=real_chapter_id,
        )
        read_chapter = body.get("readChapter")
        pages = read_chapter.get("pages") if isinstance(read_chapter, dict) else None
        if not isinstance(pages, list) or not pages:
            raise SourceError(
                "source_unavailable",
                f"no pages in read/chapter for {real_chapter_id}",
            )
        urls: list[str] = []
        for page in pages:
            image = page.get("image") if isinstance(page, dict) else None
            if not isinstance(image, str) or not image:
                raise SourceError(
                    "source_unavailable",
                    f"read/chapter page missing image for {real_chapter_id}",
                )
            url = urljoin(self.base_url + "/", image.lstrip("/"))
            if not _is_allowed_image_url(url):
                # Never fetch a non-allowlisted (off-host / off-shape) URL (T-07-07).
                raise SourceError(
                    "source_unavailable",
                    f"read/chapter image URL failed the SSRF allowlist: {url!r}",
                )
            urls.append(url)
        if ctx.expected_pages is not None and len(urls) != ctx.expected_pages:
            raise SourceError(
                "source_unavailable",
                f"manifest integrity: extracted {len(urls)} images, "
                f"chapter declares {ctx.expected_pages} pages",
            )
        return urls

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes via the shared session (PKG-02).

        A one-line delegate (mirror ``mangadex``/``mangaball``): bounded by the per-job
        semaphore, NOT the per-source API limiter. Plain ``.webp`` — no decrypt, and no
        ``Referer`` (a bare GET returns 200, live-verified 2026-06-08).
        """
        return await ctx.get_bytes(url)

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        manga_id: str,
        manga_title: str,
        chapter: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single ``allChapters`` row (D-08)."""
        chapter_id = chapter.get("id")
        if not chapter_id:
            return None
        chapter_id = str(chapter_id)
        chapter_number = self._parse_decimal(chapter.get("number"))
        page_count = self._parse_int(chapter.get("pageCount"))
        publish_date = (
            _ms_to_iso(chapter.get("createdAt")) or datetime.now(UTC).isoformat()
        )

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(manga_title, ch_str)
        guid = f"atsumaru:{manga_id}:ch-{ch_str}:{chapter_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                # COMPOSITE: read/chapter needs BOTH ids (see fetch_manifest).
                chapter_id=f"{manga_id}:{chapter_id}",
                language="en",
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=None,
                scanlation_group=None,
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
            language="en",
            page_count=page_count,
            ids={"atsumaruMangaId": manga_id, "atsumaruChapterId": chapter_id},
        )

    @classmethod
    def _language_wanted(cls, languages: list[str] | None) -> bool:
        """True unless the caller asked for languages that exclude English.

        Atsumaru is English-only, so a request constrained to other languages yields
        nothing (return ``[]``). An unset / English-inclusive filter passes through.
        """
        return not languages or "en" in languages

    @staticmethod
    def _build_title(manga_title: str, chapter: str) -> str:
        """Compose a MangaParser-parseable release title (REL-02), MangaDex shape."""
        return f"{manga_title} - Chapter {chapter} (en)"

    @staticmethod
    def _parse_decimal(raw: Any) -> Decimal | None:
        """Parse a chapter number to Decimal (SRCH-06 — preserves ``1.5``)."""
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


def _other_names(doc: dict[str, Any]) -> list[str]:
    """Extract a search hit's ``otherNames`` as a flat list of strings (#139).

    ``otherNames`` may arrive as a list, a single string, or be absent — normalize to
    a list of non-empty strings so the #126 prune can score native/alternate titles.
    """
    raw = doc.get("otherNames")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, str) and s.strip()]
    return []
