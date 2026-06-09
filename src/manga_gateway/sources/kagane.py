"""Kagane source — a ``cloudflare`` Komga-derived JSON-REST source (SRC-01).

Kagane (``https://kagane.to``) is a Next.js SPA whose backend is a **Komga-fork
JSON-REST API** on a SEPARATE host, ``https://yuzuki.kagane.to/api/v2/...``, with
page images served from a THIRD host (``https://kstatic.to``, the response-provided
``cache_url``). It adds **zero networking glue** — every outbound call is
``ctx.post_json_body`` / ``ctx.get_json`` / ``ctx.get_bytes`` (SRC-01/SRC-02); all
anti-bot/rate-limit/retry/clearance live in the framework.

Two-axis classification (recon 2026-06-09, see the quick-task RECON write-up):

* **Axis A (prep/antibot) = comix** — ``antibot = "cloudflare"`` +
  ``cloudflare_challenge_url = "https://kagane.to/"``. Only ONE call is actually
  Cloudflare-gated: ``POST https://kagane.to/api/integrity`` (the download-path token
  mint). ``yuzuki.kagane.to`` (data API) and ``kstatic.to`` (images) answer cold over
  plain httpx — but declaring the source ``cloudflare`` is correct: the framework
  injects the captured ``cf_clearance`` + bound UA on every call (harmless on the open
  hosts, REQUIRED on ``/api/integrity``). NO comix-style browser-DOM read: the cleared
  API is clean JSON over httpx, and the integrity token is a normal server route (not a
  JS-VM-minted token), so ``solver.fetch_via_browser`` is not used.
* **Axis B (search topology) = atsumaru** — title→chapter fan-out: ``search/series``
  returns TITLES; the COMPLETE chapter list lives in a separate per-series
  ``GET /api/v2/series/{id}`` call (``series_books``). ``recent`` is a title-only feed
  with the newest chapter EMBEDDED (``latest_chapters``) → DIRECT mint (no fan-out).

ENDPOINT SHAPES (live-recon-pinned, 2026-06-09):

* base (cf-gated): ``https://kagane.to`` — ``POST /api/integrity`` ``{}`` → ``{token}``.
* API (open): ``https://yuzuki.kagane.to``
  - search: ``POST /api/v2/search/series?page=0&size=N`` (no ``sort`` ⇒ relevance),
    JSON body ``{"title": <query>, "content_rating": ["Safe", "Suggestive"]}`` →
    ``{content:[{series_id, title, alternate_titles, current_books, translated_language,
    latest_chapters:[{book_id, chapter_no, created_at, ...}]}]}`` — TITLE-ONLY.
  - recent: same endpoint, ``?sort=updated_at,desc``, body ``{"content_rating":[...]}``;
    each row's ``latest_chapters[0]`` mints a DIRECT release (the book_id is present).
  - chapter list: ``GET /api/v2/series/{series_id}`` → ``{series_books:[{book_id,
    chapter_no, volume_no, page_count, groups:[{title}], became_visible_at, ...}],
    current_books}`` — the COMPLETE feed (verified 1182/1182 for One Piece, one call,
    no page/offset param).
  - book token + manifest: ``POST /api/v2/books/{book_id}?is_datasaver=false`` with
    the ``x-integrity-token`` header → ``{access_token, cache_url,
    manifest:{pages:[{page_id, ext, page_no, ...}]}}``.
* image (open): ``GET {cache_url}/api/v2/books/page/{book_id}/{page_id}.{ext}
  ?token={access_token}&is_datasaver=false`` → plaintext ``image/jpeg`` (the manifest
  declares ``ext=jxl`` but the CDN serves decoded JPEG; ``decrypt_scheme = None``).

guid (D-08): ``kagane:{series_id}:ch-{chapter_no}:{book_id}``. The
``ResolutionRecord.chapter_id`` is the bare ``book_id`` — ``fetch_manifest`` keys the
whole token flow on it alone (no composite needed).

Framework seam: ``search/series`` + ``integrity`` + book-token are JSON-body POSTs with
query params, and the book-token needs an ``x-integrity-token`` header — none of which
the form-encoded ``ctx.post_json`` supports. They ride the additive
``ctx.post_json_body(url, *, body, params, headers)`` seam (added with this source);
the source still adds ZERO networking glue.

Anti-bot caveat: the same managed challenge as comix gates ``kagane.to``; the solver
almost certainly must run HEADED (``GATEWAY_CLOUDFLARE_HEADLESS=false``) — confirmed
cleared headed in recon. A "captured clearance" that still yields ``upstream 403`` is
the stale-cookie false positive (headless-blocked).
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..framework.base import Source
from ..framework.enum_cache import Enumeration
from ..framework.errors import SourceError
from ..framework.relevance import _normalize, prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# The Komga-derived data API lives on a different host than the cf-gated front-end.
_API_BASE = "https://yuzuki.kagane.to"

# Search/recent request body: the keyword rides ``title`` (NOT ``keyword`` — that field
# is a no-op, falsified live). ``content_rating`` mirrors the site's own default so
# suggestive titles are not silently dropped (comix uses the same widening).
_CONTENT_RATING = ["Safe", "Suggestive"]
# search() page size pulled from the site's own relevance call (?size=99).
_SEARCH_PAGE_SIZE = 30

# search() deep-enumerates at most this many relevance-pruned title candidates (mirrors
# atsumaru/mangaball GAP-1). Each candidate is a full series-detail fan-out.
_DEFAULT_TITLE_CANDIDATES = 5
# Bounds the per-candidate series-detail fan-out in search() (mirrors atsumaru): at most
# this many chapter-list fetches run concurrently.
_CHAPTERS_FANOUT_CONCURRENCY = 6
# recent() reads one search/series page (newest-first) and mints DIRECT from each row's
# embedded ``latest_chapters`` — no per-title fan-out — so the only bound is the page
# size; cap it so a single recent poll stays cheap.
_RECENT_PAGE_SIZE = 50

# SSRF allowlist for the EXTRACTED page-image URLs (T-v7n-02, CLAUDE.md). The CDN host
# is the response-provided ``cache_url`` (``kstatic.to``, live); the path namespace is
# ``/api/v2/books/page/`` and the extension is an image one. Host + namespace +
# extension are the stable invariants — the per-page ``{page_id}`` filename is NOT
# over-pinned beyond the extension. ``*.kagane.to`` is allowed too in case ``cache_url``
# moves onto a kagane subdomain (live-tune item).
_KAGANE_IMAGE_HOSTS = frozenset({"kstatic.to", "www.kstatic.to"})
_KAGANE_IMG_PATH_RE = re.compile(
    r"^/api/v2/books/page/[A-Za-z0-9_./-]+\.(jxl|jpg|jpeg|png|webp|avif|gif)$",
    re.IGNORECASE,
)


def _iso_or_none(raw: Any) -> str | None:
    """Normalize a Kagane timestamp → RFC3339 ``date-time`` (or ``None``).

    Kagane rows carry already-RFC3339 strings (``2026-03-07T14:27:20.534756Z`` /
    ``…+00:00``); normalize defensively to an aware-UTC ISO string. Returns ``None`` for
    a missing/unparseable value so the caller can fall back to "now".
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` is a Kagane page-image URL (SSRF allowlist, T-v7n-02).

    Belt-and-suspenders defence on every extracted manifest URL before the framework
    fetches it: ``https`` + host ``kstatic.to`` (or any ``*.kagane.to``) + the
    ``/api/v2/books/page/`` namespace + an image extension. Rejects non-HTTPS, off-host,
    off-namespace, and traversal URLs. Validates the ``posixpath.normpath``-resolved
    path (httpx normalizes ``..`` first) and rejects any literal ``..`` segment.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    if not (host in _KAGANE_IMAGE_HOSTS or host.endswith(".kagane.to")):
        return False
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return bool(_KAGANE_IMG_PATH_RE.match(norm_path))


class KaganeSource(Source):
    """Kagane (kagane.to) — antibot cloudflare, Komga-fork JSON-REST, title fan-out.

    A clean-JSON ``cloudflare`` source: ``search/series`` is title-only so ``search``
    deep-enumerates each candidate's complete ``series_books`` feed (atsumaru fan-out
    shape), and the download path mints a per-book DRM token (integrity → book-token →
    page URL) entirely through ``ctx`` (zero networking glue; module docstring).
    """

    key = "kagane"
    name = "Kagane"
    base_url = "https://kagane.to"
    # Title-search only — Kagane exposes no external metadata-id namespace to mint on.
    id_types: list[str] = []
    # Predominantly English-translated aggregator; per-series ``translated_language``
    # gates the language filter (see :meth:`_language_wanted`).
    languages = ["en"]
    # PROBE-MEASURED (2026-06-09, scripts/probe_rate_limits.py --no-proxy, local IP):
    # the BINDING limit is the manifest token path — ``POST /api/integrity`` /
    # ``POST /api/v2/books/{id}`` HARD-block above ~30/min with ``Retry-After: 30``
    # (30 ok then 30 blocked at 60/min). The per-source limiter gates EVERY ``limited``
    # call (search/series GET + both token POSTs), so it must sit under that ceiling:
    # 24 = ~80% of the 30/min hard block, real headroom (the old conservative 30 sat
    # exactly AT the block). search is slow (~3-4s/call) but never 429s; image bytes
    # are ``get_bytes`` ``limited=False``-exempt (clean to 240/min, the probe cap).
    rate_limit_per_minute = 24
    # D-30: the token path's tight 30/min bucket is the bottleneck (each job mints 2
    # token calls). 2 keeps concurrent jobs comfortably under it (the limiter paces the
    # 4 token calls of 2 jobs across the budget); image fetch is exempt. The job manager
    # clamps this to the global max_concurrent_chapters (8).
    max_concurrent_jobs = 2
    antibot = "cloudflare"
    cloudflare_challenge_url = "https://kagane.to/"
    decrypt_scheme = None
    session_prep = None
    supports_search = True
    supports_recent = True

    # ─────────────────────────────── search ──────────────────────────────────

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-chapter Releases (SRCH-01..07, D-08).

        Two-call live flow (atsumaru fan-out): ``POST /api/v2/search/series`` is
        TITLE-ONLY, so ``search`` deep-enumerates the first
        ``_DEFAULT_TITLE_CANDIDATES`` (relevance-pruned) candidates via a per-series
        ``GET /api/v2/series/{id}``. Each candidate's COMPLETE ``series_books`` feed is
        filtered by :meth:`chapter_matches`, ordered newest-first, sliced to
        ``req.limit``, and ONLY THEN minted (GAP-2). ZERO glue — both calls are ``ctx``.
        """

        async def _resolve_fn() -> list[dict[str, Any]]:
            body = await ctx.post_json_body(
                f"{_API_BASE}/api/v2/search/series",
                body={"title": req.query or "", "content_rating": _CONTENT_RATING},
                params={"page": 0, "size": _SEARCH_PAGE_SIZE},
            )
            content = body.get("content")
            rows = [r for r in (content or []) if isinstance(r, dict)]
            return prune_candidates(
                rows,
                req.query or "",
                keys=lambda d: [d.get("title"), *_alternate_titles(d)],
                cap=_DEFAULT_TITLE_CANDIDATES,
            )

        # Layer 1 (D-01): cache the title→pruned-candidate resolution.
        candidates: list[dict[str, Any]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(req.query or ""), req.languages or []),
            _resolve_fn,
        )
        # Drop candidates whose translated language the caller excluded (cheap
        # pre-filter so a discarded language costs no series-detail fan-out call).
        candidates = [c for c in candidates if self._language_wanted(req.languages, c)]
        ctx.candidates_enumerated = len(candidates)
        per_candidate_limit = req.limit or 50
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(series: dict[str, Any]) -> list[Release]:
            series_id = str(series["series_id"])

            async def _enum_fn() -> Enumeration:
                # Semaphore + GET INSIDE the cached fn so a HIT takes no slot/token.
                async with sem:
                    detail = await ctx.get_json(
                        f"{_API_BASE}/api/v2/series/{series_id}"
                    )
                books = detail.get("series_books")
                rows = [
                    b
                    for b in (books if isinstance(books, list) else [])
                    if isinstance(b, dict)
                ]
                return Enumeration(
                    items=rows,
                    chapter_numbers=tuple(
                        d
                        for b in rows
                        if (d := self._parse_decimal(b.get("chapter_no"))) is not None
                    ),
                    exhausted=True,  # series_books is the COMPLETE feed
                    requested_limit=per_candidate_limit,
                )

            enum = await ctx.cached_enumerate(
                ctx.cached_enumerate_key(series_id, []), _enum_fn
            )
            return self._books_to_releases(
                enum.items, series, per_candidate_limit, ctx, req
            )

        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for series in candidates:
            if series.get("series_id"):
                tasks.append(_fetch_candidate(series))

        results = await asyncio.gather(*tasks)
        releases: list[Release] = []
        for chunk in results:
            releases.extend(chunk)
        return releases

    def _books_to_releases(
        self,
        books: list[Any],
        series: dict[str, Any],
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one series' ``series_books`` → per-chapter Releases (GAP-2).

        Filtered by :meth:`chapter_matches`, ordered NEWEST-FIRST by ``sort_no`` (the
        monotonic chapter ordinal), sliced to ``limit``, THEN minted — so the handle
        count per candidate is bounded by ``limit`` (store-cap safety).
        """
        rows: list[tuple[float, dict[str, Any]]] = []
        for book in books:
            if not isinstance(book, dict) or not book.get("book_id"):
                continue
            number = self._parse_decimal(book.get("chapter_no"))
            if not self.chapter_matches(req, number):
                continue
            rows.append((self._parse_float(book.get("sort_no")), book))
        rows.sort(key=lambda row: row[0], reverse=True)  # newest-first
        releases: list[Release] = []
        for _sort, book in rows[:limit]:  # mint AFTER slice (GAP-2)
            rel = self._to_release(series, book, ctx)
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
        """Newest-updated series → DIRECT newest-chapter releases (RCNT-01/02).

        ONE call: ``POST /api/v2/search/series?sort=updated_at,desc`` returns
        newest-first series rows, each EMBEDDING ``latest_chapters`` (the newest chapter
        carries a stable ``book_id``), so recent mints a DIRECT release per row with no
        per-series fan-out (``1`` call total). The route applies the authoritative
        newest-first sort + ``since`` cut (IN-01). Zero glue (SRC-02).
        """
        body = await ctx.post_json_body(
            f"{_API_BASE}/api/v2/search/series",
            body={"content_rating": _CONTENT_RATING},
            params={"page": 0, "size": _RECENT_PAGE_SIZE, "sort": "updated_at,desc"},
        )
        content = body.get("content")
        rows = [r for r in (content or []) if isinstance(r, dict)]
        releases: list[Release] = []
        for series in rows:
            if not self._language_wanted(languages, series):
                continue
            latest = series.get("latest_chapters")
            newest = next(
                (
                    c
                    for c in (latest if isinstance(latest, list) else [])
                    if isinstance(c, dict) and c.get("book_id")
                ),
                None,
            )
            if newest is None:
                continue
            rel = self._to_release(series, newest, ctx)
            if rel is not None:
                releases.append(rel)
        return releases

    # ─────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a ``book_id`` → ordered page URLs via the token flow (PKG-01).

        Three ``ctx`` calls (zero glue): (1) mint an integrity token on the cf-gated
        ``kagane.to/api/integrity``; (2) POST it as ``x-integrity-token`` to the open
        ``yuzuki`` book-token endpoint → ``{access_token, cache_url, manifest.pages}``;
        (3) EXTRACT each page URL by joining the server-provided ``cache_url`` (never a
        reconstructed host) + the page path + the ``access_token`` query. Every URL is
        SSRF-allowlisted (T-v7n-02). The extracted count is guarded against the
        chapter's declared ``page_count`` when known (``ctx.expected_pages``).
        """
        book_id = chapter_id
        if not book_id:
            raise SourceError("source_unavailable", "empty kagane chapter id")

        # 1. integrity token (cf_clearance is injected by the framework for kagane.to).
        integ = await ctx.post_json_body(f"{self.base_url}/api/integrity", body={})
        token = integ.get("token")
        if not isinstance(token, str) or not token:
            raise SourceError(
                "source_unavailable", "kagane integrity mint returned no token"
            )

        # 2. book token + page manifest (open host, integrity-header gated).
        book = await ctx.post_json_body(
            f"{_API_BASE}/api/v2/books/{book_id}",
            body={},
            params={"is_datasaver": "false"},
            headers={"x-integrity-token": token},
        )
        access_token = book.get("access_token")
        cache_url = book.get("cache_url")
        manifest = book.get("manifest")
        pages = manifest.get("pages") if isinstance(manifest, dict) else None
        if not (isinstance(access_token, str) and access_token):
            raise SourceError(
                "source_unavailable", f"no access_token for book {book_id}"
            )
        if not (isinstance(cache_url, str) and cache_url):
            raise SourceError("source_unavailable", f"no cache_url for book {book_id}")
        if not isinstance(pages, list) or not pages:
            raise SourceError("source_unavailable", f"no pages for book {book_id}")

        # 3. EXTRACT + order + allowlist the page URLs.
        ordered: list[dict[str, Any]] = sorted(
            (p for p in pages if isinstance(p, dict)),
            key=lambda p: self._parse_float(p.get("page_no")),
        )
        urls: list[str] = []
        base = cache_url.rstrip("/")
        for page in ordered:
            page_id = page.get("page_id")
            ext = page.get("ext")
            if not (
                isinstance(page_id, str) and page_id and isinstance(ext, str) and ext
            ):
                raise SourceError(
                    "source_unavailable",
                    f"book {book_id} page missing page_id/ext",
                )
            url = (
                f"{base}/api/v2/books/page/{book_id}/{page_id}.{ext}"
                f"?token={access_token}&is_datasaver=false"
            )
            if not _is_allowed_image_url(url):
                raise SourceError(
                    "source_unavailable",
                    f"page URL failed the SSRF allowlist: "
                    f"{base}/api/v2/books/page/{book_id}/{page_id}.{ext}",
                )
            urls.append(url)
        if ctx.expected_pages is not None and len(urls) != ctx.expected_pages:
            raise SourceError(
                "source_unavailable",
                f"manifest integrity: extracted {len(urls)} pages, "
                f"chapter declares {ctx.expected_pages}",
            )
        return urls

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes (PKG-02).

        A one-line delegate: the ``?token=`` query is the gate, the CDN serves plaintext
        ``image/jpeg`` (no decrypt). Bounded by the per-job semaphore, not the API
        limiter (``get_bytes`` is ``limited=False``).
        """
        return await ctx.get_bytes(url)

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        series: dict[str, Any],
        book: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a series row + a book/chapter row (D-08).

        ``book`` is a ``series_books`` row (search — has ``page_count``/``groups``) OR a
        ``latest_chapters`` row (recent — no page_count/groups); both carry ``book_id``,
        ``chapter_no``, ``volume_no``.
        """
        book_id = book.get("book_id")
        if not book_id:
            return None
        book_id = str(book_id)
        series_id = str(series.get("series_id"))
        manga_title = str(series.get("title") or "Unknown")
        language = str(series.get("translated_language") or "en")
        chapter_number = self._parse_decimal(book.get("chapter_no"))
        page_count = self._parse_int(book.get("page_count"))
        group = _first_group(book)
        publish_date = (
            _iso_or_none(book.get("became_visible_at"))
            or _iso_or_none(book.get("available_at"))
            or _iso_or_none(book.get("created_at"))
            or datetime.now(UTC).isoformat()
        )

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(manga_title, ch_str, language, group=group)
        guid = f"kagane:{series_id}:ch-{ch_str}:{book_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                # fetch_manifest keys the whole token flow on the bare book_id.
                chapter_id=book_id,
                language=language,
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=self._parse_int(book.get("volume_no")),
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
            language=language,
            scanlation_group=group,
            page_count=page_count,
            ids={"kaganeSeriesId": series_id, "kaganeBookId": book_id},
        )

    @classmethod
    def _language_wanted(
        cls, languages: list[str] | None, series: dict[str, Any]
    ) -> bool:
        """True unless the caller's language filter excludes this series' language.

        An unset filter passes through. Otherwise the series' ``translated_language``
        (default ``en``) must be among the requested languages.
        """
        if not languages:
            return True
        lang = str(series.get("translated_language") or "en")
        return lang in languages

    @staticmethod
    def _build_title(
        manga_title: str, chapter: str, language: str, *, group: str | None = None
    ) -> str:
        """Compose a MangaParser-parseable release title (REL-02), MangaDex shape."""
        parts = [manga_title, "-", f"Chapter {chapter}", f"({language})"]
        if group:
            parts.append(f"[{group}]")
        return " ".join(parts)

    @staticmethod
    def _parse_decimal(raw: Any) -> Decimal | None:
        """Parse a chapter/volume number to Decimal (SRCH-06 — preserves ``1.5``)."""
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

    @staticmethod
    def _parse_float(raw: Any) -> float:
        """Parse a sort/page ordinal to float; unparseable → 0.0 (stable ordering)."""
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0


def _alternate_titles(series: dict[str, Any]) -> list[str]:
    """Extract a series row's ``alternate_titles`` as a flat list of strings (#139)."""
    raw = series.get("alternate_titles")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, str) and s.strip()]
    return []


def _first_group(book: dict[str, Any]) -> str | None:
    """First scanlation group NAME from a book row's ``groups`` (advisory, REL-03)."""
    groups = book.get("groups")
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict) and (name := g.get("title")):
            return str(name)
    return None
