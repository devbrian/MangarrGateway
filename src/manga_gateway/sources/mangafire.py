"""MangaFire source — an atsumaru-class title→chapter fan-out over a plain JSON API.

MangaFire (``https://mangafire.to``) rewrote its whole frontend (260706-hgu): it
dropped the PHP/jQuery server-rendered HTML + AJAX surface (the ``/filter`` HTML, the
``/ajax/manga/{slug}/chapter/{lang}`` HTML-in-JSON, the build-hash-tied ``vrf`` token,
and the per-image piece-shuffle descramble) for a React SPA backed by a **plain,
unsigned ``GET /api/*`` JSON REST API**. There is no token, header, or query signature —
every outbound call is a bare ``ctx.get_json`` / ``ctx.get_bytes`` (SRC-01/SRC-02), so
the source needs **no browser and no vrf**.

Structurally it is now a MangaDex/atsumaru-class source: a title-only search that
deep-enumerates each candidate's chapter list, DIRECT newest-chapter mint on recent, and
a manifest that reads server-minted page URLs verbatim. The four ``Source`` hooks:

* **search** — ``GET /api/titles?keyword=&limit=&page=`` → ``{items:[{id,hid,slug,title,
  …}],meta}`` (TITLE-ONLY) → prune candidates → per-candidate chapter-list fan-out →
  ``chapter_matches`` filter → slice → GAP-2 mint-after-slice.
* **recent** — ``GET /api/titles?order[chapter_updated_at]=desc&limit=`` (the bracket
  param is required; built via ``urlencode``) → per-title newest-chapter DIRECT mint.
* **chapter list** — ``GET /api/titles/{hid}/chapters?language=&limit=200&page=`` →
  ``{items:[{id,number,name,createdAt}],meta:{lastPage}}``. Max ``limit`` is 200, so a
  long series PAGINATES page=1..meta.lastPage (the extra pages fanned out concurrently)
  for the COMPLETE list (source-onboarding completeness rule). The chapter numeric
  ``id`` is the resolve unit; ``createdAt`` is a unix-epoch (seconds).
* **fetch_manifest** — ``GET /api/chapters/{chapterId}`` → ``{data:{pages:[{url,width,
  height}]}}`` — direct ``mfcdnN.xyz`` CDN URLs, NO scramble offset. Each URL is
  SSRF-allowlisted (SEC-01) and returned clean (no URL fragment).
* **fetch_image** — ``ctx.get_bytes`` of the clean URL with adaptive CDN zone-retry
  (the ``mfcdnN`` WAF-block self-heal); NO descramble anymore.

The image CDN family (``{prefix}.mfcdn{N}.xyz``) is UNCHANGED and still IP-bans direct
datacenter egress, so the SSRF allowlist, the CDN zone-retry, and ``image_fetch_via_
proxy_pool`` all stay (see below).

SSRF (SEC-01): the page-image CDN host VARIES per content (``o48.mfcdn1.xyz``, …) and
is NEVER pinned. ``_is_allowed_image_url`` enforces only the stable invariants
(``https`` + the ``mfcdnN.xyz`` CDN-zone host shape + ``/mf/`` namespace + image
extension + no traversal), stripping any URL fragment BEFORE the path is matched so a
malicious ``#`` fragment cannot smuggle a path past the regex.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urldefrag, urlencode, urlparse

from ..framework.base import Source
from ..framework.enum_cache import Enumeration
from ..framework.errors import SourceError
from ..framework.relevance import _normalize, prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# ── SSRF allowlist (host-agnostic; copy the mangaball/weebcentral shape, SEC-01) ──
# The page-image host is NEVER pinned (it varies per content), so the meaningful
# invariants carry the trust: https + public-host shape + /mf/ namespace + image
# extension + no traversal. The internal/metadata suffixes are rejected explicitly
# (the broad host regex would otherwise accept ``metadata.google.internal``).
_MANGAFIRE_IMG_PATH_RE = re.compile(
    r"^/mf/[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
# MangaFire page images are ALWAYS served from a ``{prefix}.mfcdn{N}.xyz`` CDN zone
# (prefix ∈ nw8/l1n/k99/m3z/o48; N ∈ _MANGAFIRE_CDN_ZONES). Pin the SSRF allowlist to
# exactly that zone family (WR-02): a compromised/hostile manifest URL pointing at an
# internal address or an attacker host no longer passes the public-host shape — it is a
# hard validation failure. If MangaFire ever adds a second CDN domain family it will
# surface here as an immediately-visible rejection rather than silently fetching any
# host. (The JSON API host — base_url / mangafire.to — is never routed through this
# image allowlist; only the resolved page-image CDN URLs are.)
_MANGAFIRE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.mfcdn\d+\.xyz$", re.IGNORECASE)
_MANGAFIRE_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".localhost")

# ── adaptive CDN zone-retry (debug mangafire-stale-manifest-reresolve-budget) ───
# MangaFire serves a chapter's pages from ONE of a small rotating set of CDN zones,
# each host shaped ``{prefix}.mfcdn{N}.xyz`` (prefix ∈ nw8/l1n/k99/m3z/o48; N ∈ the
# set below). Cloudflare's WAF on SOME zones hard-blocks (403) the gateway's egress
# IP while OTHERS allow it; the block is egress-IP-dependent, so we do NOT pin a
# single "good" zone — on a per-page 403 we retry the IDENTICAL page path with the
# host's ``mfcdnN`` rewritten to each OTHER known zone until one returns bytes (the
# live cross-test proved the exact path returns 200 on an un-blocked zone), then
# remember the winning zone for the rest of the job. Extend this tuple if MangaFire
# adds a zone — no other code change needed.
_MANGAFIRE_CDN_ZONES: tuple[int, ...] = (1, 2, 3)
# Matches the ``mfcdnN`` label inside a MangaFire image host (``o48.mfcdn1.xyz``) so
# the zone digit can be rewritten while leaving the subdomain prefix + ``.xyz`` TLD
# (and the whole path) byte-for-byte intact.
_MANGAFIRE_ZONE_RE = re.compile(r"\.mfcdn(\d+)\.", re.IGNORECASE)
# Per-job sidecar attr stashed on the SourceContext holding the CDN zone that last
# answered 200 for THIS job, so subsequent pages try it FIRST and never re-probe the
# blocked zone every page. The context is per-job (engine._build_context), so this
# never cross-contaminates concurrent jobs — unlike source-instance state (the source
# object is a shared registry singleton).
_PREFERRED_ZONE_ATTR = "_mangafire_preferred_cdn_zone"

# ── control-flow constants (mirror atsumaru/weebcentral) ───────────────────────
# search() requests at most this many title results from /api/titles before pruning.
_SEARCH_RESULT_LIMIT = 30
# search() deep-enumerates at most this many title candidates (GAP-1 lock).
_DEFAULT_TITLE_CANDIDATES = 5
# Bounds the per-candidate chapter-list fan-out (search + recent) AND the per-title
# chapter-list PAGE fan-out (the >200-chapter pagination). The unlimited get_json
# chapter-list path is bound by this semaphore, not the rate limiter — probe-tuned.
_CHAPTERS_FANOUT_CONCURRENCY = 6
# recent() bounds the per-title fan-out so a poll never balloons into dozens of GETs.
_RECENT_TITLE_CAP = 20
# Default chapter language when the request carries no language filter.
_DEFAULT_LANGUAGE = "en"
# Max page size the chapters endpoint accepts (limit=300 → HTTP 422). A long series
# has meta.lastPage > 1, so the COMPLETE list paginates page=1..lastPage.
_CHAPTER_PAGE_LIMIT = 200


def _iso_from_epoch(raw: Any) -> str | None:
    """Convert a MangaFire ``createdAt`` unix-epoch (SECONDS) → RFC3339 ``date-time``.

    The new JSON API renders chapter ``createdAt`` as an integer unix epoch in
    SECONDS (e.g. ``1757308339``), replacing the old ``MMM dd, yyyy`` string. Returns
    the aware-UTC ISO string, or ``None`` for a missing/unparseable/out-of-range value
    so the caller can fall back to ``now(UTC)`` (the contract's ``publishDate`` is
    required RFC3339).
    """
    if raw is None or raw == "":
        return None
    try:
        secs = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(secs, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` is a well-formed MangaFire ``/mf/`` page image (SSRF, SEC-01).

    Belt-and-suspenders defence on every manifest URL before the framework fetches it.
    The CDN host VARIES per content so it is NEVER pinned — the trust comes from
    ``https`` + the ``mfcdnN.xyz`` CDN-zone host shape + the ``/mf/`` namespace + an
    image extension + no traversal. Any URL fragment (a hostile ``#`` smuggle) is
    stripped BEFORE the path is parsed, so a fragment can never sneak a bad path past
    the regex. We reject internal/metadata host namespaces and validate the
    ``posixpath.normpath``-resolved path (httpx normalizes ``..`` before fetching).
    """
    clean, _frag = urldefrag(url)
    parsed = urlparse(clean)
    host = (parsed.hostname or "").lower()
    if host.endswith(_MANGAFIRE_INTERNAL_HOST_SUFFIXES):
        return False
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return (
        parsed.scheme == "https"
        and bool(_MANGAFIRE_HOST_RE.match(host))
        and bool(_MANGAFIRE_IMG_PATH_RE.match(norm_path))
    )


def _zone_of(url: str) -> int | None:
    """The ``mfcdnN`` CDN zone number of ``url``'s host (``None`` if not a zoned host).

    Reads the ``.mfcdn{N}.`` label out of the host (``o48.mfcdn1.xyz`` → ``1``). A URL
    whose host carries no ``mfcdn`` label (a non-MangaFire-CDN host) returns ``None`` so
    the zone-retry path can no-op safely on it.
    """
    host = (urlparse(url).hostname or "").lower()
    m = _MANGAFIRE_ZONE_RE.search(host)
    return int(m.group(1)) if m else None


def _rewrite_zone(url: str, zone: int) -> str:
    """Return ``url`` with its host's ``mfcdnN`` zone label rewritten to zone ``zone``.

    Only the zone digit changes — the subdomain prefix, the ``.xyz`` TLD, and the
    entire path + query stay byte-for-byte identical (the live cross-test proved the
    exact path returns 200 on an un-blocked zone). A URL with no ``mfcdn`` label is
    returned unchanged. Operates on the netloc only so a path segment that happened to
    contain ``mfcdnN`` could never be rewritten.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    new_host = _MANGAFIRE_ZONE_RE.sub(f".mfcdn{zone}.", host, count=1)
    if new_host == host:
        return url
    netloc = new_host
    if parsed.port is not None:
        netloc = f"{new_host}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


class MangaFireSource(Source):
    """MangaFire (mangafire.to) — plain JSON API, title→chapter fan-out (260706-hgu).

    A MangaDex/atsumaru-class source since the site's React-SPA rewrite: a title-only
    ``/api/titles`` search deep-enumerates each candidate's
    ``/api/titles/{hid}/chapters`` feed (paginating past the 200-cap), recent the newest
    chapter DIRECT, and ``/api/chapters/{id}`` yields server-minted CDN page URLs. Zero
    networking glue — every outbound call is ``ctx.get_json`` / ``ctx.get_bytes``
    (SRC-01/SRC-02). See the module docstring for the per-hook map.
    """

    key = "mangafire"
    name = "MangaFire"
    base_url = "https://mangafire.to"
    # Title-search only — MangaFire exposes no external metadata-id namespace (SRCH-07).
    id_types: list[str] = []
    # Recon-observed language set (the /api/titles/{hid}/chapters language filter).
    languages = ["en", "fr", "es", "es-la", "pt-br", "ja"]
    # Probe-measured (2026-06-10, scripts/probe_rate_limits.py, residential proxies) —
    # D-14. The mangafire.to API host enforces a real HTTP-429 ceiling: clean at
    # 120/min, 429 onset at 300/min. 100 is ~83% of the clean ceiling, governing limited
    # get_json search/chapter-list path. The image CDN (separate host) had NO limit
    # (clean to 960/min at concurrency 8); the get_bytes image path is exempt from this
    # limiter and bounded by max_concurrent_jobs.
    rate_limit_per_minute = 100
    # D-30 per-source override — bounds concurrent download JOBS only (search/recent use
    # the separate _CHAPTERS_FANOUT_CONCURRENCY semaphore). 3 is the D-14 probe-measured
    # safe value; the manifest is a single cheap httpx GET (no reader nav), so there is
    # no per-job contention to serialize.
    max_concurrent_jobs = 3
    # D-05: the JSON API answers cold over plain httpx (search/recent/chapters/read need
    # no clearance), but declare cloudflare anyway so the framework keeps a lazy-solved
    # clearance and degrades gracefully on a datacenter host that trips a managed
    # challenge. Deferred-solve (cloudflare_challenge_optional): cold requests go over
    # httpx first, and a real challenge (is_cf_challenge) → retried with force_resolve.
    antibot = "cloudflare"
    cloudflare_challenge_optional = True
    cloudflare_challenge_url = "https://mangafire.to/"
    # No response-byte decrypt seam and no session bootstrap — the API is plain JSON.
    decrypt_scheme = None
    session_prep = None
    supports_search = True
    supports_recent = True
    # Opt OUT of the engine's 403→stale-baseUrl re-resolve recovery (debug
    # ``mangafire-stale-manifest-reresolve-budget``). A MangaFire image 403 is a
    # Cloudflare WAF deny of the gateway's egress IP on a particular CDN zone
    # (``mfcdnN``), NOT a stale/expired baseUrl: re-resolving re-fetches the SAME pages
    # on the SAME blocked zone, so the engine's re-resolve is provably useless. We
    # instead self-heal INSIDE ``fetch_image`` by retrying the identical page path
    # across the OTHER CDN zones; a 403 only escapes ``fetch_image`` when ALL zones are
    # blocked, where re-resolve cannot help anyway — so failing fast is correct.
    reresolve_manifest_on_403 = False
    # 260620-4im opt-in: route this source's ``fetch_image`` byte fetches through the
    # framework residential proxy pool. MangaFire's image CDN zones IP-ban the gateway's
    # DIRECT egress (verified Cloudflare Error-1020 403 on all ``mfcdnN`` zones); ban
    # is IP-based, not fingerprint-based. A clean residential proxy returns ``200
    # image/jpeg``. LAYERING (locked): the proxy is the OUTER, per-job-sticky dimension
    # (framework-owned — one sticky proxy spans the WHOLE ``mfcdnN`` zone-retry below);
    # the zone-rewrite stays MangaFire-specific, INNER dimension (per-page, same proxy).
    # The framework rotates the proxy ONLY when the entire zone-retry fails. Search is
    # never given a pool (unchanged).
    image_fetch_via_proxy_pool = True

    # ─────────────────────────────── search ──────────────────────────────────

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-chapter Releases (SRCH-01..07).

        Two-call flow (mirrors atsumaru's GAP-1 lock): ``GET /api/titles?keyword=`` is
        TITLE-ONLY, so ``search`` deep-enumerates the first
        ``_DEFAULT_TITLE_CANDIDATES`` (relevance-pruned) candidates via a chapter fanout
        (paginating past the 200-cap for the COMPLETE feed). For each candidate the feed
        is filtered by :meth:`chapter_matches`, kept NEWEST-FIRST (the API returns
        number desc), sliced to ``req.limit``, and ONLY THEN minted (GAP-2 — a
        long title would otherwise evict the returned releases' own handles). Zero
        networking glue (SRC-01/02).
        """
        query = req.query or ""
        if not query:
            return []
        lang = self._filter_language(req.languages)
        per_candidate_limit = req.limit or 50

        async def _resolve_fn() -> list[dict[str, Any]]:
            titles = await self._search_titles(query, _SEARCH_RESULT_LIMIT, ctx)
            return prune_candidates(
                titles,
                query,
                keys=lambda d: [d.get("title")],
                cap=_DEFAULT_TITLE_CANDIDATES,
            )

        # Layer 1 (D-01): cache the title→pruned-candidate resolution so a repeat
        # chapter search on the same (query, languages) skips the /api/titles fetch.
        candidates: list[dict[str, Any]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(query), req.languages or []),
            _resolve_fn,
        )
        ctx.candidates_enumerated = len(candidates)
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(
            hid: str, title_id: str, manga_title: str
        ) -> list[Release]:
            # Layer 2 (CACHE-02/03): cache the UNFILTERED per-candidate chapter list.
            # The semaphore + the GET live INSIDE ``_enum_fn`` so a cache HIT acquires
            # no fan-out slot.
            async def _enum_fn() -> Enumeration:
                async with sem:
                    rows = await self._chapter_list(hid, lang, ctx)
                return Enumeration(
                    items=rows,
                    chapter_numbers=tuple(
                        d
                        for c in rows
                        if isinstance(c, dict)
                        and (d := self._parse_decimal(c.get("number"))) is not None
                    ),
                    exhausted=True,  # the chapter-list endpoint is the COMPLETE feed
                    requested_limit=per_candidate_limit,
                )

            # ``_chapter_list`` fetches the ``language=``-scoped feed, so the Layer-2
            # key MUST be namespaced by ``lang`` — keying on ``hid`` alone lets one
            # language's warmed rows leak into a later search for another language.
            enum = await ctx.cached_enumerate(
                ctx.cached_enumerate_key(hid, [lang]),
                _enum_fn,
            )
            return self._chapters_to_releases(
                enum.items,
                hid,
                title_id,
                manga_title,
                lang,
                per_candidate_limit,
                ctx,
                req,
            )

        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for title in candidates:
            hid = title.get("hid")
            if not hid:
                continue
            title_id = str(title.get("id") or "")
            manga_title = str(title.get("title") or "Unknown")
            tasks.append(_fetch_candidate(str(hid), title_id, manga_title))

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
        hid: str,
        title_id: str,
        manga_title: str,
        lang: str,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one candidate's chapter list → per-chapter Releases (GAP-2).

        Filtered by :meth:`chapter_matches`, kept NEWEST-FIRST (the API returns number
        desc), sliced to ``limit``, THEN minted — handle count per candidate is bounded
        by ``limit`` so the returned releases' handles always survive the store cap.
        """
        rows: list[dict[str, Any]] = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            if chapter.get("id") is None:
                continue  # no resolve unit
            number = self._parse_decimal(chapter.get("number"))
            if not self.chapter_matches(req, number):
                continue
            rows.append(chapter)
        releases: list[Release] = []
        for chapter in rows[:limit]:  # mint AFTER slice (newest-first preserved)
            rel = self._to_release(hid, title_id, manga_title, lang, chapter, ctx)
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

        GETs ``/api/titles?order[chapter_updated_at]=desc&limit=`` (the bracket param is
        required — a plain ``sort=`` is silently ignored — so the URL is built via
        ``urlencode``), then fans out the per-title chapter list under a bounded
        semaphore and mints the NEWEST chapter DIRECT (the chapter id is always present,
        no ``:DEFERRED``). The route applies the authoritative newest-first sort +
        ``since`` cut; the source-side ``since`` is left to the route (IN-01).
        """
        lang = self._filter_language(languages)
        bound = min(_RECENT_TITLE_CAP, limit or _RECENT_TITLE_CAP)
        titles = await self._recent_titles(bound, ctx)
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _newest_release(
            hid: str, title_id: str, manga_title: str
        ) -> Release | None:
            async with sem:
                rows = await self._chapter_list(hid, lang, ctx)
            newest = next(
                (r for r in rows if isinstance(r, dict) and r.get("id") is not None),
                None,
            )
            if newest is None:
                return None
            return self._to_release(hid, title_id, manga_title, lang, newest, ctx)

        tasks: list[Coroutine[Any, Any, Release | None]] = []
        for title in titles[:bound]:
            hid = title.get("hid")
            if not hid:
                continue
            title_id = str(title.get("id") or "")
            manga_title = str(title.get("title") or "Unknown")
            tasks.append(_newest_release(str(hid), title_id, manga_title))

        results = await asyncio.gather(*tasks)
        return [rel for rel in results if rel is not None]

    # ─────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered SSRF-allowlisted page URLs (PKG-01/R6).

        ``chapter_id`` is the numeric chapter id (the ``/api/chapters/{id}`` key) minted
        by search/recent. GETs ``/api/chapters/{id}`` → ``{data:{pages:[{url,width,
        height}]}}``, reads the per-page CDN URLs VERBATIM (array order = page order),
        strips any fragment, and SSRF-allowlists each (a non-allowlisted URL raises
        ``source_unavailable`` pre-fetch — SEC-01). The new API carries NO scramble
        offset, so URLs are returned CLEAN (no URL fragment). The page count is
        guarded against ``ctx.expected_pages`` when known.
        """
        pages = await self._chapter_pages(chapter_id, ctx)
        urls: list[str] = []
        for page in pages:
            raw_url = page.get("url") if isinstance(page, dict) else None
            if not isinstance(raw_url, str) or not raw_url:
                raise SourceError(
                    "source_unavailable",
                    f"mangafire page missing url for chapter {chapter_id}",
                )
            clean, _frag = urldefrag(raw_url)
            if not _is_allowed_image_url(clean):
                # Never fetch a non-allowlisted (off-host / off-shape) URL (SEC-01).
                raise SourceError(
                    "source_unavailable",
                    f"image URL failed the SSRF allowlist: {clean!r}",
                )
            urls.append(clean)
        if ctx.expected_pages is not None and len(urls) != ctx.expected_pages:
            raise SourceError(
                "source_unavailable",
                f"manifest integrity: extracted {len(urls)} images, "
                f"chapter declares {ctx.expected_pages} pages",
            )
        return urls

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's bytes via the shared session (PKG-02).

        Strips any URL fragment and fetches the CLEAN URL via ``ctx.get_bytes`` (NEVER
        the browser — CLAUDE.md) with adaptive CDN zone-retry (see
        :meth:`_get_bytes_zone_retry` / debug ``mangafire-stale-manifest-reresolve-
        budget``). The new JSON API carries NO scramble offset, so there is nothing to
        descramble — the fetched bytes are returned as-is (PKG-04, never recompressed).

        PROXY LAYERING (260620-4im): this source opts into the framework residential
        proxy pool (``image_fetch_via_proxy_pool``). The engine invokes ``fetch_image``
        via ``ctx.fetch_image_via_pool``, which pins ONE per-job sticky proxy as the
        OUTER egress dimension — so every inner ``ctx.get_bytes`` here, including the
        ``mfcdnN`` zone-retry below, egresses through that SAME proxy. This method needs
        NO pool-aware code — the routing is transparent at the transport layer.
        """
        clean, _frag = urldefrag(url)
        return await self._get_bytes_zone_retry(clean, ctx)

    async def _get_bytes_zone_retry(self, clean_url: str, ctx: SourceContext) -> bytes:
        """``ctx.get_bytes`` the clean URL, retrying across CDN zones on a 403.

        MangaFire's per-page 403 is a Cloudflare WAF deny of the gateway's egress IP on
        a particular ``mfcdnN`` CDN zone, not a stale/expired URL (debug
        ``mangafire-stale-manifest-reresolve-budget``). The IDENTICAL page path returns
        200 on an un-blocked zone, so on a 403 we re-fetch the same path with the host's
        zone label rewritten to each OTHER known zone (:data:`_MANGAFIRE_CDN_ZONES`)
        until one answers; the winning zone is remembered on the per-job context
        (:data:`_PREFERRED_ZONE_ATTR`) so the rest of this job tries it FIRST and never
        re-probes the blocked zone every page.

        Every rewritten URL is re-validated through :func:`_is_allowed_image_url` (SSRF,
        SEC-01) before it is fetched — a rewrite that somehow failed the allowlist is
        skipped, never fetched blindly. A non-403 ``SourceError`` (a genuine page loss)
        propagates unchanged. When EVERY zone 403s the request raises a clear terminal
        ``source_unavailable`` naming the blocked host.

        PROXY LAYERING (260620-4im): the zone-retry is the INNER dimension. The
        framework proxy pool holds ONE sticky proxy for this whole call (OUTER), so all
        ``ctx.get_bytes`` candidate below egresses through the SAME proxy; the framework
        rotates to a DIFFERENT proxy only when this entire zone-retry raises.
        """
        # Build the zone-attempt order: the job's remembered-good zone first (if any),
        # then the current URL's zone, then every other known zone — de-duplicated,
        # order-preserving. A URL with no ``mfcdn`` label (current_zone is None) just
        # gets a single plain fetch with no rewrite.
        current_zone = _zone_of(clean_url)
        if current_zone is None:
            return await ctx.get_bytes(clean_url)

        preferred = getattr(ctx, _PREFERRED_ZONE_ATTR, None)
        order: list[int] = []
        for z in (preferred, current_zone, *_MANGAFIRE_CDN_ZONES):
            if isinstance(z, int) and z not in order:
                order.append(z)

        last_403: SourceError | None = None
        attempted: list[str] = []
        for zone in order:
            candidate = (
                clean_url if zone == current_zone else _rewrite_zone(clean_url, zone)
            )
            # SEC-01: never fetch a rewritten URL that fails the SSRF allowlist (it
            # will pass — same shape — but assert it defensively).
            if not _is_allowed_image_url(candidate):
                continue
            attempted.append(f"mfcdn{zone}")
            try:
                content = await ctx.get_bytes(candidate)
            except SourceError as exc:
                if exc.status == 403:
                    last_403 = exc
                    continue
                raise
            # Remember the zone that answered for the rest of this job. The context is
            # per-job (engine._build_context) so this never crosses jobs; the source
            # object is a shared singleton and must NOT hold this state.
            setattr(ctx, _PREFERRED_ZONE_ATTR, zone)
            return content

        host = (urlparse(clean_url).hostname or "").lower()
        raise SourceError(
            "source_unavailable",
            f"all mangafire CDN zones blocked (403): {host} "
            f"(tried {', '.join(attempted) or 'none'})",
            status=403,
        ) from last_403

    # ─────────────────────────── data access (plain JSON) ─────────────────────

    async def _search_titles(
        self, query: str, limit: int, ctx: SourceContext
    ) -> list[dict[str, Any]]:
        """GET ``/api/titles?keyword=&limit=&page=1`` → the ``items`` title list.

        TITLE-ONLY (each item is a series ``{id,hid,slug,title,…}``, not a chapter); the
        chapter-list key is ``hid``. Returns ``[]`` for a missing/malformed ``items``.
        """
        body = await ctx.get_json(
            f"{self.base_url}/api/titles", keyword=query, limit=limit, page=1
        )
        items = body.get("items")
        return (
            [it for it in items if isinstance(it, dict)]
            if isinstance(items, list)
            else []
        )

    async def _recent_titles(
        self, limit: int, ctx: SourceContext
    ) -> list[dict[str, Any]]:
        """GET ``/api/titles?order[chapter_updated_at]=desc&limit=`` → newest titles.

        The ``order[...]`` bracket param is REQUIRED (a plain ``sort=`` is silently
        ignored) and can't be a Python kwarg, so the URL is built via ``urlencode`` and
        passed whole to ``ctx.get_json``. Returns ``[]`` for a malformed body.
        """
        qs = urlencode({"order[chapter_updated_at]": "desc", "limit": limit})
        body = await ctx.get_json(f"{self.base_url}/api/titles?{qs}")
        items = body.get("items")
        return (
            [it for it in items if isinstance(it, dict)]
            if isinstance(items, list)
            else []
        )

    async def _chapter_list(
        self, hid: str, lang: str, ctx: SourceContext
    ) -> list[dict[str, Any]]:
        """GET the COMPLETE per-title chapter list, paginating past the 200-cap.

        ``GET /api/titles/{hid}/chapters?language=&limit=200&page=`` → ``{items,meta}``.
        The endpoint caps ``limit`` at 200, so a long series has ``meta.lastPage > 1``;
        for the COMPLETE list (source-onboarding completeness rule) the extra pages
        2..lastPage are fetched concurrently (bounded semaphore) and concatenated in
        page order — which preserves the endpoint's newest-first (number desc) ordering.
        """
        first_items, last_page = await self._chapters_page(hid, lang, 1, ctx)
        items = list(first_items)
        if last_page <= 1:
            return items
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _page(page: int) -> list[dict[str, Any]]:
            async with sem:
                page_items, _ = await self._chapters_page(hid, lang, page, ctx)
            return page_items

        rest = await asyncio.gather(*[_page(p) for p in range(2, last_page + 1)])
        for page_items in rest:
            items.extend(page_items)
        return items

    async def _chapters_page(
        self, hid: str, lang: str, page: int, ctx: SourceContext
    ) -> tuple[list[dict[str, Any]], int]:
        """One chapters page → ``(items, meta.lastPage)`` (``lastPage`` floors to 1)."""
        body = await ctx.get_json(
            f"{self.base_url}/api/titles/{hid}/chapters",
            language=lang,
            limit=_CHAPTER_PAGE_LIMIT,
            page=page,
        )
        raw_items = body.get("items")
        items = (
            [it for it in raw_items if isinstance(it, dict)]
            if isinstance(raw_items, list)
            else []
        )
        meta = body.get("meta")
        last_page = (
            self._parse_int(meta.get("lastPage")) if isinstance(meta, dict) else None
        )
        return items, last_page or 1

    async def _chapter_pages(self, chapter_id: str, ctx: SourceContext) -> list[Any]:
        """GET ``/api/chapters/{id}`` → the raw ``data.pages`` list (``[{url,…}]``).

        Raises ``source_unavailable`` when ``data``/``pages`` is missing or empty.
        """
        body = await ctx.get_json(f"{self.base_url}/api/chapters/{chapter_id}")
        data = body.get("data")
        pages = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(pages, list) or not pages:
            raise SourceError(
                "source_unavailable",
                f"no pages in mangafire chapter {chapter_id}",
            )
        return pages

    @classmethod
    def _filter_language(cls, languages: list[str] | None) -> str:
        """The single language threaded into the chapter-list URLs.

        MangaFire is multi-language and the chapter feed is language-scoped, so a
        search/recent request picks the first requested language (or the English
        default).
        """
        if languages:
            return languages[0]
        return _DEFAULT_LANGUAGE

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        hid: str,
        title_id: str,
        manga_title: str,
        lang: str,
        chapter: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single chapter row (D-08).

        The chapter numeric ``id`` is the resolve unit (the ``/api/chapters/{id}`` key)
        AND is folded into the guid tail so chapters with a blank ``number`` (ch_str
        ``"?"``) don't collide and get silently deduped by Mangarr (issue #219).
        """
        chapter_id = chapter.get("id")
        if chapter_id is None:
            return None
        chapter_id = str(chapter_id)
        chapter_number = self._parse_decimal(chapter.get("number"))
        publish_date = (
            _iso_from_epoch(chapter.get("createdAt")) or datetime.now(UTC).isoformat()
        )
        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(manga_title, ch_str, lang)
        guid = f"mangafire:{hid}:ch-{ch_str}:{lang}:{chapter_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=chapter_id,  # the numeric chapter id is the resolve unit
                language=lang,
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
            language=lang,
            scanlation_group=None,
            page_count=None,
            ids={"mangafireHid": hid, "mangafireTitleId": title_id},
        )

    @staticmethod
    def _build_title(manga_title: str, chapter: str, lang: str) -> str:
        """Compose a MangaParser-parseable release title (REL-02, MangaDex shape)."""
        return " ".join([manga_title, "-", f"Chapter {chapter}", f"({lang})"])

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
