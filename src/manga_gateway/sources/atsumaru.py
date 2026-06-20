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
* scanlation group names: a chapter row carries only an opaque ``scanlationMangaId``,
  NOT the group NAME — the name map lives in ``GET /api/manga/page?id=`` →
  ``mangaPage.scanlators`` (``[{id, name}]``; One Piece ``[{… "Alpha"}]``, Solo
  Leveling ``[Alpha, Asura, Flame]``). Enumeration fetches it alongside ``allChapters``
  (concurrently, BEST-EFFORT — an advisory group must never fail the enumeration) and
  resolves each chapter's ``scanlationMangaId`` → ``scanlationGroup``. (``manga.page``
  itself is title-only-windowed via ``hasMoreChapters`` — NOT a complete chapter
  source — so it is used ONLY for the scanlators map, never for enumeration.)
* recent: ``GET /api/infinite/recentlyUpdated?page=&types=`` →
  ``{items:[{id,title,type,…}]}`` — a TITLE-ONLY newest-updated feed (no embedded
  chapter), so ``recent`` fans out ONE ``GET /api/manga/page?id=`` per title
  (``mangaPage.chapters`` newest-first window + ``mangaPage.scanlators`` in a single
  response) and mints the newest chapter — NOT ``allChapters`` (recent needs only the
  newest chapter, not the complete feed), keeping it at ``1 + N`` calls per poll.
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
from ..framework.external_links import normalize
from ..framework.relevance import _normalize, prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import ExternalLinks, SearchRequest

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
# Source-wide fan-out concurrency: the single bound on every per-source fan-out
# (search()'s per-candidate allChapters fetches AND recent()'s per-title manga.page
# fetches). Sized to ``_RECENT_TITLE_CAP`` so the widest fan-out — recent's 20-wide
# ``manga.page`` set — runs in ONE wave. atsu.moe carries a heavy intermittent latency
# tail (≈0.2s on a CDN hit, ≈4s+ on a cache miss, up to ~21s under load, debug
# ``atsumaru-unavailable-58171``); at the old search-tuned concurrency 6 a recent
# poll's cumulative wall-clock spanned ~4 sequential waves and could exceed fanout.py's
# 30s per-source ``asyncio.timeout``, cancelling the source → ``source_unavailable /
# "timed out"`` despite every call returning 200. A single wave keeps the worst case at
# ~recentlyUpdated + the slowest single page, well under 30s. The shared 480/min (8/s)
# limiter still paces issuance, so atsu.moe never sees more than ~8 concurrent — inside
# the probe-validated concurrency-32 ceiling (2026-06-08 parallelism sweep, zero
# throttling). search() enumerates ≤5 candidates, so the wider bound is a harmless
# ceiling there (already one wave).
_CHAPTERS_FANOUT_CONCURRENCY = 20
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
    # An out-of-range / absurd epoch (huge or very negative millis) makes
    # ``datetime.fromtimestamp`` raise OverflowError/OSError/ValueError; swallow it
    # and fall back rather than letting a single malformed ``createdAt`` crash the
    # whole search/recent parse (CodeRabbit #184; mirrors the source's
    # never-crash-on-a-bad-field discipline).
    try:
        return datetime.fromtimestamp(millis / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


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
    # Probe-tuned (2026-06-08, scripts/probe_rate_limits.py): two sweeps across fresh
    # residential proxies found NO hard limit on atsumaru — zero
    # 429/403/Cloudflare-challenge/Retry-After and zero latency degradation on ANY
    # endpoint. The rate-ceiling sweep sustained 960/min cleanly at concurrency 8
    # (search + manifest + image alike) — a FLOOR (the true ceiling is higher). 480 is
    # the conservative ~50%-of-floor suggestion, the same shape as the mangaball/
    # mangadot precedent (#101). The limiter is SHARED across search + manifest API
    # calls; image bytes are exempt (``get_bytes`` is ``limited=False``).
    rate_limit_per_minute = 480
    # D-30 per-source override: the 2026-06-08 parallelism sweep sustained concurrency
    # 32 cleanly on every endpoint (zero throttling), so chapter downloads parallelize
    # safely. 3 mirrors the mangaball/mangadot precedent (#101); the job manager clamps
    # it to the global max_concurrent_chapters (8). (This governs concurrent download
    # JOBS for this source, not search fan-out.)
    max_concurrent_jobs = 3
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
                    # allChapters (load-bearing) + the manga.page metadata fetched
                    # concurrently; the page resolve is BEST-EFFORT (helper swallows
                    # its own errors → ({}, {})), so it never fails the enumeration.
                    # The page body carries BOTH the scanlator map AND the tracker-link
                    # fields, so links ride this existing fetch — zero added HTTP (R5).
                    listing, page_meta = await asyncio.gather(
                        ctx.get_json(
                            f"{self.base_url}/api/manga/allChapters", mangaId=manga_id
                        ),
                        self._fetch_page_meta(manga_id, ctx),
                    )
                group_names, links_raw = page_meta
                # Stash the raw tracker links for the zero-HTTP fetch_external_links
                # hook to read back (only when present; happens on an enum MISS — a
                # repeat search serves links from the resolve-cache HIT, Pitfall 7).
                if links_raw:
                    ctx.external_links_raw[manga_id] = links_raw
                chapters = listing.get("chapters")
                rows = chapters if isinstance(chapters, list) else []
                _attach_group_names(rows, group_names)
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
            releases = self._chapters_to_releases(
                enum.items, manga_id, manga_title, per_candidate_limit, ctx, req
            )

            # Phase-13 (R4/R5): resolve the series' tracker links ONCE (the framework
            # owns the cache/timeout/swallow) from the stashed mangaPage fields, then
            # stamp the single resolved object onto every release of the series.
            async def _parse_links() -> ExternalLinks | None:
                return await self.fetch_external_links(manga_id, ctx)

            links = await ctx.resolve_external_links(manga_id, _parse_links)
            for rel in releases:
                rel.external_links = links
            return releases

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

    # IN-02: recent() intentionally does NOT populate Release.externalLinks. Atsumaru's
    # recent feed does not carry tracker links in the response, so there is no zero-cost
    # source for them on this path; interactive search() does carry them. See review
    # finding IN-02 if a future phase needs externalLinks on recent rows.
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
        fans out exactly ONE ``GET /api/manga/page?id=`` per title — that detail
        endpoint returns the newest-first chapter WINDOW *and* the scanlators map in a
        single response, so recent needs no separate ``allChapters`` (it only wants the
        single newest chapter, not the complete feed) and no separate scanlators call.
        This keeps recent at ``1 + N`` calls, not ``1 + 2N`` (#recent-call-volume). The
        newest chapter mints a DIRECT release (id always present, no ``:DEFERRED``). The
        route applies the authoritative newest-first sort + ``since`` cut; the
        source-side ``since`` is left to the route (IN-01). Zero glue (SRC-02).
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
        # Single-wave concurrency: the source-wide _CHAPTERS_FANOUT_CONCURRENCY (20) is
        # sized to the title cap, so the whole manga.page fan-out completes inside the
        # 30s per-source budget even on atsu.moe's slow latency tail (debug
        # atsumaru-unavailable-58171).
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _newest_release(manga_id: str, manga_title: str) -> Release | None:
            # ONE call per title: ``manga.page`` returns the newest-first chapter
            # WINDOW *and* the scanlators map together (#recent-call-volume) — recent
            # only needs the single newest chapter, so the complete ``allChapters``
            # feed (up to 1000+ rows) is NOT fetched here. (search still uses
            # ``allChapters`` because it needs the COMPLETE list to match a chapter.)
            async with sem:
                body = await ctx.get_json(
                    f"{self.base_url}/api/manga/page", id=manga_id
                )
            page = body.get("mangaPage") if isinstance(body, dict) else None
            if not isinstance(page, dict):
                return None
            chapters = page.get("chapters")
            rows = chapters if isinstance(chapters, list) else []
            # The window is newest-first; chapters[0] is the newest (verified == the
            # allChapters head). Take the first row carrying a usable id.
            newest = next(
                (c for c in rows if isinstance(c, dict) and c.get("id")), None
            )
            if newest is None:
                return None
            group_names = _scanlator_names(page)
            newest["_group"] = group_names.get(
                str(newest.get("scanlationMangaId") or "")
            )
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
        # Resolved scanlation group name (baked onto the row at enumeration time by
        # ``_attach_group_names`` from the manga.page scanlators map). ``None`` when
        # the best-effort lookup found nothing — an advisory field (REL-03/D-19).
        group = chapter.get("_group") or None

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(manga_title, ch_str, group=group)
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
            language="en",
            scanlation_group=group,
            page_count=page_count,
            ids={"atsumaruMangaId": manga_id, "atsumaruChapterId": chapter_id},
        )

    async def _fetch_page_meta(
        self, manga_id: str, ctx: SourceContext
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Best-effort ``(scanlator-name map, raw tracker links)`` from ``manga.page``.

        ONE ``GET /api/manga/page?id=`` carries BOTH the ``scanlators`` map (REL-03 —
        ``[{id, name}]``, resolved from the chapter rows' opaque ``scanlationMangaId``)
        AND the series' 8 tracker-link fields (Pitfall 5) on the SAME ``mangaPage``
        body. Both are advisory (D-19/D-03): ANY missing/malformed shape OR a fetch
        error (``SourceError`` swallowed) degrades to ``({}, {})`` so a flaky/absent
        ``manga.page`` never fails the load-bearing ``allChapters`` enumeration. The
        links are returned VERBATIM (raw upstream keys) — the framework normalizer owns
        the canonical mapping + the kenmei URL->slug rule.
        """
        try:
            body = await ctx.get_json(f"{self.base_url}/api/manga/page", id=manga_id)
        except SourceError:
            return {}, {}
        # Defence-in-depth: ``get_json`` already raises SourceError on a non-object
        # body (caught above), but guard explicitly so the never-fail-enumeration
        # contract holds without relying on the transport's internals (CodeRabbit #185).
        if not isinstance(body, dict):
            return {}, {}
        page = body.get("mangaPage")
        return _scanlator_names(page), _mangapage_links(page)

    async def fetch_external_links(
        self, series_id: str, ctx: SourceContext
    ) -> ExternalLinks | None:
        """Parse the stashed ``mangaPage`` tracker fields → canonical links (D-01/R5).

        PARSING ONLY (zero added HTTP): reads the raw link dict stashed during
        enumeration on ``ctx.external_links_raw`` (lifted off the same ``mangaPage``
        body the scanlator map came from, Pitfall 5) and routes it through the shared
        normalizer — ``None`` when the series carried no tracker fields. The framework
        owns the resolve-once cache + best-effort timeout/swallow.
        """
        # IN-03: ``normalize`` short-circuits a falsy ``raw`` (cache miss → ``None``)
        # to ``None`` itself, so the ``or {}`` guard is unnecessary — pass through.
        return normalize(ctx.external_links_raw.get(series_id), "atsumaru")

    @classmethod
    def _language_wanted(cls, languages: list[str] | None) -> bool:
        """True unless the caller asked for languages that exclude English.

        Atsumaru is English-only, so a request constrained to other languages yields
        nothing (return ``[]``). An unset / English-inclusive filter passes through.
        """
        return not languages or "en" in languages

    @staticmethod
    def _build_title(
        manga_title: str, chapter: str, *, group: str | None = None
    ) -> str:
        """Compose a MangaParser-parseable release title (REL-02), MangaDex shape.

        Appends a ``[group]`` segment when the scanlation group is known (mirrors
        ``mangaball``/``mangadex``) so Mangarr can parse + match on the group.
        """
        parts = [manga_title, "-", f"Chapter {chapter}", "(en)"]
        if group:
            parts.append(f"[{group}]")
        return " ".join(parts)

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


# The 8 tracker-link fields Atsumaru carries on the ``mangaPage`` body (13-RESEARCH
# frozen table, Pitfall 5). All bare values except ``kenmeiUrl`` (a full URL); the
# framework normalizer owns the canonical mapping + the kenmei URL->slug extraction.
_ATSUMARU_LINK_KEYS = (
    "anilistId",
    "malId",
    "mangaUpdatesId",
    "mangaBakaId",
    "kitsuId",
    "apId",
    "annId",
    "kenmeiUrl",
)


def _mangapage_links(page: Any) -> dict[str, Any]:
    """Lift the raw tracker-link fields off a ``mangaPage`` object (Pitfall 5).

    Returns the subset of :data:`_ATSUMARU_LINK_KEYS` present (truthy) on the page —
    the RAW upstream keys/values, fed verbatim to ``framework.external_links.normalize``
    (the single owner of the canonical mapping + the kenmei URL->slug rule). ``{}`` for
    a non-dict page or one carrying no tracker fields. Read off the SAME body the
    scanlator map came from — zero added HTTP.
    """
    if not isinstance(page, dict):
        return {}
    return {key: page[key] for key in _ATSUMARU_LINK_KEYS if page.get(key)}


def _scanlator_names(page: Any) -> dict[str, str]:
    """Parse a ``mangaPage`` object's ``scanlators`` → ``{id: name}`` (REL-03).

    Best-effort: returns ``{}`` for any non-dict ``page`` / missing-or-malformed
    ``scanlators`` so the advisory group never breaks the caller. Shared by the
    search path (``_fetch_page_meta`` GETs ``manga.page`` for the map + tracker links)
    and the recent path (which already holds the ``manga.page`` body and needs both the
    newest chapter and the map from it).
    """
    scanlators = page.get("scanlators") if isinstance(page, dict) else None
    if not isinstance(scanlators, list):
        return {}
    return {
        str(s["id"]): str(s["name"])
        for s in scanlators
        if isinstance(s, dict) and s.get("id") and s.get("name")
    }


def _attach_group_names(rows: list[Any], names: dict[str, str]) -> None:
    """Bake the resolved scanlation group name onto each chapter row (``_group``).

    Maps each row's ``scanlationMangaId`` through the ``manga.page`` scanlators map
    so :meth:`AtsumaruSource._to_release` can read ``chapter["_group"]`` without a
    second lookup. Mutates ``rows`` in place; a row with no/unknown scanlator gets
    ``_group=None``. The enriched rows are what the Layer-2 enum cache stores.
    """
    for row in rows:
        if isinstance(row, dict):
            row["_group"] = names.get(str(row.get("scanlationMangaId") or ""))


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
