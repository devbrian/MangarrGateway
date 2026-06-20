"""Mangadot source — a ``cloudflare`` clean-JSON source + the bare-array endpoint proof.

Mangadot (``https://mangadot.net``) is a **clean-JSON** ``antibot="cloudflare"``
source (plain JSON once cleared, NOT ``+encrypted`` — it is not encrypted like comix):
the framework injects a captured ``cf_clearance`` cookie + UA (D-40) and the plain
``ctx.get_json`` / ``ctx.get_json_array`` / ``ctx.get_bytes`` calls then hit the
``/api`` endpoints directly. The closest analog is now **kagane** (``cloudflare``
clean-JSON, ``decrypt_scheme=None``) rather than mangadex.

HISTORY (read in order — mangadot has TOGGLED its CF interstitial twice):

* debug ``nightly-cf-warm-127-128`` (2026-06-05): mangadot was ``antibot="cloudflare"``
  but had **dropped** its Cloudflare interstitial — every ``/api`` endpoint returned
  plain JSON, HTTP 200, with NO ``cf_clearance`` cookie ever set. The old ``cloudflare``
  solver then navigated the homepage and polled 60s for a never-issued cookie, timing
  out every warm (#128); on the ONE shared ``solve_concurrency=1`` solver that 60s hang
  also starved comix's real solves (#127). It was reclassified to ``antibot="none"``.
* debug ``mangadot-live-smoke-403`` (2026-06-09, #200): mangadot **RE-ENABLED** the
  Cloudflare managed-challenge interstitial — the exact REVERSE of #127/#128. A GET to
  ``/api/search`` with the gateway's plain-httpx UA now returns HTTP 403 + the
  Cloudflare "Just a moment..." JS-challenge HTML (verified live through a FRESH
  residential proxy IP — host-level CF gating, not proxy-IP reputation, so proxy
  rotation does not help). This module is reclassified BACK to ``antibot="cloudflare"``
  so the Patchright solver clears the challenge (as it does for comix/kagane), restoring
  the search→handle→download→CBZ path. ``+encrypted`` is NOT used — mangadot serves
  plain JSON post-clear.

The one genuinely-new framework capability Mangadot exercises is a **bare
top-level JSON array** chapter-list endpoint: ``GET /api/manga/{id}/chapters/list``
returns ``[{…}, …]`` directly (no ``{data:[…]}`` envelope), which every existing
JSON method rejects via ``_parse_json_object``. The additive ``ctx.get_json_array``
(``framework/context.py``) consumes it with full ``get_json`` parity. This module
adds **ZERO networking glue** — every outbound call is ``ctx.get_json`` /
``ctx.get_json_array`` / ``ctx.get_bytes`` (SRC-01/SRC-02).

ENDPOINT SHAPES (live-pinned 2026-06-02, RESEARCH.md):

* base: ``https://mangadot.net``
* search: ``GET /api/search?search={q}&sortBy=relevance&page=N`` →
  ``{manga_list:[{id,title,…}],pagination,…}``. **TITLE-ONLY** — no chapters
  embedded; the search param key is ``search=`` (``q``/``query`` are silently
  ignored). ``chapter_count`` in the search envelope is unreliable (manga-level
  metadata only).
* chapter list: ``GET /api/manga/{id}/chapters/list`` → **bare JSON array** of
  ``{id,chapter_number,volume_number,language,group_name,date_added,page_count,
  source,scanlator_name,…}``. ``chapter_number`` is numeric (int or ``"26.0"``) →
  ``Decimal(str(x))``; ``date_added`` is space-separated + ``+00`` offset
  (``"2026-05-12 12:21:39+00"``) → RFC3339 normalize; one ``chapter_number`` can
  have multiple language/group rows → one Release each.
* manifest: **two namespaces** (debug mangadot-resolve-404) keyed off the chapter
  row's ``source`` field — ``GET /api/uploads/{chapter_id}/images`` for
  ``source=="user"`` (user-uploaded) chapters and
  ``GET /api/chapters/{chapter_id}/images`` for scraped chapters. Both return the
  SAME shape ``{chapter,manga,images:[{url,w,h,filename}],prev_chapter_id,…}``;
  ``images[].url`` is **relative, same-origin**
  (``/chapters/manga_43/chapter_83_g17423/001.webp``); prepend ``base_url``. No
  separate CDN. ``w/h`` often ``0`` → ignored. The two id namespaces OVERLAP
  numerically, so the id alone is ambiguous — ``fetch_manifest`` MUST pick the
  endpoint from the packed ``source`` (see :meth:`_build_resolve_id`).
* image: ``GET https://mangadot.net{images[].url}`` (webp/jpg/png) via
  ``ctx.get_bytes`` (one-line delegate).

guid (D-08): ``mangadot:{manga_id}:ch-{number}:{language}:{chapter_id}`` — the
language + chapter id are required because one chapter number maps to N
language/group rows. The guid (and ``Release.ids.mangadotChapterId``) carry the BARE
chapter ``id`` (wire-stable / display-only). ``ResolutionRecord.chapter_id`` carries
a COMPOSITE ``{id}|{source}`` (debug mangadot-resolve-404) so the stateless
``fetch_manifest`` hook can route to the right manifest namespace; this mirrors
comix's composite-chapter-id idiom. Still DIRECT — no ``:DEFERRED`` (Mangadot exposes
the stable id, unlike Comix's recent feed).

recent(): a verbatim no-op (``return []``, ``supports_recent=False``). Mangadot
exposes no clean chapter-level JSON feed (CONTEXT.md / RESEARCH.md — live-probed
2026-06-02). The core value (search → handle → download → CBZ, R1/R6) is fully
delivered without recent(). Tracked for revisit as a GitHub issue (mirrors comix #31).

⚠️ Chapter-id namespace split (debug mangadot-resolve-404, CONFIRMED LIVE via the
reader's own network calls): mangadot has TWO manifest namespaces —
``/api/uploads/{id}`` (user-uploaded, ``source=="user"``) and ``/api/chapters/{id}``
(scraped) — whose numeric ids OVERLAP. A ``source=="user"`` id fetched on the
``/api/chapters`` endpoint either
404s (absent there) or returns a DIFFERENT manga's chapter (e.g. id 451326 →
"Selfish Romance" on ``/api/chapters`` but "Solo Max-Level Newbie" ch 262 on
``/api/uploads``). This was earlier mis-described as junk/aggregated entries listing
foreign ids; it is actually the namespace split, now handled by routing on ``source``.
The live-smoke ``default_query`` is chosen so its ``chapters/list`` ids resolve back to
the SAME manga with a matching ``page_count`` (see ``tests/live/profiles/mangadot.py``).
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
from ..framework.external_links import normalize
from ..framework.relevance import _normalize, prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import ExternalLinks, SearchRequest

# search() deep-enumerates this many manga candidates (mangaball GAP-1 lock —
# search is TITLE-ONLY, so each candidate is a full chapters/list fan-out). Like
# mangaball, ``req.interactive`` does NOT widen the candidate count.
_DEFAULT_MANGA_CANDIDATES = 5

# Bounds the per-candidate ``chapters/list`` fan-out in ``search`` — at most this
# many chapter-list fetches run concurrently. The candidates are no longer fetched
# serially after the #101 fix (sequential at rate_limit=10 crossed the 30s fanout
# timeout); 6 collapses the wall-clock while staying well under the per-source
# rate budget (480/min).
_CHAPTERS_FANOUT_CONCURRENCY = 6

# Floor for empty/malformed timestamps so they sort oldest and never crash.
_TS_FLOOR = datetime.min.replace(tzinfo=UTC)

# Separator packing each chapter row's ``source`` into the minted
# ``ResolutionRecord.chapter_id`` so the stateless ``fetch_manifest(chapter_id, ctx)``
# hook can route to the correct manifest namespace (debug mangadot-resolve-404).
# mangadot serves user-uploaded chapters from ``/api/uploads/{id}/images`` and scraped
# chapters from ``/api/chapters/{id}/images`` — two namespaces whose numeric ids
# OVERLAP, so the id ALONE is ambiguous. Mirrors comix's composite-chapter-id idiom
# (comix.py ``_CID_SEP``).
_CHAPTER_ID_SEP = "|"

# SSRF allowlist for the EXTRACTED page-image URLs (T-07-07/T-07-09, CLAUDE.md).
# Unlike mangaball's varying CDN, Mangadot serves images SAME-ORIGIN, so the host
# is PINNED to ``mangadot.net`` / ``*.mangadot.net`` and the path namespace to
# ``^/chapters/`` (RESEARCH §SSRF). The folder/filename shape varies per source
# (``manga_{id}/chapter_{n}/`` for scraped, ``manga_{id}/user_{uid}_…/`` for user),
# so only the STABLE invariants are enforced: ``/chapters/`` prefix + image ext.
_MANGADOT_IMG_PATH_RE = re.compile(
    r"^/chapters/[A-Za-z0-9_./-]+\.(webp|jpg|jpeg|png)$",
    re.IGNORECASE,
)


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` is a Mangadot same-origin page image (SSRF allowlist).

    Belt-and-suspenders defence on every manifest-extracted URL before the
    framework fetches it (T-07-07/T-07-09). Mangadot serves images same-origin,
    so — unlike mangaball — the host is PINNED to ``mangadot.net`` /
    ``*.mangadot.net`` and the path to the ``^/chapters/`` namespace. Rejects
    non-HTTPS schemes, off-host URLs, path-traversal, and non-image extensions.

    CR-01 parity (mangaball): validate the path httpx will ACTUALLY fetch — httpx
    normalizes ``..`` segments before issuing the request, so a raw path like
    ``/chapters/../../etc/passwd.webp`` would match the allowlist regex yet fetch
    ``/etc/passwd.webp``. Reject any literal ``..`` segment outright AND validate
    the ``posixpath.normpath``-resolved path.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # Pin to the EXACT origin (review WR/CR): mangadot serves page images
    # same-origin and ``fetch_manifest`` rewrites every relative ``/chapters/…``
    # url onto ``base_url`` (mangadot.net), so no subdomain is ever a legitimate
    # image host. A wildcard ``*.mangadot.net`` only widens the SSRF surface for
    # zero real coverage.
    if host != "mangadot.net":
        return False
    # Reject traversal outright; validate the NORMALIZED path httpx will fetch.
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return parsed.scheme == "https" and bool(_MANGADOT_IMG_PATH_RE.match(norm_path))


class MangadotSource(Source):
    """Mangadot (mangadot.net) — clean-JSON ``antibot="cloudflare"`` (bare-array list).

    A kagane-class clean-JSON ``cloudflare`` source whose only new requirement is the
    bare top-level JSON array chapter-list endpoint, consumed via the additive
    ``ctx.get_json_array`` (full ``get_json`` parity). Cloudflare clearance is
    framework-owned (D-40/D-35) — this source declares only the class-attrs
    (``antibot="cloudflare"`` + ``cloudflare_challenge_url``) and adds zero networking
    glue (see module docstring).
    """

    key = "mangadot"
    name = "Mangadot"
    base_url = "https://mangadot.net"
    # Title-search only — no external metadata-id namespace.
    id_types: list[str] = []
    # English-only at v1 (widen if the live loop shows other langs present).
    languages = ["en"]
    # Measured ceiling (#101 fix): the PR #102 rate-limit probe ran mangadot live at
    # concurrency 8 and the full sweep up to 960/min sustained across
    # search/manifest/image with ZERO rate-limiting and ZERO latency degradation
    # ("FLOOR — no limit hit across the full tested grid"). 480/min is a conservative
    # half-of-floor with large headroom. The prior `10` was the root cause of #101: a
    # single search issues up to 6 serialized rate-limited requests (1 /api/search + up
    # to 5 sequential chapters/list); at 10/min the limiter spaced them ~6s apart once
    # the burst budget drained, crossing framework/fanout.py's 30s timeout →
    # source_unavailable. At 480/min the calls space ~0.125s apart.
    rate_limit_per_minute = 480
    # Per-source download-job concurrency (D-30 override): the PR #102 probe measured
    # mangadot tolerating >=960/min/endpoint with no throttling, so up to 3 chapters
    # download in parallel (vs the global per-source default of 1). Clamped to the
    # global max_concurrent_chapters by the job manager.
    max_concurrent_jobs = 3
    # Cloudflare managed-challenge interstitial — RE-ENABLED 2026-06-09 (debug
    # mangadot-live-smoke-403, #200), the exact REVERSE of #127/#128 which had recorded
    # mangadot DROPPING it. As of 2026-06-09 a GET to any mangadot.net/api endpoint with
    # the gateway's plain-httpx UA returns HTTP 403 + the Cloudflare "Just a moment..."
    # JS-challenge HTML (verified live through a FRESH residential proxy IP → host-level
    # CF gating, not proxy-IP reputation; proxy rotation does not help). It is the SAME
    # interactive-challenge class the Patchright solver already clears for comix/kagane,
    # and the SAME state mangadot was in pre-#127/#128. ``antibot="cloudflare"`` (NOT
    # ``+encrypted`` — mangadot serves plain JSON once cleared; it is not encrypted like
    # comix) re-adds mangadot to the shared CF solver's warm set, so the framework
    # injects the captured ``cf_clearance`` cookie + UA (D-40) and reconciles a
    # challenge 403 via a single forced re-solve (D-35). The framework picks this up
    # PURELY from the classification — no per-source warm-set wiring (CLAUDE.md: adding
    # a CF source needs zero custom networking glue).
    antibot = "cloudflare"
    # Per-source challenge host the solver navigates to earn the cf_clearance cookie
    # (#88 per-domain map; mirrors comix/kagane). mangadot serves plain JSON post-clear,
    # so ``decrypt_scheme``/``session_prep`` stay ``None`` (no encrypted-response path).
    cloudflare_challenge_url = "https://mangadot.net/"
    # Phase 10 (SRC-01): route clearance to the Android-WebView solver sidecar, NOT
    # the desktop Patchright solver. mangadot.net serves a STRICT Cloudflare Turnstile
    # that desktop browser automation (Patchright/Camoufox/nodriver) cannot clear from
    # Linux — a mechanistic catch-22 proven across 27 experiments (resolved debug
    # ``mangadot-cf-linux-fingerprint``). A real Android WebView is a trusted
    # fingerprint class that DOES clear it (proven end-to-end). This one-line attr is
    # the only per-source code the engine switch needs.
    solver_engine = "android"
    decrypt_scheme = None
    session_prep = None
    supports_search = True
    # No clean chapter-level JSON feed (CONTEXT.md / RESEARCH.md). recent() is a
    # verbatim no-op; tracked for revisit as a GitHub issue (mirrors comix #31).
    supports_recent = False

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-(chapter row) Releases (SRCH-01..07, D-08).

        Two-call live flow: ``GET /api/search`` is TITLE-ONLY, so ``search``
        deep-enumerates the first ``_DEFAULT_MANGA_CANDIDATES`` manga candidates
        via a per-candidate ``GET /api/manga/{id}/chapters/list`` (a bare JSON
        array, consumed by ``ctx.get_json_array``). Those per-candidate fetches run
        with bounded concurrency (``_CHAPTERS_FANOUT_CONCURRENCY``) — not serially —
        which collapses the wall-clock under the 30s fan-out budget (#101). One
        Release is minted per chapter row — and per language/group row when one
        ``chapter_number`` has multiple rows (D-08). Per candidate, releases are
        language-filtered by ``req.languages``, ordered NEWEST-FIRST by parsed
        ``date_added``, sliced to ``req.limit``, and ONLY THEN minted (mangaball
        GAP-2 mint-after-slice — handle-eviction lesson). Cross-candidate
        aggregation preserves submission order (``asyncio.gather`` returns results
        in task order). ZERO networking glue — both calls ride ``ctx`` (SRC-01/02).
        """

        async def _resolve_fn() -> list[dict[str, Any]]:
            envelope = await ctx.get_json(
                f"{self.base_url}/api/search",
                search=req.query or "",
                sortBy="relevance",
                page=1,
            )
            manga_list = envelope.get("manga_list")
            dict_candidates = [m for m in (manga_list or []) if isinstance(m, dict)]
            # Prune obviously-irrelevant candidates BEFORE the per-candidate
            # chapters/list fan-out (#126): an exact-match query enumerates only the
            # one correct series; ambiguous queries still fan out to the cap (the
            # prune falls back to the historic ``[:_DEFAULT_MANGA_CANDIDATES]``).
            return prune_candidates(
                dict_candidates,
                req.query or "",
                # Score over the main title OR any alternate/native title (#139). The
                # alt-title field is absent from the current mangadot search payload,
                # so ``or []`` keeps behavior byte-identical until/if it appears.
                keys=lambda m: [m.get("title"), *(m.get("alt_titles") or [])],
                cap=_DEFAULT_MANGA_CANDIDATES,
            )

        # Layer 1 (D-01): cache the title→pruned-candidate resolution so a repeat
        # chapter search on the same (query, languages) skips the ``/api/search``
        # call entirely (genuinely zero upstream calls on a HIT). The key normalizes
        # the query the SAME way the relevance scorer does so case/punctuation
        # variants collapse onto one entry (T-09-01: never keys on type/chapter).
        candidates: list[dict[str, Any]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(req.query or ""), req.languages or []),
            _resolve_fn,
        )
        # 260605-e9a deliverable 5: how many manga candidates we deep-enumerate (HIT
        # too — set AFTER the resolve).
        ctx.candidates_enumerated = len(candidates)
        wanted_langs = set(req.languages) if req.languages else None
        limit = req.limit or 50

        # Bound the per-candidate chapters/list fan-out (one Semaphore shared across
        # the dispatch; constructed HERE so it binds to the running loop, never at
        # import time).
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(manga_id: str, manga_title: str) -> list[Release]:
            # Layer 2 (CACHE-02/03): cache the UNFILTERED per-candidate chapters/list
            # enumeration per (manga_id, languages). The ``async with sem:`` + the
            # ``get_json_array`` upstream fetch live INSIDE ``_enum_fn`` so a HIT
            # acquires NEITHER the fan-out semaphore NOR a rate-limit token (the
            # limiter lives inside ``get_json_array``, one level below the cache
            # check). ``exhausted=True``: ``/chapters/list`` returns the COMPLETE feed,
            # so ``covers_floor`` is always True and no deeper refetch exists.
            async def _enum_fn() -> Enumeration:
                async with sem:
                    rows = await ctx.get_json_array(
                        f"{self.base_url}/api/manga/{manga_id}/chapters/list"
                    )
                return Enumeration(
                    items=rows,
                    chapter_numbers=tuple(
                        d
                        for r in rows
                        if isinstance(r, dict)
                        and (d := self._parse_decimal(r.get("chapter_number")))
                        is not None
                    ),
                    exhausted=True,
                    requested_limit=limit,
                )

            # IN-02: ``/chapters/list`` returns the whole manga's feed and does NOT
            # depend on ``languages`` (the language filter is applied post-cache), so
            # key on ``[]`` — different-language requests for one manga share the
            # cached listing instead of re-fetching byte-identical data per language.
            enum = await ctx.cached_enumerate(
                ctx.cached_enumerate_key(manga_id, []), _enum_fn
            )
            # _chapters_to_releases is UNCHANGED — the chapter_matches filter +
            # newest-first sort + [:limit] + GAP-2 mint-after-slice all stay; it
            # simply consumes ``enum.items`` (the cached raw rows) instead of the
            # raw fetch result.
            releases = self._chapters_to_releases(
                enum.items, manga_id, manga_title, wanted_langs, limit, ctx, req
            )
            # CR #289: skip the external-links resolve entirely when no release
            # survived filtering — a non-matching/sparse candidate must not issue a
            # wasted detail GET (the resolved object would be stamped onto nothing).
            if not releases:
                return releases

            # Phase-13 (R4/R6/D-02): resolve the series' tracker links ONCE via the
            # framework wrapper (which owns the resolve-once cache + 5s timeout +
            # swallow-all-to-None best-effort, D-03), then stamp the single resolved
            # object onto every release of the series (identical-object, R4). The
            # detail URL inside ``fetch_external_links`` is built from ``manga_id``
            # (this source's OWN search candidate id), never client input (SSRF-safe).
            async def _parse_links(mid: str = manga_id) -> ExternalLinks | None:
                return await self.fetch_external_links(mid, ctx)

            links = await ctx.resolve_external_links(manga_id, _parse_links)
            for rel in releases:
                rel.external_links = links
            return releases

        # Pre-filter candidates lacking a usable ``id`` BEFORE dispatching tasks — a
        # candidate with no ``id`` must not produce a task (preserves the old skip).
        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for manga in candidates:
            manga_id = manga.get("id")
            if manga_id is None:
                continue
            tasks.append(
                _fetch_candidate(str(manga_id), str(manga.get("title") or "Unknown"))
            )

        # gather, NOT TaskGroup: gather (return_exceptions=False) re-raises the FIRST
        # child exception UNCHANGED, so a SourceError from ctx.get_json_array
        # propagates out of search() exactly as the old sequential loop did, and
        # framework/fanout.py's ``except SourceError`` classifies it as the source's
        # own code. TaskGroup wraps child exceptions in an ExceptionGroup → fanout's
        # ``except Exception`` → "unexpected error" (changed classification,
        # regression). gather also returns results in submission order, so
        # cross-candidate aggregation order is byte-identical to the old loop.
        results = await asyncio.gather(*tasks)

        releases: list[Release] = []
        for chunk in results:
            releases.extend(chunk)
        return releases

    async def fetch_external_links(
        self, series_id: str, ctx: SourceContext
    ) -> ExternalLinks | None:
        """One best-effort detail GET → canonical tracker links (D-02/R6).

        Issues a SINGLE ``GET /api/manga/{series_id}`` (the detail object, distinct
        from the ``/chapters/list`` enumeration) through the framework transport + the
        source's CF-cleared android-solver session — no new anti-bot plumbing — and
        routes the tracker fields (``anilist_id``, ``mal_id``, ``mangaupdates_id``,
        ``mangabaka_id``, ``kitsu_id``, ``mangadex_id``) through the shared normalizer
        (numeric fields stringified, ``null`` fields dropped). Those fields live in the
        NESTED ``manga`` sub-object of the detail body (live-verified 2026-06-20,
        RESEARCH A1) — the unwrap is below.

        ``series_id`` is a gateway-internal id (this source's OWN search candidate),
        so the URL is never a Mangarr-supplied value (SSRF-safe, T-13-01). NO
        try/except/timeout here — the framework wrapper (``resolve_external_links``)
        owns resolve-once + the 5s timeout + swallow-all-to-``None`` (D-03), so a
        raising/slow detail GET leaves the chapter releases intact (R6).
        """
        body = await ctx.get_json(f"{self.base_url}/api/manga/{series_id}")
        # The tracker fields live in the NESTED ``manga`` sub-object, NOT at the top
        # level (live-verified 2026-06-20 via the android-solver, RESEARCH A1 — the
        # headed-Patchright recon that froze the flat-shape assumption was never
        # re-verified). The real detail body is
        # ``{"manga": {…, anilist_id, mal_id, mangaupdates_id, mangabaka_id,
        # kitsu_id, mangadex_id, …}, "total_chapters": …, "first_chapter_id": …,
        # "first_chapter_source": …, "status_text": …, "date_added_formatted": …}``.
        # The flat top-level body carries NONE of the tracker keys, so the normalizer
        # dropped everything → empty ``externalLinks`` for every release (the live-smoke
        # gate that caught this). The field NAMES inside ``manga`` match _MANGADOT_MAP
        # verbatim, so only the unwrap changes; ``mangadex_id`` is ``null`` (dropped).
        manga = body.get("manga")
        if not isinstance(manga, dict):
            return None
        return normalize(manga, "mangadot")

    def _chapters_to_releases(
        self,
        rows: list[Any],
        manga_id: str,
        manga_title: str,
        wanted_langs: set[str] | None,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one manga's bare chapter array → per-row Releases (mangaball GAP-2).

        Language-filtered, NEWEST-FIRST by parsed ``date_added``, sliced to
        ``limit``. Multi-language/group-per-number is preserved: distinct chapter
        ids → distinct guids.

        GAP-2 (mangaball): mint handles ONLY for the post-slice survivors. A long
        title's full ``chapters/list`` (hundreds of rows × language rows) would
        otherwise over-mint and EVICT the returned releases' own handles from the
        10_000-entry store, so a later ``POST /downloads`` resolves to a miss.
        Collect sort keys first, slice to ``limit``, THEN mint.
        """
        sortable: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("id") is None:
                continue  # no resolve unit → _to_release would drop it anyway
            language = str(row.get("language") or "en")
            if wanted_langs is not None and language not in wanted_langs:
                continue
            # 260606-2ff: drop a non-matching chapter BEFORE it enters `sortable` →
            # before the newest-first sort / [:limit] slice / handle mint (preserves
            # the GAP-2 mint-after-slice ordering). Gate-off = pass-through.
            chapter_number = self._parse_decimal(row.get("chapter_number"))
            if not self.chapter_matches(req, chapter_number):
                continue
            sortable.append((self._parse_ts(row.get("date_added")), row))
        sortable.sort(key=lambda item: item[0], reverse=True)  # newest-first

        releases: list[Release] = []
        for _ts, row in sortable[:limit]:  # mint AFTER slice (GAP-2)
            rel = self._to_release(manga_id, manga_title, row, ctx)
            if rel is not None:
                releases.append(rel)
        return releases

    # IN-02: recent() intentionally does NOT populate Release.externalLinks. Mangadot is
    # a DETAIL-FETCH source — populating links would cost one detail GET per distinct
    # series in the recent feed (not guarded by the resolve-once cache), a deliberate
    # trade-off; interactive search() carries them. See review finding IN-02.
    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        """No-op recent feed — ``supports_recent=False`` for v1 (verbatim comix).

        Mangadot exposes no clean chapter-level JSON feed (CONTEXT.md / RESEARCH.md,
        live-probed 2026-06-02: ``/api/search?sortBy=latest`` is manga-level with
        ``chapter_count:0``; ``/view-all/latest-updates`` is SSR HTML with no
        chapter id/date/language; the ``.data`` loader is a React-Router
        turbo-stream). The core value (search → handle → download → CBZ, R1/R6) is
        fully delivered without recent(). Tracked for revisit as a GitHub issue
        (mirrors comix #31). ``framework/fanout.py`` does not gate on
        ``supports_recent``, so an empty list is a clean no-op, not a contract
        failure.
        """
        _ = (languages, limit, since, ctx)  # deliberately unused — see docstring
        return []

    # ───────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered page-image URLs, INTERNALLY (PKG-01/R6).

        Routes by the chapter's upstream namespace, then GETs the manifest JSON
        object via ``ctx.get_json``, reads the ordered ``images`` array (array order =
        page order), prepends ``base_url`` to each RELATIVE ``/chapters/...`` url, and
        SSRF-allowlists every resulting URL (:func:`_is_allowed_image_url`) BEFORE
        return. The image URLs are EXTRACTED from the response, NEVER reconstructed
        (T-07 / CLAUDE.md SSRF). Empty ``images`` OR any URL failing the allowlist
        raises ``SourceError("source_unavailable", …)`` — no blind fetch
        (T-07-07/09). The manifest is consumed only by the gateway's own engine,
        never returned to a caller (R6).

        Namespace routing (debug mangadot-resolve-404): mangadot serves
        **user-uploaded** chapters from ``/api/uploads/{id}/images`` and **scraped**
        chapters from ``/api/chapters/{id}/images`` — two id namespaces whose numeric
        ranges OVERLAP. The search step packs the row's ``source`` into the resolve id
        (:meth:`_build_resolve_id`); here we unpack it and pick the endpoint:
        ``source == "user"`` → ``uploads`` (the reader's own choice for these rows),
        else ``chapters`` (scraped — the pre-existing default; a BARE id with no packed
        source also lands here, so older handles resolve unchanged). Using the wrong
        namespace previously caused either an upstream 404 (id absent there) OR a
        SILENT wrong-manga manifest (id collided with a different scraped chapter).
        The response shape is identical across both endpoints, so the parse below is
        namespace-agnostic.
        """
        raw_chapter_id, source = self._parse_resolve_id(chapter_id)
        endpoint = "uploads" if source == "user" else "chapters"
        body = await ctx.get_json(
            f"{self.base_url}/api/{endpoint}/{raw_chapter_id}/images"
        )
        images = body.get("images")
        # WR-01: guard the nested ``images`` shape explicitly. ``get_json`` only
        # validates the top-level object; a non-list ``images`` (string/dict/null)
        # would otherwise iterate wrongly (or as ``[]``) and surface as the
        # misleading "no page images found" below. A clear shape error is
        # diagnosable; mirrors ``_parse_json_array``'s non-list guard discipline.
        if not isinstance(images, list):
            raise SourceError(
                "source_unavailable",
                f"malformed manifest for chapter {chapter_id}: "
                f"'images' is not a list (got {type(images).__name__})",
            )
        urls: list[str] = []
        for entry in images:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("url")
            if not isinstance(raw, str) or not raw.strip():
                continue
            raw = raw.strip()
            # Prepend base_url to the relative, same-origin ``/chapters/...`` url.
            # An already-absolute url is allowlisted as-is (defensive — the live
            # shape is relative, but the host pin still scopes it to mangadot.net).
            url = raw if raw.startswith("http") else f"{self.base_url}{raw}"
            urls.append(url)
        if not urls:
            raise SourceError(
                "source_unavailable",
                f"no page images found in manifest for chapter {chapter_id}",
            )
        for url in urls:
            if not _is_allowed_image_url(url):
                # Never fetch a non-allowlisted (off-host / off-shape) URL. Name the
                # offending URL so an allowlist/shape divergence is diagnosable.
                raise SourceError(
                    "source_unavailable",
                    f"manifest image URL failed the SSRF allowlist: {url!r}",
                )
        return urls

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes via the shared session (PKG-02).

        Delegates to ``ctx.get_bytes`` (mirror mangadex/mangaball): bounded by the
        per-job semaphore, NOT the per-source API limiter (D-31). Mangadot serves
        plaintext webp/jpg/png same-origin. A ``Referer: https://mangadot.net/`` is
        added ONLY if the live loop shows the image GET hotlink-403s with a bare GET
        (RESEARCH §image — likely unneeded; live-tune).
        """
        return await ctx.get_bytes(url)

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        manga_id: str,
        manga_title: str,
        row: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single chapter row (D-08)."""
        chapter_id = row.get("id")
        if chapter_id is None:
            return None
        chapter_id = str(chapter_id)
        # Capture the upstream namespace ("user" upload vs scraped) so fetch_manifest
        # can route to the right manifest endpoint (debug mangadot-resolve-404).
        chapter_source = row.get("source")

        language = str(row.get("language") or "en")
        chapter_number = self._parse_decimal(row.get("chapter_number"))
        page_count = self._parse_int(row.get("page_count"))
        # REL-03: display-only votes from the per-chapter discussion counter
        # (unit = comments). Live values are often 0 (a data property, not a
        # wiring concern) and None-safe when absent. NOT added to the minted
        # ResolutionRecord (display-only).
        votes = self._parse_int(row.get("comment_count"))
        volume = self._parse_int(row.get("volume_number"))
        publish_date = self._normalize_publish_date(row.get("date_added"))
        group_name = (
            str(row["group_name"])
            if row.get("group_name")
            else (str(row["scanlator_name"]) if row.get("scanlator_name") else None)
        )

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(
            manga_title, ch_str, language=language, group=group_name
        )
        # D-08: language + chapter id needed — one chapter number maps to N
        # language/group rows.
        guid = f"mangadot:{manga_id}:ch-{ch_str}:{language}:{chapter_id}"

        # Pack the chapter's source into the resolve id so the stateless
        # fetch_manifest hook routes to /api/uploads vs /api/chapters (debug
        # mangadot-resolve-404). The guid + ids below stay the BARE chapter id
        # (wire-stable / display-only); only the handle's resolve unit carries it.
        resolve_id = self._build_resolve_id(chapter_id, chapter_source)
        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=resolve_id,  # bare id (+ packed source) → manifest route
                language=language,
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=volume,
                scanlation_group=group_name,
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
            scanlation_group=group_name,
            page_count=page_count,
            votes=votes,
            ids={"mangaId": manga_id, "mangadotChapterId": chapter_id},
        )

    @staticmethod
    def _build_title(
        manga_title: str,
        chapter: str,
        *,
        language: str | None,
        group: str | None,
    ) -> str:
        """Compose a MangaParser-parseable release title (REL-02), mangaball shape."""
        parts = [manga_title, "-", f"Chapter {chapter}"]
        if language:
            parts.append(f"({language})")
        if group:
            parts.append(f"[{group}]")
        return " ".join(parts)

    @staticmethod
    def _parse_ts(raw: Any) -> datetime:
        """Parse a ``date_added`` to an aware datetime for the newest-first sort.

        Mangadot's ``date_added`` is space-separated with a ``+00`` offset
        (``"2026-05-12 12:21:39+00"``); ``datetime.fromisoformat`` accepts both the
        space separator and the offset (Python 3.11+). Empty/malformed values floor
        to epoch-min so they sort oldest rather than raising.
        """
        if not raw:
            return _TS_FLOOR
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            return _TS_FLOOR
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @classmethod
    def _normalize_publish_date(cls, raw: Any) -> str:
        """Normalize ``date_added`` → RFC3339 ``date-time`` (REL-03; mangaball shape).

        The contract's ``Release.publishDate`` is ``format: date-time`` (RFC3339,
        ``T`` separator). The live ``date_added`` is space-separated
        (``"2026-05-12 12:21:39+00"``) which fails schema conformance.
        :meth:`_parse_ts` yields an aware datetime; ``.isoformat()`` re-serializes
        with the ``T`` separator. Empty/unparseable values floor to ``_TS_FLOOR``
        (year 1) — a nonsense publish date — so we fall back to ``now(UTC)``.
        """
        parsed = cls._parse_ts(raw)
        if parsed == _TS_FLOOR:
            return datetime.now(UTC).isoformat()
        return parsed.isoformat()

    @staticmethod
    def _parse_decimal(raw: Any) -> Decimal | None:
        """Parse a chapter number to Decimal (handles int 1 and string "26.0")."""
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

    @classmethod
    def _build_resolve_id(cls, chapter_id: str, source: Any) -> str:
        """Pack ``chapter_id`` + chapter ``source`` into the handle's resolve id.

        mangadot serves user-uploaded chapters from ``/api/uploads/{id}/images`` and
        scraped chapters from ``/api/chapters/{id}/images`` — two namespaces whose
        numeric ids OVERLAP (debug mangadot-resolve-404). The row's ``source`` decides
        which, so it is packed into ``ResolutionRecord.chapter_id`` (the only datum
        :meth:`fetch_manifest` receives). A falsy/blank source yields a BARE id (no
        separator) so scraped rows — and any pre-existing handle — keep hitting
        ``/api/chapters`` exactly as before.
        """
        src = str(source).strip() if source not in (None, "") else ""
        return f"{chapter_id}{_CHAPTER_ID_SEP}{src}" if src else chapter_id

    @classmethod
    def _parse_resolve_id(cls, composite: str) -> tuple[str, str | None]:
        """Unpack ``{chapter_id}|{source}`` → ``(chapter_id, source|None)``.

        Tolerant of a BARE id (no separator → ``source=None`` → scraped namespace),
        so older handles and scraped rows resolve exactly as before. Inverse of
        :meth:`_build_resolve_id`.
        """
        raw_id, sep, src = composite.partition(_CHAPTER_ID_SEP)
        if not sep:
            return composite, None
        return raw_id, (src or None)
