"""ProjectSuki source — the first pure-HTML-scraped ``none``-prep source (SRC-01).

ProjectSuki (``https://projectsuki.com``) is a **server-rendered HTML aggregator**
(Bootstrap, no SPA): every search/series/chapter datum lives in the initial HTML,
so each hook fetches HTML via ``ctx.get_bytes(url, limited=True)`` and parses it with
**lxml**, ALWAYS offloaded through ``asyncio.to_thread`` (RESEARCH Pitfall 6 / ruff
ASYNC — never parse on the event loop). It adds **zero networking glue** — every
outbound call is ``ctx.get_bytes`` / ``ctx.post_json_body`` (SRC-01/SRC-02).

Structurally it mirrors :mod:`atsumaru` — a **two-call title→chapter fan-out**
(the search page is title-only, so each candidate is deep-enumerated for its
chapters) with mint-after-slice, a COMPOSITE ``{bookId}:{chapterId}`` chapter id,
an SSRF allowlist on the extracted page-image URLs, the ``ctx.expected_pages``
integrity guard, and a one-line ``fetch_image``. The only structural wrinkle is the
manifest: the reader streams its pages through a ``POST /callpage`` page-walk rather
than a single JSON array (see :meth:`fetch_manifest`).

ENDPOINT SHAPES (live-recon-pinned, 2026-06-20):

* base: ``https://projectsuki.com``
* search (title-only): ``GET /search?q={q}`` → HTML. Book results are
  ``<a href="/book/{bookId}">{Title}</a>`` anchors (a thumbnail anchor with an
  ``<img>`` + a title anchor inside an ``<h4>``). ``search`` extracts
  ``(bookId, title)`` pairs, prunes to ``_DEFAULT_TITLE_CANDIDATES`` candidates, and
  deep-enumerates each book's chapter list. NO chapters on this page.
* chapter list (per series, COMPLETE, no pagination): ``GET /book/{bookId}`` → HTML.
  Two parseable sources, both keyed off the ``/read/{bookId}/{chapterId}/1`` href:
  chapter rows (``<tr class="row mx-0">`` with the read anchor's ``Chapter {N}``
  text + ``title="{subtitle}"`` + a ``/group/{gid}`` anchor + an "English" language
  token + a date) AND the ``<select id="chapter-select">`` dropdown
  (``<option value="{chapterId}">{N} - {subtitle} - {GroupName}</option>``, a clean
  fallback for number/subtitle/group). Verified COMPLETE (221 chapters for Solo
  Leveling in one page). The ``chapterId`` ALWAYS comes from the ``/read`` href.
* recent: ``GET /`` (homepage) → the "Latest Chapters" cards, each carrying a book
  link ``/book/{bookId}``, the newest chapter link ``/read/{bookId}/{chapterId}/1``,
  a ``/group/{gid}`` anchor, and an "English" token. Mints DIRECT releases (both ids
  present). The route owns the authoritative newest-first sort + ``since`` cut (IN-01).
* manifest (per chapter — EXTRACT, never reconstruct):
  1. ``GET /read/{bookId}/{chapterId}/1`` → HTML. The ``.strip-reader`` div carries
     page **1**'s ``<img src="https://projectsuki.com/images/gallery/{bookId}/{hash}
     /001?{ts}">`` (gives page 1 + the per-chapter ``{hash}``).
  2. ``POST /callpage`` with JSON body ``{bookid, chapterid, first}`` →
     ``{chapter_id, src, super}``. ``src`` is an HTML fragment of ``<img>`` tags for
     the REMAINING pages. LOOP while ``super`` is truthy, passing the returned
     ``chapter_id`` back as ``chapterid`` (``first=False``), bounded by
     ``_CALLPAGE_MAX_ITERATIONS``. (Live: a 24-page chapter returned pages 002–0024
     in ONE response, ``super`` falsy → 1 iteration.)
  3. Concatenate [page-1 url] + [all /callpage urls] in order, SSRF-allowlist EACH,
     and guard the count against ``ctx.expected_pages`` when known.
  ⚠️ Page filenames are ``00{n}`` (page 1=``001``, 10=``0010``, 24=``0024``) with NO
  file extension + a ``?{ts}`` query — never reconstruct page numbers, and the SSRF
  regex allows the extension-less numeric page path.
* image: plain ``GET`` of each absolute ``https://projectsuki.com/images/gallery/...``
  URL → ``image/jpeg``. ``fetch_image`` is a one-line ``ctx.get_bytes(url)``. Live-tune:
  confirm no ``Referer`` is needed (a bare GET returned 200 in recon).

guid (D-08): ``projectsuki:{bookId}:ch-{number}:{chapterId}``. The chapterId is
already unique per chapter, so no language/group segment is needed (English-only,
one upload per chapter).

Anti-bot caveat (D-12): the ``antibot = "none"`` classification was made from a
residential IP — Cloudflare fronts the site in CDN-only/passive mode (direct 200,
no challenge). If a datacenter IP (or a future site change) trips a MANAGED
challenge, the escalation is the documented one-attribute flip
(``antibot = "cloudflare"`` + ``cloudflare_challenge_url`` + a ``solver_engine``) —
the framework already owns the clearance path, so no networking glue changes.
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

# search() deep-enumerates at most this many title candidates (mirrors atsumaru's
# GAP-1 lock). Each candidate is a full /book chapter-list fan-out, so the count is
# held fixed regardless of ``req.interactive``.
_DEFAULT_TITLE_CANDIDATES = 5
# Bounds the per-candidate ``/book/{id}`` fan-out in search() — at most this many
# book-page fetches run concurrently. The Semaphore is acquired INSIDE the cached
# ``_enum_fn`` so a cache HIT takes no slot (CACHE-03). Conservative; the orchestrator
# probe-tunes it alongside ``rate_limit_per_minute`` (Step 5.5).
_CHAPTERS_FANOUT_CONCURRENCY = 5
# Hard cap on the /callpage page-walk so a malformed ``super`` flag (or a server that
# never stops returning ``super:true``) can never loop forever. Live a single chapter
# resolved in 1 iteration, so 50 is a generous safety ceiling.
_CALLPAGE_MAX_ITERATIONS = 50

# SSRF allowlist for the extracted page-image URLs (T-07-07/T-07-09, CLAUDE.md).
# Page images are same-origin (``projectsuki.com/images/gallery/...``), so the host is
# PINNED. The page path is ``/images/gallery/{bookId}/{hexHash}/{pageNumber}`` with NO
# file extension and a ``?{ts}`` query (the query is stripped by ``urlparse`` before
# the path match). Filenames are ``00{n}`` (001, 0010, 0024) — the trailing ``\d+``
# accepts any width; we do NOT over-pin the page-number padding (live W: padStart(3)
# reconstruction 404s at page ≥10, which is exactly why we EXTRACT, never rebuild).
_PROJECTSUKI_IMAGE_HOSTS = frozenset({"projectsuki.com", "www.projectsuki.com"})
_PROJECTSUKI_IMG_PATH_RE = re.compile(r"^/images/gallery/\d+/[A-Fa-f0-9]+/\d+$")

# Book-detail href (``/book/{bookId}``) — the search-result + recent-card series link.
_BOOK_HREF_RE = re.compile(r"^/book/(\d+)")
# Read href (``/read/{bookId}/{chapterId}/{page}``) — the SOURCE of the chapterId on
# both the book page (chapter rows) and the homepage (recent cards). Never rebuilt.
_READ_HREF_RE = re.compile(r"^/read/(\d+)/([^/?#]+)/")
# Chapter number after a ``Chapter``/``Ch.`` label in the read-anchor text. Anchored to
# a clean ``N``/``N.M`` shape so ``_parse_decimal`` gets a clean capture (keeps 1.5).
_CHAPTER_NUMBER_RE = re.compile(r"(?:chapter|ch\.?)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# Leading numeric token for the dropdown option text (``{N} - {subtitle} - {Group}``).
_LEADING_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)")
# Best-effort date capture from a chapter row / recent card (LIVE-TUNE: the exact date
# rendering was not pinned in recon — this matches ISO ``YYYY-MM-DD`` and the common
# ``Mon D, YYYY`` / ``D Mon YYYY`` shapes; an unmatched/relative date falls to now).
_DATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}"
    r"|[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+[A-Z][a-z]{2,8}\.?\s+\d{4})"
)


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` is a ProjectSuki same-origin page image (SSRF allowlist).

    Belt-and-suspenders defence on every extracted manifest URL before the framework
    fetches it (T-07-07/T-07-09). The page-image host is fixed (``projectsuki.com``)
    and the path namespace is ``/images/gallery/{bookId}/{hexHash}/{pageNumber}`` with
    NO file extension; we reject non-HTTPS schemes, off-host URLs, path traversal, and
    any path outside that namespace. We validate the ``posixpath.normpath``-resolved
    path (httpx normalizes ``..`` before issuing the request) and reject any literal
    ``..`` segment outright. The ``?{ts}`` query is dropped by ``urlparse``.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return (
        parsed.scheme == "https"
        and host in _PROJECTSUKI_IMAGE_HOSTS
        and bool(_PROJECTSUKI_IMG_PATH_RE.match(norm_path))
    )


def _date_to_iso(raw: Any) -> str | None:
    """Best-effort parse a chapter-row/recent-card date → RFC3339 ``date-time``.

    The contract's ``Release.publishDate`` is ``format: date-time`` (RFC3339, ``T``
    separator). Tries ``datetime.fromisoformat`` first, then a few common absolute
    formats. Returns ``None`` for a missing/relative/unparseable value so the caller
    falls back to ``now(UTC)`` (Pitfall 4). LIVE-TUNE: the exact date rendering is a
    recon gap — extend the format list once the live shape is confirmed.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return aware.isoformat()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).isoformat()
        except ValueError:
            continue
    return None


# ─────────────────────────── HTML parse helpers (sync) ───────────────────────────
# Every helper is a pure, blocking lxml parse run via ``asyncio.to_thread`` at the
# call site (ruff ASYNC). Each is defensive: a missing/malformed field degrades to
# ``None`` / is skipped rather than crashing the whole parse.


def _parse_search_results(html: str) -> list[dict[str, str]]:
    """Parse ``GET /search`` HTML → ``[{book_id, title}, …]`` (deduped, page order).

    Book results appear as both a thumbnail anchor (``<img>``, empty text) and a
    title anchor (inside an ``<h4>``), both ``href="/book/{bookId}"``. We keep the
    first NON-empty title per ``bookId`` and preserve first-seen order. Title-only —
    no chapters on this page.
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    books: dict[str, str] = {}
    for anchor in doc.iter("a"):
        href = anchor.get("href") or ""
        match = _BOOK_HREF_RE.match(href)
        if match is None:
            continue
        book_id = match.group(1)
        title = anchor.text_content().strip()
        if book_id not in books:
            books[book_id] = title
        elif not books[book_id] and title:
            books[book_id] = title
    return [{"book_id": bid, "title": title} for bid, title in books.items() if title]


def _parse_book_chapters(html: str, book_id: str) -> list[dict[str, Any]]:
    """Parse ``GET /book/{bookId}`` HTML → the COMPLETE chapter list (defensive).

    Primary source: the ``/read/{bookId}/{chapterId}/1`` anchors (the chapterId is
    ALWAYS read from the href, never reconstructed). Each anchor yields the chapter
    number (``Chapter {N}`` in its text), the subtitle (its ``title`` attr), and —
    from its enclosing row — the scanlation group (``/group/`` anchor), language, and
    date. The ``<select id="chapter-select">`` dropdown supplements missing
    number/subtitle/group and back-fills any chapter that has no parseable row.
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    dropdown = _parse_chapter_dropdown(doc)
    chapters: dict[str, dict[str, Any]] = {}
    read_re = re.compile(rf"^/read/{re.escape(book_id)}/([^/?#]+)/")
    for anchor in doc.iter("a"):
        href = anchor.get("href") or ""
        match = read_re.match(href)
        if match is None:
            continue
        chapter_id = match.group(1)
        if chapter_id in chapters:
            continue
        num_match = _CHAPTER_NUMBER_RE.search(anchor.text_content())
        number = num_match.group(1) if num_match is not None else None
        subtitle = (anchor.get("title") or "").strip() or None
        group: str | None = None
        language: str | None = None
        date_raw: str | None = None
        row = _enclosing_row(anchor)
        if row is not None:
            group = _row_group(row)
            row_text = row.text_content()
            language = _detect_language(row_text)
            date_raw = _extract_date(row_text)
        drop = dropdown.get(chapter_id)
        if drop is not None:
            number = number or drop.get("number")
            subtitle = subtitle or drop.get("subtitle")
            group = group or drop.get("group")
        chapters[chapter_id] = {
            "chapter_id": chapter_id,
            "number": number,
            "subtitle": subtitle,
            "group": group,
            "language": language or "English",
            "date_raw": date_raw,
        }
    # Dropdown-only back-fill: any chapter the row parse missed (rows absent/odd shape).
    for chapter_id, drop in dropdown.items():
        if chapter_id in chapters:
            continue
        chapters[chapter_id] = {
            "chapter_id": chapter_id,
            "number": drop.get("number"),
            "subtitle": drop.get("subtitle"),
            "group": drop.get("group"),
            "language": "English",
            "date_raw": None,
        }
    return list(chapters.values())


def _parse_chapter_dropdown(doc: Any) -> dict[str, dict[str, str | None]]:
    """Parse the ``<select id="chapter-select">`` options → ``{chapterId: fields}``.

    Each option is ``<option value="{chapterId}">{N} - {subtitle} - {Group}</option>``;
    the leading token is the chapter number and the ``" - "``-split tail carries the
    subtitle + group. A robust, complete fallback for number/subtitle/group.
    """
    result: dict[str, dict[str, str | None]] = {}
    for select in doc.iter("select"):
        if select.get("id") != "chapter-select":
            continue
        for option in select.iter("option"):
            chapter_id = (option.get("value") or "").strip()
            if not chapter_id:
                continue
            text = option.text_content().strip()
            num_match = _LEADING_NUM_RE.match(text)
            parts = [part.strip() for part in text.split(" - ")]
            result[chapter_id] = {
                "number": num_match.group(1) if num_match is not None else None,
                "subtitle": (parts[1] if len(parts) >= 2 else None) or None,
                "group": (parts[2] if len(parts) >= 3 else None) or None,
            }
    return result


def _parse_recent(html: str) -> list[dict[str, Any]]:
    """Parse the homepage "Latest Chapters" cards → DIRECT release fields (defensive).

    Each card's ``/read/{bookId}/{chapterId}/1`` anchor carries BOTH ids; the
    enclosing card supplies the book title (``/book/{bookId}`` anchor text), the
    scanlation group (``/group/`` anchor), language, and date. Deduped by
    ``(bookId, chapterId)``; the route owns the authoritative newest-first sort.
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    seen: set[tuple[str, str]] = set()
    cards: list[dict[str, Any]] = []
    for anchor in doc.iter("a"):
        href = anchor.get("href") or ""
        match = _READ_HREF_RE.match(href)
        if match is None:
            continue
        book_id, chapter_id = match.group(1), match.group(2)
        key = (book_id, chapter_id)
        if key in seen:
            continue
        seen.add(key)
        num_match = _CHAPTER_NUMBER_RE.search(anchor.text_content())
        number = num_match.group(1) if num_match is not None else None
        title, group, language, date_raw = _recent_card_meta(anchor, book_id)
        cards.append(
            {
                "book_id": book_id,
                "chapter_id": chapter_id,
                "title": title,
                "number": number,
                "subtitle": None,
                "group": group,
                "language": language,
                "date_raw": date_raw,
            }
        )
    return cards


def _recent_card_meta(
    anchor: Any, book_id: str
) -> tuple[str | None, str | None, str | None, str | None]:
    """Walk up from a recent read-anchor to its card → ``(title, group, lang, date)``.

    Climbs at most 8 ancestors looking for one that contains the matching
    ``/book/{bookId}`` title anchor; from that card it reads the title, the
    ``/group/`` anchor, the language token, and a date. All best-effort — a missing
    field degrades to ``None`` (the caller defaults the title to ``"Unknown"``).
    """
    book_re = re.compile(rf"^/book/{re.escape(book_id)}(?:[/?#]|$)")
    node = anchor.getparent()
    levels = 0
    while node is not None and levels < 8:
        title: str | None = None
        group: str | None = None
        for inner in node.iter("a"):
            href = inner.get("href") or ""
            if book_re.match(href):
                text = inner.text_content().strip()
                if text and title is None:
                    title = text
            elif "/group/" in href and group is None:
                group = (inner.get("title") or inner.text_content() or "").strip()
                group = group or None
        if title is not None:
            node_text = node.text_content()
            return title, group, _detect_language(node_text), _extract_date(node_text)
        node = node.getparent()
        levels += 1
    return None, None, None, None


def _enclosing_row(anchor: Any) -> Any:
    """Return the nearest ancestor ``<tr>`` of ``anchor`` (or ``None``)."""
    node = anchor.getparent()
    levels = 0
    while node is not None and levels < 8:
        tag = node.tag
        if isinstance(tag, str) and tag.lower() == "tr":
            return node
        node = node.getparent()
        levels += 1
    return None


def _row_group(row: Any) -> str | None:
    """Extract the scanlation group name from a row's ``/group/`` anchor (advisory)."""
    for anchor in row.iter("a"):
        href = anchor.get("href") or ""
        if "/group/" in href:
            name = (anchor.get("title") or anchor.text_content() or "").strip()
            return name or None
    return None


def _detect_language(text: str | None) -> str | None:
    """Return ``"English"`` when the row/card text advertises it, else ``None``."""
    if text and "english" in text.lower():
        return "English"
    return None


def _extract_date(text: str | None) -> str | None:
    """Best-effort capture of an absolute date token from row/card text (else None)."""
    if not text:
        return None
    match = _DATE_RE.search(text)
    return match.group(1) if match is not None else None


def _extract_page1_url(html: str) -> str | None:
    """Extract page 1's image URL from the ``.strip-reader`` div (read page).

    The reader's page-1 ``<img>`` lives in ``<div class="strip-reader">`` and carries
    the absolute ``https://projectsuki.com/images/gallery/{bookId}/{hash}/001?{ts}``
    URL (yielding the per-chapter hash). Falls back to the first gallery ``<img>``
    anywhere on the page if the div shape shifts. Returns ``None`` when none is found.
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return None
    for div in doc.iter("div"):
        if "strip-reader" in (div.get("class") or "").split():
            for img in div.iter("img"):
                src = (img.get("src") or "").strip()
                if "/images/gallery/" in src:
                    return src
    srcs = _extract_gallery_img_srcs(html)
    return srcs[0] if srcs else None


def _extract_gallery_img_srcs(html: str) -> list[str]:
    """Extract every ``/images/gallery/...`` ``<img>`` ``src`` in document order.

    Used for both the read-page fallback and the ``/callpage`` ``src`` HTML fragment
    (the remaining pages). The URLs are taken VERBATIM from the markup — never
    reconstructed (CLAUDE.md SSRF); each is allowlisted downstream by
    :func:`_is_allowed_image_url`.
    """
    try:
        doc = lxml.html.fragment_fromstring(html, create_parent="div")
    except Exception:
        return []
    urls: list[str] = []
    for img in doc.iter("img"):
        src = (img.get("src") or "").strip()
        if "/images/gallery/" in src:
            urls.append(src)
    return urls


class ProjectSukiSource(Source):
    """ProjectSuki (projectsuki.com) — antibot none, HTML-scraped title fan-out.

    The first pure-HTML-scraped httpx source: ``GET`` HTML via ``ctx.get_bytes`` and
    parse with lxml (offloaded via ``asyncio.to_thread``), with a ``POST /callpage``
    page-walk for the manifest. Zero networking glue (module docstring).
    """

    key = "projectsuki"
    name = "ProjectSuki"
    base_url = "https://projectsuki.com"
    # Title-search only — no external metadata-id namespace to mint against (SRCH-07).
    id_types: list[str] = []
    # Single-language (English) aggregator — no per-chapter language metadata exists.
    languages = ["en"]
    # Probe-tuned (2026-06-20, scripts/probe_rate_limits.py): two proxied sweeps found
    # NO hard limit on projectsuki — zero 429/403/CF-challenge/Retry-After and zero
    # latency degradation on the search path. The rate-ceiling sweep sustained 960/min
    # cleanly at concurrency 8 — a FLOOR (the true ceiling is higher). 480 is the
    # conservative ~50%-of-floor suggestion, matching the mangaball/mangadot/atsumaru
    # precedent (#101). The limiter is SHARED across the search + /callpage manifest
    # calls; image bytes are exempt (``get_bytes`` is ``limited=False``).
    rate_limit_per_minute = 480
    # D-30 per-source override: the parallelism sweep sustained concurrency 32 cleanly
    # (zero throttling), so chapter downloads parallelize safely. 3 mirrors the
    # mangaball/mangadot/atsumaru precedent; the job manager clamps it to the global
    # max_concurrent_chapters. (Governs concurrent download JOBS, not search fan-out.)
    max_concurrent_jobs = 3
    antibot = "none"
    decrypt_scheme = None
    session_prep = None
    supports_search = True
    supports_recent = True

    # ─────────────────────────────── search ──────────────────────────────────

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-chapter Releases (SRCH-01..07, D-08).

        Two-call flow (mirror atsumaru): ``GET /search`` is TITLE-ONLY, so ``search``
        deep-enumerates the first ``_DEFAULT_TITLE_CANDIDATES`` (relevance-pruned)
        book candidates via a per-candidate ``GET /book/{id}`` chapter-list fetch. The
        fan-out runs CONCURRENTLY under a bounded ``asyncio.Semaphore`` acquired INSIDE
        the cached ``_enum_fn`` (a cache HIT takes no slot, CACHE-03). Each book's
        complete chapter list is filtered by :meth:`chapter_matches`, ordered
        NEWEST-FIRST, sliced to ``req.limit``, and ONLY THEN minted (mint-after-slice
        — a long title carries 200+ chapters and minting per row would evict the
        returned releases' handles from the store). ZERO networking glue.
        """
        if not self._language_wanted(req.languages):
            return []

        async def _resolve_fn() -> list[dict[str, str]]:
            query = req.query or ""
            url = f"{self.base_url}/search?{urlencode({'q': query})}"
            raw = await ctx.get_bytes(url, limited=True)
            html = raw.decode("utf-8", "replace")
            books = await asyncio.to_thread(_parse_search_results, html)
            return prune_candidates(
                books,
                query,
                keys=lambda book: [book.get("title")],
                cap=_DEFAULT_TITLE_CANDIDATES,
            )

        candidates: list[dict[str, str]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(req.query or ""), req.languages or []),
            _resolve_fn,
        )
        ctx.candidates_enumerated = len(candidates)
        per_candidate_limit = req.limit or 50
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(book_id: str, manga_title: str) -> list[Release]:
            async def _enum_fn() -> Enumeration:
                # The semaphore + the GET live INSIDE ``_enum_fn`` so a cache HIT
                # acquires neither the fan-out slot nor a rate-limit token (CACHE-03).
                async with sem:
                    raw = await ctx.get_bytes(
                        f"{self.base_url}/book/{book_id}", limited=True
                    )
                html = raw.decode("utf-8", "replace")
                rows = await asyncio.to_thread(_parse_book_chapters, html, book_id)
                return Enumeration(
                    items=rows,
                    chapter_numbers=tuple(
                        d
                        for c in rows
                        if isinstance(c, dict)
                        and (d := self._parse_decimal(c.get("number"))) is not None
                    ),
                    exhausted=True,  # the /book page is the COMPLETE feed
                    requested_limit=per_candidate_limit,
                )

            # The chapter list does not depend on ``languages`` (single-language site),
            # so key on ``[]`` — every request for one book shares the cached list.
            enum = await ctx.cached_enumerate(
                ctx.cached_enumerate_key(book_id, []), _enum_fn
            )
            return self._chapters_to_releases(
                enum.items, book_id, manga_title, per_candidate_limit, ctx, req
            )

        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for book in candidates:
            book_id = book.get("book_id")
            if not book_id:
                continue
            manga_title = str(book.get("title") or "Unknown")
            tasks.append(_fetch_candidate(str(book_id), manga_title))

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
        book_id: str,
        manga_title: str,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one book's chapter list → per-chapter Releases (mint-after-slice).

        Filtered by :meth:`chapter_matches`, ordered NEWEST-FIRST by chapter number
        (descending), sliced to ``limit``, THEN minted — handle count per candidate is
        bounded by ``limit`` so the returned releases' handles survive the store cap.
        """
        rows: list[tuple[Decimal, dict[str, Any]]] = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            if not chapter.get("chapter_id"):
                continue  # no resolve unit
            number = self._parse_decimal(chapter.get("number"))
            if not self.chapter_matches(req, number):
                continue
            # ``None`` numbers sort oldest (Decimal(-1)) so numbered chapters lead.
            rows.append((number if number is not None else Decimal(-1), chapter))
        rows.sort(key=lambda row: row[0], reverse=True)  # newest-first
        releases: list[Release] = []
        for _number, chapter in rows[:limit]:  # mint AFTER slice
            rel = self._to_release(book_id, manga_title, chapter, ctx)
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
        """Homepage "Latest Chapters" → DIRECT newest-chapter releases (RCNT-01/02).

        GETs the homepage HTML and parses the "Latest Chapters" cards; each card's
        ``/read`` anchor carries BOTH ids, so every release is minted DIRECT (no
        ``:DEFERRED`` late-bind). The route applies the authoritative newest-first sort
        + ``since`` cut, so the whole page is consumed here (source-side ``since`` left
        to the route, IN-01). Zero networking glue.
        """
        if not self._language_wanted(languages):
            return []
        raw = await ctx.get_bytes(f"{self.base_url}/", limited=True)
        html = raw.decode("utf-8", "replace")
        cards = await asyncio.to_thread(_parse_recent, html)
        releases: list[Release] = []
        for card in cards:
            book_id = card.get("book_id")
            chapter_id = card.get("chapter_id")
            if not book_id or not chapter_id:
                continue
            manga_title = str(card.get("title") or "Unknown")
            rel = self._to_release(str(book_id), manga_title, card, ctx)
            if rel is not None:
                releases.append(rel)
        return releases

    # ─────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a COMPOSITE ``{bookId}:{chapterId}`` → ordered page URLs (PKG-01).

        Both ``search`` and ``recent`` mint the composite ``{bookId}:{chapterId}`` as
        the ``ResolutionRecord.chapter_id`` (the manifest needs the bookId for both the
        read-page GET and the ``/callpage`` POST). The flow: GET the read page for page
        1 (+ the per-chapter hash), then page-walk ``POST /callpage`` while ``super``
        is truthy, extracting every ``/images/gallery/...`` ``<img>`` ``src`` in order.
        EVERY URL is SSRF-allowlisted before return (T-07-07); the extracted count is
        guarded against ``ctx.expected_pages`` when known (IN-03, mirror atsumaru).
        """
        book_id, _, real_chapter_id = chapter_id.partition(":")
        if not book_id or not real_chapter_id:
            raise SourceError(
                "source_unavailable",
                f"malformed projectsuki chapter id {chapter_id!r} "
                "(want bookId:chapterId)",
            )

        read_raw = await ctx.get_bytes(
            f"{self.base_url}/read/{book_id}/{real_chapter_id}/1", limited=True
        )
        read_html = read_raw.decode("utf-8", "replace")
        page1 = await asyncio.to_thread(_extract_page1_url, read_html)

        urls: list[str] = []
        if page1:
            urls.append(page1)

        current_chapter = real_chapter_id
        first = True
        for _ in range(_CALLPAGE_MAX_ITERATIONS):
            resp = await ctx.post_json_body(
                f"{self.base_url}/callpage",
                body={
                    "bookid": book_id,
                    "chapterid": current_chapter,
                    "first": first,
                },
            )
            src = resp.get("src")
            if isinstance(src, str) and src:
                extracted = await asyncio.to_thread(_extract_gallery_img_srcs, src)
                urls.extend(extracted)
            if not resp.get("super"):
                break
            next_chapter = resp.get("chapter_id")
            if not next_chapter:
                break
            current_chapter = str(next_chapter)
            first = False

        if not urls:
            raise SourceError(
                "source_unavailable",
                f"no page images found for chapter {chapter_id}",
            )
        for url in urls:
            if not _is_allowed_image_url(url):
                # Never fetch a non-allowlisted (off-host / off-shape) URL (T-07-07).
                raise SourceError(
                    "source_unavailable",
                    f"projectsuki image URL failed the SSRF allowlist: {url!r}",
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
        semaphore, NOT the per-source API limiter. Plain ``image/jpeg`` — no decrypt.
        A ``Referer: https://projectsuki.com/`` may be needed if the CDN hotlink-
        protects (live-tune; a bare GET returned 200 in recon).
        """
        return await ctx.get_bytes(url)

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        book_id: str,
        manga_title: str,
        chapter: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single chapter dict (D-08)."""
        chapter_id = chapter.get("chapter_id")
        if not chapter_id:
            return None
        chapter_id = str(chapter_id)
        chapter_number = self._parse_decimal(chapter.get("number"))
        group = chapter.get("group") or None
        publish_date = (
            _date_to_iso(chapter.get("date_raw")) or datetime.now(UTC).isoformat()
        )

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(manga_title, ch_str, group=group)
        guid = f"projectsuki:{book_id}:ch-{ch_str}:{chapter_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                # COMPOSITE: the manifest needs BOTH ids (see fetch_manifest).
                chapter_id=f"{book_id}:{chapter_id}",
                language="en",
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=None,
                scanlation_group=group,
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
            scanlation_group=group,
            page_count=None,
            ids={
                "projectsukiBookId": book_id,
                "projectsukiChapterId": chapter_id,
            },
        )

    @classmethod
    def _language_wanted(cls, languages: list[str] | None) -> bool:
        """True unless the caller asked for languages that exclude English.

        ProjectSuki is English-only, so a request constrained to other languages
        yields nothing (return ``[]``). An unset / English-inclusive filter passes.
        """
        return not languages or "en" in languages

    @staticmethod
    def _build_title(
        manga_title: str, chapter: str, *, group: str | None = None
    ) -> str:
        """Compose a MangaParser-parseable release title (REL-02), MangaDex shape.

        Appends a ``[group]`` segment when the scanlation group is known (mirror
        atsumaru/mangadex) so Mangarr can parse + match on the group.
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
