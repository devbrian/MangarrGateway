"""Weeb Central source — a none-prep × title→chapter fan-out HYBRID that parses HTML.

Weeb Central (``https://weebcentral.com``) is a server-rendered HTML + **HTMX**
site (NOT a JSON API, NOT a JS-rendered SPA): the endpoint map lives in
``hx-get``/``hx-post`` attributes and every response is an HTML fragment. So this
source is a hybrid no single existing source embodies — **atsumaru's control flow
(none-prep × title→chapter fan-out → mint-after-slice) with mangaball's lxml HTML
parsing (offloaded via ``asyncio.to_thread``)**. It adds **zero networking glue**:
every outbound call is ``ctx.get_bytes`` (the HTML byte primitive — the JSON
primitives would raise on an HTML body, RECON §1 / framework_notes).

Classification (live recon 2026-06-08, residential IP): ``antibot = "none"``
(behind Cloudflare but PASSIVE — every GET answered 200 cold, any UA, no
cookie/CSRF/auth), ``decrypt_scheme = None`` (plain ``.png`` images),
``session_prep = None``.

ENDPOINT SHAPES (live-probed 2026-06-08 — recon 3/4 were HYPOTHESES; these are the
shapes the live smoke confirmed):

* base: ``https://weebcentral.com``
* search (title): ``GET /search/data`` — server-rendered HTML, one
  ``<article class="bg-base-300 …">`` per series (TITLE-ONLY). Series link
  ``href=".../series/{ULID}/{slug}"``; title from the cover
  ``<img alt="<Title> cover">``. ``search`` deep-enumerates each candidate's
  complete chapter list.
* chapter list: ``GET /series/{ULID}/full-chapter-list`` — the COMPLETE list in one
  response (verified 1184/1184 for One Piece, newest-first), one
  ``<a href=".../chapters/{ULID}">`` per chapter carrying ``Chapter N`` span text +
  a ``<time datetime="<RFC3339>">``. **NOT the ~9-entry embedded series-page list.**
* recent: ``GET /latest-updates/1`` — an ID-BEARING feed: one
  ``<article data-tip="<Title>">`` per update carrying BOTH the series link and the
  newest ``/chapters/{ULID}`` link + ``Chapter N`` + ``<time datetime>``. ``recent``
  mints a DIRECT release straight from the feed (like ``mangaball.recent`` — no
  per-title fan-out, no ``:DEFERRED`` late-bind: the id is always present).
* manifest: ``GET /chapters/{ULID}/images`` with
  ``is_prev=False&current_page=1&reading_style=long_strip``
  — server-rendered ``<img src="https://{cdn}/manga/{Series}/{NNNN}-{PPP}.png">``,
  absolute + ordered, ALL pages in one call. **``reading_style=long_strip`` is
  REQUIRED — the endpoint 307-redirects (0 imgs) without it (live-verified). Do NOT
  send an ``HX-Request`` header (→ 307); ``get_bytes`` sends none.**
* image: plain httpx ``GET`` of each absolute CDN ``.png`` (no ``Referer`` needed —
  bare GET → 200, live-verified).

Resolve unit = the BARE chapter ULID (the manifest endpoint needs only
``{chapterId}`` — NOT a composite). guid (D-08):
``weebcentral:{seriesId}:ch-{number}:{chapterId}`` — Weeb Central is English-only
with one upload per chapter, so no language/group segment is needed for uniqueness.

CDN host (SSRF): the page-image host VARIES per series (``scans-hot.planeptune.us``,
``scans.lastation.us``, ``official.lowee.us``, …), so the SSRF allowlist is
host-UNPINNED (mirror ``mangaball._is_allowed_image_url``) — it enforces only the
stable invariants (``https`` + public-host shape + no internal-host + no traversal +
``/manga/`` namespace + image extension). URLs are EXTRACTED verbatim from the
rendered HTML, NEVER reconstructed (RECON §4 / CLAUDE.md SSRF).

Anti-bot caveat (D-12): the ``antibot = "none"`` classification was made from a
RESIDENTIAL IP. A DATACENTER IP MAY trip a managed Cloudflare challenge
(weebcentral.com fronts Cloudflare passively); the escalation is the existing
one-attribute flip (``antibot = "cloudflare"`` + ``cloudflare_challenge_url``) — no
glue change, since the framework already owns the clearance path.

Bulk-load caveat (rate-limit probe; manifest onset pinned 2026-06-10, #189): UNLIKE
the other none-antibot sources, weebcentral DOES server-side rate-limit its HTML
endpoints (real HTTP-429): the manifest endpoint is CLEAN through 90 requests/min and
429s from 120/min PER IP (even at concurrency 1; successes hard-cap ~77/min), and
search 429s at concurrency >=8. Because every HTML call here uses the UNLIMITED
``ctx.get_bytes`` byte path (``limited=False``), the ``rate_limit_per_minute`` limiter
does NOT gate them — so a bulk grab issuing >~100 manifests/min from one IP CAN 429.
Normal interactive/automated use stays far below that (the image CDN, where downloads
spend ~all their requests, is unlimited). RECOMMENDED FOLLOW-UP: route weebcentral's
HTML GETs through a rate-limited primitive (or add a per-source byte-path limiter) so
bulk operations honor the site's real per-IP ceiling — file as a GitHub issue.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

import lxml.html

from ..framework.base import Source
from ..framework.enum_cache import Enumeration
from ..framework.errors import SourceError
from ..framework.relevance import _normalize, prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# Live frontend defaults for ``GET /search/data`` (RECON §3, 2026-06-08). The
# keyword rides ``text``; the rest mirror the site's own search form so server-side
# relevance matches what a user sees. ``get_bytes`` takes NO ``params`` kwarg, so
# these are urlencoded INTO the URL at call time (framework_notes).
_SEARCH_DEFAULT_PARAMS: dict[str, str] = {
    "sort": "Best Match",
    "order": "Descending",
    "official": "Any",
    "anime": "Any",
    "adult": "Any",
    "display_mode": "Full Display",
    "author": "",
}

# Manifest query params (RECON §3). ``reading_style=long_strip`` is REQUIRED — the
# endpoint 307-redirects (0 imgs) without it (live-verified, the #1 trap).
_MANIFEST_PARAMS: dict[str, str] = {
    "is_prev": "False",
    "current_page": "1",
    "reading_style": "long_strip",
}

# search() deep-enumerates at most this many title candidates (mirrors atsumaru's
# GAP-1 lock). Each candidate is a full chapter-list fan-out, so the count is held
# fixed regardless of ``req.interactive``.
_DEFAULT_TITLE_CANDIDATES = 5
# Bounds the per-candidate full-chapter-list fan-out in search() (mirrors atsumaru's
# ``_CHAPTERS_FANOUT_CONCURRENCY``): at most this many chapter-listing fetches run
# concurrently, collapsing wall-clock while staying under the per-source budget.
_CHAPTERS_FANOUT_CONCURRENCY = 6

# Crockford-base32 ULID (26 chars). Weeb Central keys both series and chapters by
# ULID; we capture them from the ``/series/{ULID}/…`` and ``/chapters/{ULID}`` hrefs
# and NEVER reconstruct a URL from them (the host/path are read verbatim, SSRF).
_ULID_RE = re.compile(r"[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26}")
_SERIES_ID_RE = re.compile(r"/series/(" + _ULID_RE.pattern + r")")
_CHAPTER_ID_RE = re.compile(r"/chapters/(" + _ULID_RE.pattern + r")")
# Chapter number after a ``Chapter`` label. Anchored to a clean ``N`` / ``N.M`` shape
# (mirror mangaball's WR-04) so ``_parse_decimal`` always gets a clean capture; a
# non-numeric special (volume/oneshot) simply yields no match → ``number = None``.
_CHAPTER_NUMBER_RE = re.compile(r"chapter\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
# Trailing " cover" suffix on the search cover ``<img alt="<Title> cover">``.
_ALT_COVER_SUFFIX_RE = re.compile(r"\s+cover\s*$", re.IGNORECASE)

# SSRF allowlist for the extracted page-image URLs (T-rkt-01/02, CLAUDE.md). The CDN
# host VARIES per series (RECON §4, live e.g. ``scans-hot.planeptune.us``,
# ``scans.lastation.us``, ``official.lowee.us``) so — like mangaball — it CANNOT be
# pinned to one literal. The meaningful, stable invariants are enforced instead:
# ``https`` + public-host shape (host regex + internal-suffix reject + no traversal)
# + the ``/manga/`` namespace + an image extension. The per-page filename
# (``{NNNN}-{PPP}.png``) varies per upload and is NOT over-pinned beyond the extension.
_WEEBCENTRAL_IMG_PATH_RE = re.compile(
    r"^/manga/[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_WEEBCENTRAL_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)
# Internal/metadata host suffixes the broad host regex would otherwise accept
# (``metadata.google.internal``, ``foo.local`` etc.). The host is NOT pinned to a
# literal, so reject the non-public namespaces explicitly (CR-01 / SSRF).
_WEEBCENTRAL_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".localhost")


def _iso_or_none(raw: str | None) -> str | None:
    """Normalize a Weeb Central ``<time datetime>`` value → RFC3339 ``date-time``.

    The contract's ``Release.publishDate`` is ``format: date-time`` (RFC3339, ``T``
    separator). Weeb Central renders dates as a ``<time datetime="…Z">`` attribute
    — already RFC3339, but we re-serialize through ``datetime`` so a trailing ``Z`` or
    a naive value normalizes to an aware ``…+00:00`` string. Returns ``None`` for a
    missing/unparseable value so the caller can fall back to ``now(UTC)``.
    """
    if not raw:
        return None
    try:
        # ``fromisoformat`` accepts ``Z`` on 3.11+, but the explicit replace keeps the
        # parse robust against a bare-``Z`` value regardless of the runtime.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` looks like a Weeb Central CDN page image (SSRF allowlist).

    Belt-and-suspenders defence on every HTML-extracted manifest URL before the
    framework fetches it (T-rkt-01/02). Rejects non-HTTPS schemes, empty/malformed
    hosts, internal/metadata hostnames, path-traversal, and any path outside the
    ``/manga/`` namespace or without an image extension. The host is NOT pinned to a
    literal — the CDN host varies per series (RECON §4) — so the ``https`` + host
    shape + ``/manga/`` namespace + image extension guards carry it (mirror mangaball).

    CR-01: validate the path httpx will ACTUALLY fetch. httpx normalizes ``..``
    segments before issuing the request, so a raw path like
    ``/manga/../../etc/passwd.png`` would match the regex yet fetch ``/etc/passwd.png``.
    We reject any literal ``..`` segment outright AND validate the
    ``posixpath.normpath``-resolved path.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith(_WEEBCENTRAL_INTERNAL_HOST_SUFFIXES):
        return False
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return (
        parsed.scheme == "https"
        and bool(_WEEBCENTRAL_HOST_RE.match(host))
        and bool(_WEEBCENTRAL_IMG_PATH_RE.match(norm_path))
    )


# ───────────────────────────── lxml HTML parse helpers ─────────────────────────────
# All blocking (lxml C-parse) by design — every caller offloads them via
# ``asyncio.to_thread`` (ruff ASYNC; mirror mangaball._extract_chapter_image_urls).


def _parse_search(html: bytes) -> list[dict[str, str]]:
    """Parse ``GET /search/data`` HTML → ``[{id, title}]`` per series (TITLE-ONLY).

    One top-level ``<article class="bg-base-300 …">`` per result; the series ULID is
    captured from its first ``/series/{ULID}/…`` anchor and the title from the cover
    ``<img alt="<Title> cover">`` (the " cover" suffix stripped). Returns the series
    candidates in document (relevance) order, deduped by id; a result with no series
    id or no title is skipped (it cannot mint a sane guid).
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for article in doc.xpath("//article[contains(@class, 'bg-base-300')]"):
        hrefs = article.xpath(".//a[contains(@href, '/series/')]/@href")
        if not hrefs:
            continue
        match = _SERIES_ID_RE.search(str(hrefs[0]))
        if match is None:
            continue
        series_id = match.group(1)
        if series_id in seen:
            continue
        alts = article.xpath(".//img/@alt")
        title = _ALT_COVER_SUFFIX_RE.sub("", str(alts[0]).strip()) if alts else ""
        if not title:
            continue
        seen.add(series_id)
        out.append({"id": series_id, "title": title})
    return out


def _parse_chapter_list(html: bytes) -> list[dict[str, Any]]:
    """Parse ``/series/{id}/full-chapter-list`` HTML → ordered chapter rows.

    One ``<a href=".../chapters/{ULID}">`` per chapter, in the page's NEWEST-FIRST
    document order. Each row carries the bare chapter ULID, the ``Chapter N`` number
    (``None`` for a non-numeric special), and the ``<time datetime>`` RFC3339 date.
    The COMPLETE list (RECON §6 — 1184/1184 for One Piece, no pagination param).
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for anchor in doc.xpath("//a[contains(@href, '/chapters/')]"):
        match = _CHAPTER_ID_RE.search(anchor.get("href") or "")
        if match is None:
            continue
        num_match = _CHAPTER_NUMBER_RE.search(anchor.text_content())
        times = anchor.xpath(".//time/@datetime")
        out.append(
            {
                "chapter_id": match.group(1),
                "number": num_match.group(1) if num_match else None,
                "date": str(times[0]) if times else None,
            }
        )
    return out


def _parse_recent(html: bytes) -> list[dict[str, Any]]:
    """Parse ``GET /latest-updates/1`` HTML → DIRECT recent rows (ID-BEARING feed).

    One ``<article data-tip="<Title>">`` per update; each carries BOTH the series link
    and the newest ``/chapters/{ULID}`` link + ``Chapter N`` + ``<time datetime>``, so
    a DIRECT release is minted straight from the feed (no per-title fan-out, no
    ``:DEFERRED``). A row missing the series id or the chapter id is skipped.
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for article in doc.xpath("//article[@data-tip]"):
        series_hrefs = article.xpath(".//a[contains(@href, '/series/')]/@href")
        chapter_hrefs = article.xpath(".//a[contains(@href, '/chapters/')]/@href")
        if not series_hrefs or not chapter_hrefs:
            continue
        series_match = _SERIES_ID_RE.search(str(series_hrefs[0]))
        chapter_match = _CHAPTER_ID_RE.search(str(chapter_hrefs[0]))
        if series_match is None or chapter_match is None:
            continue
        num_match = _CHAPTER_NUMBER_RE.search(article.text_content())
        times = article.xpath(".//time/@datetime")
        out.append(
            {
                "series_id": series_match.group(1),
                "chapter_id": chapter_match.group(1),
                "title": (article.get("data-tip") or "").strip() or "Unknown",
                "number": num_match.group(1) if num_match else None,
                "date": str(times[0]) if times else None,
            }
        )
    return out


def _parse_image_urls(html: bytes) -> list[str]:
    """Extract the ordered page-image ``<img src>`` URLs from the manifest HTML.

    The manifest fragment is server-rendered ``<img src="https://{cdn}/manga/…">`` in
    page order (RECON §4 — NOT a JS array, unlike mangaball). EXTRACT verbatim; the
    caller SSRF-allowlists each before any fetch. Returns ``[]`` on a parse miss,
    which the caller turns into a clear ``source_unavailable`` (the ``reading_style``
    trap surfaces here as 0 imgs).
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    return [str(src).strip() for src in doc.xpath("//img/@src") if str(src).strip()]


class WeebCentralSource(Source):
    """Weeb Central (weebcentral.com) — antibot none, no prep, HTML title→chapter.

    A none-prep × title→chapter fan-out HYBRID that parses HTML: atsumaru's control
    flow (relevance-prune → bounded concurrent per-candidate enumeration →
    mint-after-slice) with mangaball's lxml parsing (offloaded via
    ``asyncio.to_thread``). Zero networking glue — every call is ``ctx.get_bytes``
    (the HTML byte primitive; the JSON primitives would raise on an HTML body).
    """

    key = "weebcentral"
    name = "Weeb Central"
    base_url = "https://weebcentral.com"
    # Title-search only — Weeb Central exposes no external metadata-id namespace.
    id_types: list[str] = []
    # English-scanlation focused → no per-chapter language metadata exists.
    languages = ["en"]
    # Probe-MEASURED (scripts/probe_rate_limits.py — residential proxies). UNLIKE the
    # mangaball/mangadot/atsumaru precedent (all "no hard limit" → 480), weebcentral
    # DOES server-side rate-limit its HTML endpoints (real HTTP-429):
    #   * manifest (/chapters/{id}/images): the BINDING per-IP rate constraint. The
    #     2026-06-10 re-probe (#189, faster pool, concurrency 1, cell-timeout 240s)
    #     PINNED the onset that 2026-06-09 left fuzzy: CLEAN through 90/min (30/60/90
    #     all 0 blocked), 429 ONSET at 120/min (77 ok / 43 blocked ≈ 36 %, corroborating
    #     the original 36 % figure), 57 % blocked at 180/min — successes cap ≈ 77/min.
    #   * search (/search/data): clean at concurrency 1 through 180/min (zero 429; the
    #     2026-06-09 90/120 misses were slow-proxy cell-deadlines, re-probe confirmed
    #     clean), but 429s at concurrency >=8, 120/min (max clean parallelism = 4).
    #   * image CDN: NO limit — clean to concurrency 32 and 180/min (a FLOOR).
    # 60 is deliberately retained as the conservative advisory: it sits below the now-
    # PINNED 90/min manifest clean ceiling and at 50 % of the 120/min 429 onset. (The
    # probe's mechanical ~50 %-of-clean-ceiling suggestion was 45; 60 is kept by choice
    # — it is still safely under the measured ceiling and onset.) CRITICAL CAVEAT:
    # weebcentral is an ALL-HTML source — every search/chapter-list/recent/manifest call
    # goes through the UNLIMITED ``ctx.get_bytes`` byte path (``limited=False``), so the
    # limiter does NOT actually gate them; the value is ADVISORY for the /caps display
    # only. The site's real 429 limit is therefore unenforced on our side — see the
    # module docstring's bulk-load caveat and the recommended follow-up (#187: route
    # HTML GETs through a limited primitive).
    rate_limit_per_minute = 60
    # D-30 per-source override: the 2026-06-09 parallelism sweep had the image CDN
    # clean to concurrency 32 (downloads are image-dominated), but the manifest
    # endpoint's per-IP rate sensitivity argues against aggressive job concurrency, so
    # 3 (matching the precedent) is the conservative pick — each job issues only one
    # manifest call, keeping the sustained manifest rate well under the 429 onset. The
    # job manager clamps this to the global max_concurrent_chapters.
    max_concurrent_jobs = 3
    antibot = "none"
    decrypt_scheme = None
    session_prep = None
    supports_search = True
    supports_recent = True

    # ─────────────────────────────── search ──────────────────────────────────

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-chapter Releases (SRCH-01..07, D-08).

        Two-call live flow (mirrors atsumaru's GAP-1 lock): ``GET /search/data`` is
        TITLE-ONLY, so ``search`` deep-enumerates the first
        ``_DEFAULT_TITLE_CANDIDATES`` (relevance-pruned) title candidates via a
        per-candidate ``GET /series/{id}/full-chapter-list`` (the COMPLETE list, NOT
        the embedded series-page subset). For each candidate the chapter list is
        filtered by :meth:`chapter_matches`, kept NEWEST-FIRST (document order),
        sliced to ``req.limit``, and ONLY THEN minted (mint-after-slice — a long title
        like One Piece carries 1184 chapters, and minting per row would evict the
        returned releases' own handles from the store). ZERO networking glue — both
        calls are ``ctx.get_bytes`` (HTML), lxml-parsed via ``asyncio.to_thread``.
        """
        if not self._language_wanted(req.languages):
            return []

        async def _resolve_fn() -> list[dict[str, str]]:
            params = {"text": req.query or "", **_SEARCH_DEFAULT_PARAMS}
            url = f"{self.base_url}/search/data?{urlencode(params)}"
            html = await ctx.get_bytes(url)
            documents = await asyncio.to_thread(_parse_search, html)
            # Prune obviously-irrelevant candidates BEFORE the per-candidate fan-out
            # (#126). An exact-match query enumerates only the one correct title;
            # ambiguous queries fall back to ``[:_DEFAULT_TITLE_CANDIDATES]``.
            return prune_candidates(
                documents,
                req.query or "",
                keys=lambda d: [d.get("title")],
                cap=_DEFAULT_TITLE_CANDIDATES,
            )

        # Layer 1 (D-01): cache the title→pruned-candidate resolution so a repeat
        # chapter search on the same (query, languages) skips the /search/data call.
        candidates: list[dict[str, str]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(req.query or ""), req.languages or []),
            _resolve_fn,
        )
        ctx.candidates_enumerated = len(candidates)
        per_candidate_limit = req.limit or 50
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(series_id: str, manga_title: str) -> list[Release]:
            # Layer 2 (CACHE-02/03): cache the UNFILTERED per-candidate chapter list
            # per (series_id). The semaphore + the GET live INSIDE ``_enum_fn`` so a
            # cache HIT acquires neither the fan-out slot nor a rate-limit token.
            async def _enum_fn() -> Enumeration:
                async with sem:
                    html = await ctx.get_bytes(
                        f"{self.base_url}/series/{series_id}/full-chapter-list"
                    )
                rows = await asyncio.to_thread(_parse_chapter_list, html)
                return Enumeration(
                    items=rows,
                    chapter_numbers=tuple(
                        d
                        for c in rows
                        if (d := self._parse_decimal(c.get("number"))) is not None
                    ),
                    exhausted=True,  # full-chapter-list is the COMPLETE feed
                    requested_limit=per_candidate_limit,
                )

            # IN-02: the chapter list does not depend on ``languages`` (English-only
            # site), so key on ``[]`` — every request for one title shares the feed.
            enum = await ctx.cached_enumerate(
                ctx.cached_enumerate_key(series_id, []), _enum_fn
            )
            return self._chapters_to_releases(
                enum.items, series_id, manga_title, per_candidate_limit, ctx, req
            )

        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for doc in candidates:
            series_id = doc.get("id")
            if not series_id:
                continue
            manga_title = str(doc.get("title") or "Unknown")
            tasks.append(_fetch_candidate(str(series_id), manga_title))

        # gather (not TaskGroup): re-raise the FIRST child SourceError UNCHANGED so
        # fanout.py classifies it as the source's own failure (mirror atsumaru).
        results = await asyncio.gather(*tasks)
        releases: list[Release] = []
        for chunk in results:
            releases.extend(chunk)
        return releases

    def _chapters_to_releases(
        self,
        chapters: list[Any],
        series_id: str,
        manga_title: str,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one candidate's chapter list → per-chapter Releases (mint-after-slice).

        Filtered by :meth:`chapter_matches`, kept NEWEST-FIRST (the full-chapter-list
        document order IS newest-first, RECON §6), sliced to ``limit``, THEN minted —
        handle count per candidate is bounded by ``limit`` so the returned releases'
        handles always survive the store cap.
        """
        rows: list[dict[str, Any]] = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            if not chapter.get("chapter_id"):
                continue  # no resolve unit
            number = self._parse_decimal(chapter.get("number"))
            if not self.chapter_matches(req, number):
                continue
            rows.append(chapter)
        releases: list[Release] = []
        for chapter in rows[:limit]:  # mint AFTER slice (newest-first preserved)
            rel = self._to_release(series_id, manga_title, chapter, ctx)
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
        """Newest-updated feed → DIRECT newest-chapter releases (RCNT-01/02).

        GETs ``/latest-updates/1`` (an ID-BEARING feed — each ``<article>`` carries
        BOTH the series link and the newest ``/chapters/{ULID}`` link + number + date),
        then mints a DIRECT release straight from each row — no per-title fan-out, no
        ``:DEFERRED`` late-bind (the id is always present, like ``mangaball.recent``).
        The route applies the authoritative newest-first sort + ``since`` cut; the
        source-side ``since`` is left to the route (IN-01). Zero glue (``get_bytes``).
        """
        if not self._language_wanted(languages):
            return []
        html = await ctx.get_bytes(f"{self.base_url}/latest-updates/1")
        rows = await asyncio.to_thread(_parse_recent, html)
        releases: list[Release] = []
        for row in rows:
            rel = self._to_release(str(row["series_id"]), str(row["title"]), row, ctx)
            if rel is not None:
                releases.append(rel)
        return releases

    # ─────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a BARE chapter ULID → ordered page URLs (PKG-01/R6).

        Both ``search`` and ``recent`` mint the bare chapter ULID as the
        ``ResolutionRecord.chapter_id`` (the manifest endpoint needs only that). GETs
        ``/chapters/{id}/images?…&reading_style=long_strip`` (``reading_style`` is
        REQUIRED — the endpoint 307-redirects to 0 imgs without it, RECON §6), extracts
        the ordered ``<img src>`` URLs VERBATIM (never reconstructed — RECON §4 /
        CLAUDE.md SSRF), SSRF-allowlists EVERY one before return (raising on the first
        rejection — no blind fetch, T-rkt-01), and guards the extracted count against
        ``ctx.expected_pages`` when known (IN-03, mirror atsumaru). The large HTML
        parse is offloaded via ``asyncio.to_thread`` (ruff ASYNC).
        """
        params = urlencode(_MANIFEST_PARAMS)
        html = await ctx.get_bytes(
            f"{self.base_url}/chapters/{chapter_id}/images?{params}"
        )
        urls = await asyncio.to_thread(_parse_image_urls, html)
        if not urls:
            raise SourceError(
                "source_unavailable",
                f"no page images for chapter {chapter_id} "
                "(reading_style=long_strip required)",
            )
        for url in urls:
            if not _is_allowed_image_url(url):
                # Never fetch a non-allowlisted (off-host / off-shape) URL (T-rkt-01).
                raise SourceError(
                    "source_unavailable",
                    f"chapter image URL failed the SSRF allowlist: {url!r}",
                )
        if ctx.expected_pages is not None and len(urls) != ctx.expected_pages:
            raise SourceError(
                "source_unavailable",
                f"manifest integrity: extracted {len(urls)} images, "
                f"chapter declares {ctx.expected_pages} pages",
            )
        return urls

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes via the shared session (PKG-02).

        A one-line delegate (mirror atsumaru/mangaball): bounded by the per-job
        semaphore, NOT the per-source API limiter. Plain ``.png`` — no decrypt, and no
        ``Referer`` (a bare GET returns 200, live-verified 2026-06-08).
        """
        return await ctx.get_bytes(url)

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        series_id: str,
        manga_title: str,
        chapter: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single parsed chapter row (D-08)."""
        chapter_id = chapter.get("chapter_id")
        if not chapter_id:
            return None
        chapter_id = str(chapter_id)
        chapter_number = self._parse_decimal(chapter.get("number"))
        publish_date = (
            _iso_or_none(chapter.get("date")) or datetime.now(UTC).isoformat()
        )

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(manga_title, ch_str)
        guid = f"weebcentral:{series_id}:ch-{ch_str}:{chapter_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=chapter_id,  # the BARE chapter ULID (manifest needs only it)
                language="en",
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=None,
                scanlation_group=None,
                page_count=None,
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
            scanlation_group=None,
            page_count=None,
            ids={"weebcentralSeriesId": series_id, "weebcentralChapterId": chapter_id},
        )

    @classmethod
    def _language_wanted(cls, languages: list[str] | None) -> bool:
        """True unless the caller asked for languages that exclude English.

        Weeb Central is English-only, so a request constrained to other languages
        yields nothing (return ``[]``). An unset / English-inclusive filter passes.
        """
        return not languages or "en" in languages

    @staticmethod
    def _build_title(manga_title: str, chapter: str) -> str:
        """Compose a MangaParser-parseable release title (REL-02), MangaDex shape.

        Weeb Central is English-only with one upload per chapter, so the title carries
        no ``[group]`` segment (no scanlation group is surfaced).
        """
        return " ".join([manga_title, "-", f"Chapter {chapter}", "(en)"])

    @staticmethod
    def _parse_decimal(raw: Any) -> Decimal | None:
        """Parse a chapter number to Decimal (SRCH-06 — preserves ``10.5``)."""
        if raw is None or raw == "":
            return None
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
