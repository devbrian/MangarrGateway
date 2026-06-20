"""Comix source — the first ``cloudflare+encrypted`` declarative source (SRC-06).

Subclasses :class:`~manga_gateway.framework.base.Source` exactly like
``mangadex.py``: it declares its D-13 metadata as class attributes and overrides the
four hooks (``search``/``recent``/``fetch_manifest``/``fetch_image``). ALL networking,
rate-limiting, retry, and Cloudflare clearance injection (D-40) / challenge re-solve
(D-35) live in the injected ``ctx`` — this module is just Comix param shaping +
response parsing. This is the reusability proof of the phase (criterion #1): a new
cloudflare+encrypted source is a declarative subclass with ZERO new networking/glue,
riding the Wave-1/2 seams.

The single anti-bot declaration that distinguishes Comix from MangaDex:

* ``antibot = "cloudflare+encrypted"`` — the framework injects the captured
  ``cf_clearance`` + matching UA per request and re-solves a challenge 403 (D-40/D-35).

In the Option A pivot (Plan 04-04 commit 2/3, 2026-05-30), the chapter-pages and
chapter-list paths switched to a browser-DOM read via ``solver.fetch_via_browser``,
and the plaintext search endpoint uses ``get_json_plain``. As a result Comix has
no live encrypted-response path — ``decrypt_scheme`` stays ``None`` (issue #46
Option A: the ``comix-v1`` browser-evaluated decrypt seam was non-functional dead
code on the current ``secure-*.js`` bundle and has been removed; the
``framework.decrypt`` registry remains the seam shape for future sources whose
token + cipher problem CAN be split).

ENDPOINT SHAPES (live-recon-pinned, Plan 04-04 Commit 3):

The shapes below come from real Comix traffic captured via the Plan 04-04 recon
(see ``.planning/phases/04-comix-source-anti-bot-stack/04-CONTEXT.md``'s
``<live_recon>`` block):

* base: ``https://comix.to``
* search (PLAINTEXT, httpx): ``GET /api/v1/manga`` with ``keyword``, ``limit``,
  ``page``, ``content_rating=suggestive``, ``order[relevance]=desc``
  → ``{"status":"ok","result":{"items":[{"hid","title","url","latestChapter", …}]}}``
* chapter list (Option A — browser-DOM): navigate ``/title/{hid}-{slug}`` and
  read the rendered chapter list off the DOM, including the scanlation group
  name from each row's ``<a class="mchap-row__group">`` anchor. (The plaintext
  ``/api/v1/manga/{hid}/chapter-indexes?group_id=-1`` endpoint now requires
  a JS-minted ``_=`` request token and returns 403 ``{"message":"Invalid
  token."}`` for any unsigned call; the browser-DOM read is the only
  reachable source for the group name.)
* chapter pages (Option A — browser-DOM): navigate
  ``/title/{hid}-{slug}/{chapter_id}-chapter-{number}`` and read the rendered
  ``<img src="https://{cdn}.store/{seg}/{token}/{NN}.webp">`` tags off the
  DOM (``{seg}`` is a rotating short path segment — ``si``/``i3``/…; see
  ``_COMIX_CDN_PATH_RE``).
  The page's own JS handles token-mint + encrypted-API call + decrypt + render;
  we just read the result.
* image CDN: ``https://{cdn}.store/{seg}/{32-char-token}/{NN}.webp`` — fetched via
  httpx (NOT through the browser, CLAUDE.md). The browser only resolves the URL
  list; the bulk byte fetch is the cleared httpx client.

D-46 (hid is the canonical Comix identifier): Comix uses a 5-char base32-ish slug
(``mr3m0``, ``qeq3x``, …) as the series identifier; releaseHandle / guid composition
uses ``hid`` (not the numeric ``id``).

Composite chapter id: ``ResolutionRecord.chapter_id`` for Comix is the composite
string ``"{chapter_id}|{hid}|{slug}|{chapter_number}"`` so :meth:`fetch_manifest`
can reconstruct the full chapter URL the browser needs. The numeric chapter_id
is the leading segment; the rest are URL-construction-only metadata. The
framework treats chapter_id as source-opaque (engine just passes it through to
fetch_manifest), so the composite is self-contained.

Issue #30 / #31 (2026-05-30): two related contract gaps surfaced by the Phase 5
first live-smoke run and fixed in branch ``fix/comix-publishdate-recent``:

* #30 — ``Release.publishDate`` came out as the empty string. The chapter-list
  DOM does NOT expose a machine-readable absolute timestamp — only the
  rendered ``createdAtFormatted`` text on ``<span class="mchap-row__time">``
  (e.g. "14h ago", "3mos ago"). The JS extractor now captures that text and
  :meth:`_parse_relative_time` approximates it to an ISO 8601 UTC date-time
  so the REL-01 ``format: date-time`` requirement holds. Approximate-but-
  contract-conformant is honest: the upstream itself only renders the same
  approximation in the UI.
* #31 — ``/recent`` returned ``{releases: [], warnings: []}`` silently because
  the only known recipe was the N+1 series-page drill-down (Cloudflare
  protected, rate-limit saturating). The initial fix declared
  ``supports_recent = False``; issue #42 (2026-05-31) supersedes this with
  a plaintext one-call feed (``/api/v1/manga?order[chapter_updated_at]=desc``)
  whose chapter-id resolution is deferred to ``fetch_manifest`` at download
  time. ``supports_recent`` is now ``True`` and ``recent()`` synthesizes
  real Releases; see the deferred-resolver helpers near ``_CID_SEP``.

Spike 019 (debug comix-cdn-scheme-rotation, 2026-06): the chapter-list AND
chapter-pages reads both switched from scraping the rendered DOM to running
comix's OWN internal API loaders in the warm tab. The series enumeration calls
``chapters(hid, {limit:100})`` (``_CHAPTER_LIST_API_EXTRACT_JS``) and the
manifest calls ``/chapters/{id}`` (``_CHAPTER_PAGES_API_EXTRACT_JS``) via the
env module's axios instance, whose ``ro(ri)`` interceptors sign the ``_=`` token
AND decrypt the ``{"e":...}`` envelope. This replaced a long line of lazy-reader-
DOM heuristics (issue #20 scrollIntoView, #32 silent truncation, the
scaffold-counter wait of comix-scaffold-partial-capture, and the Step-4 filename
synthesis) which finally became untenable when comix rotated the image-CDN URL
scheme to a per-page opaque token with NO filename — breaking both the capture
regex and synthesis at once. The internal API returns the full decrypted page
list regardless of the CDN URL shape, so it is robust to the next rotation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

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

_log = logging.getLogger("manga_gateway")


# Bound a title search's candidate series. The count is mode-invariant: interactive
# no longer widens the fan-out (#162, mirrors MangaDex). Exact-match queries already
# collapse to ~1 candidate after the #126 prune regardless of the ceiling, so 5 is the
# single source of truth for both modes (still referenced by the prune/cap below).
_DEFAULT_SERIES_CANDIDATES = 5
# Comix chapter-feed page-size ceiling (live recon: server default limit=20).
_MAX_FEED_LIMIT = 100

# ── Search token gate (debug ``comix-search-api-403``, 2026-06-13) ────────────
# Comix moved the plaintext search API ``GET /api/v1/manga`` behind a JS-minted
# ``_=`` request token — exactly as it earlier did to
# ``/api/v1/manga/{hid}/chapter-indexes`` ("Invalid token."). A plain httpx call
# carrying a valid cf_clearance now 403s ``{"message":"Missing token."}``; so does
# an in-page ``fetch`` WITHOUT the token (proven live: the gate is the token, not
# the IP/fingerprint — the same cleared+proxied client still fetches the image
# CDN, and the SPA on the same IP gets 200). The token is minted by the
# VM-obfuscated ``secure-*.js`` on the search box's keyup handler and is a
# SIGNATURE bound to the EXACT query string (replaying the captured URL with
# ``limit=6``→``18`` → 403 "Invalid token."), so it can be neither minted
# statically NOR replayed with different params. So search drives the SPA's own
# search box (the MangaFire ``fetch_via_browser_typed`` pattern, Plan 12-01): type
# the keyword, let the page mint the token + fire the XHR, read the EXACT tokenized
# URL off the Resource-Timing buffer, and replay THAT url verbatim via httpx. The
# search response is PLAINTEXT (not encrypted), so the ``result.items`` shape
# ``_result_items`` parses — incl. ``altTitles``/``hasChapters`` — is unchanged.
# The SPA autocomplete fixes ``limit=6``; ``search()`` only keeps 5 candidates
# (``_DEFAULT_SERIES_CANDIDATES``) after the prune, so 6 is sufficient.
_SEARCH_INPUT_SELECTOR = "input[placeholder*='earch']"
# Wait for the tokenized keyword XHR itself to have COMPLETED (it lands in the
# Resource-Timing buffer regardless of how many results it returned), NOT for a
# result ROW to render. A legitimately zero-result query renders no
# ``.search-pop__item--result`` row, so waiting on the row would hang to the 60s
# timeout and surface a spurious ``source_unavailable`` instead of an empty result
# (CodeRabbit, PR #235). The ``keyword=`` filter still distinguishes the search XHR
# from the homepage "latest" feed (whose ``/api/v1/manga`` call carries
# ``order[...]`` and no ``keyword=``), so the pre-rendered feed can't satisfy this
# early. Mirrors what ``_SEARCH_TOKEN_URL_EXTRACT_JS`` reads, so once this is true
# the extract has a URL to return. JS predicate (``=>``) → page.wait_for_function.
_SEARCH_REQUEST_FIRED_JS = (
    "() => performance.getEntriesByType('resource')"
    ".some(e => e.name.includes('/api/v1/manga') && e.name.includes('keyword='))"
)
# Reads the tokenized ``/api/v1/manga?keyword=…&_=…`` URL the SPA fetched off the
# Resource-Timing buffer. The box debounces, so the full-keyword XHR fires last;
# take the last matching entry. Returns ``null`` when none fired (capture failed).
_SEARCH_TOKEN_URL_EXTRACT_JS = (
    "const m = performance.getEntriesByType('resource')"
    ".map(e => e.name)"
    ".filter(n => n.includes('/api/v1/manga') && n.includes('keyword='));"
    "return m.length ? m[m.length - 1] : null;"
)
# Seconds budget for the typed search nav (type + dropdown render). Matches the
# MangaFire typed-search budget — a CF-warm page plus a debounced XHR.
_SEARCH_TYPED_TIMEOUT = 60.0

# ── Recent feed token gate (debug ``comix-chapters-token-232``, 2026-06-13) ───
# Comix also moved the recent feed ``GET /api/v1/manga?order[chapter_updated_at]=desc``
# behind the JS-minted ``_=`` request token (same SIGNATURE-over-query-string gate
# as search/chapter-list). A plain httpx call now 403s ``{"message":"Missing
# token."}``, and there is NO keyword to type. Live recon (#232) found the trigger:
# the ``/browse`` page's DEFAULT view fires the EXACT tokenized
# ``/api/v1/manga?order[chapter_updated_at]=desc&page=1&limit=28&content_rating=
# suggestive&_=<token>`` XHR. So ``recent()`` navigates ``/browse`` and PASSIVELY
# captures that tokenized URL off the Resource-Timing buffer (the same technique
# the search fix uses — NEVER mint or rewrite the token; Patchright suppresses
# init-script injection), then replays it verbatim via ``get_json_plain``. The
# PLAINTEXT response is the unchanged ``/api/v1/manga`` ``result.items`` shape, so
# the deferred-composite synthesis below is untouched. The homepage "Most Recent"
# tab is server-rendered (no tokenized XHR — recon-confirmed), so it is NOT used.
_BROWSE_PATH = "/browse"
# Reads the tokenized ``order[chapter_updated_at]`` ``/api/v1/manga`` URL off the
# Resource-Timing buffer. ``order[...]`` is URL-encoded as ``order%5B…%5D`` in the
# Resource-Timing ``name`` — match either form. Returns ``null`` when none fired.
_RECENT_TOKEN_URL_EXTRACT_JS = (
    "const m = performance.getEntriesByType('resource')"
    ".map(e => e.name)"
    ".filter(n => n.includes('/api/v1/manga')"
    " && (n.includes('order%5Bchapter_updated_at%5D')"
    " || n.includes('order[chapter_updated_at]')));"
    "return m.length ? m[m.length - 1] : null;"
)
# JS predicate: the ``/browse`` feed's tokenized XHR has resolved by the time its
# ``/title/`` cards paint, so wait for at least one to render before reading the
# Resource-Timing buffer. JS predicate (contains ``=>``) → page.wait_for_function.
_BROWSE_FEED_WAIT_FOR = (
    "() => document.querySelectorAll('a[href*=\"/title/\"]').length > 0"
)
# Seconds budget for the browse nav (CF-warm page + the feed XHR).
_RECENT_NAV_TIMEOUT = 60.0

# Composite chapter-id separator. ``chapter_id`` for Comix is the composite
# string ``"{numeric_chapter_id}|{hid}|{slug}|{chapter_number}"`` so the
# stateless ``fetch_manifest(chapter_id, ctx)`` hook can reconstruct the
# chapter URL the browser navigates to (Option A — Plan 04-04). The framework
# treats chapter_id as source-opaque, so the composite is contained.
_CID_SEP = "|"

# Issue #42: recent-feed-minted handles carry this literal in the numeric_id
# slot of the composite. ``fetch_manifest`` detects it and late-binds the real
# chapter id via :func:`_resolve_deferred` + ``_series_chapters``. The
# composite still has exactly 4 non-empty segments so the existing
# ``_parse_composite_chapter_id`` accepts it unchanged (locked decision 8).
_DEFERRED_SENTINEL = "DEFERRED"

# Page-image protection (spike-012 + spike-017-verified). A page carries AT MOST ONE
# of two header-dispatched transforms; ``fetch_image`` reverses both statically (no
# browser for image bytes):
#
#  (A) BYTE cipher — ``x-enc-seed``/``x-enc-len``/``x-enc-algo``. XOR a PRNG keystream
#      over the first ``x-enc-len`` (4096) bytes. ``x-enc-seed == 0`` ⇒ plaintext.
#        * ``x-enc-algo`` 1 (or MISSING, back-compat) → 32-bit LCG, XOR ``state>>24``.
#        * ``x-enc-algo`` 2 → xorshift32 seeded ``(seed|1)``, XOR ``state & 0xFF``.
#      Output stays a valid WebP — no re-encode.
#  (B) TILE scramble — ``x-scramble-seed``/``x-scramble-grid`` (e.g. "5x5")/
#      ``x-scramble-algo``. Bytes are ALREADY a valid WebP (so they silently pass the
#      packaging ``is_valid_image`` guard); must DECODE → seeded Fisher-Yates tile
#      un-permute → RE-ENCODE lossless.
#        * ``x-scramble-algo`` 1/2 (or MISSING) → LegacyLcg (1664525/1013904223).
#        * ``x-scramble-algo`` 3 → BuildOrderV2 = the SAME xorshift32 core as byte-2.
#
# An UNKNOWN algo (byte or scramble) FAILS LOUD rather than silently applying the wrong
# transform — PR #170 regressed (#169) precisely because it blind-applied algo-1 to
# algo-2 ciphertext. Verified end-to-end in spike 017 (decode → Pillow → coherent art).
_ENC_MASK = 0xFFFFFFFF
_ENC_MULTIPLIER = 1000005  # byte-algo-1 LCG (spike 012 / PR #170)
_ENC_INCREMENT = 1234567891
_ENC_LEN_DEFAULT = 4096
# Defensive ceiling on the decoded prefix length (T-iy5-01): a hostile/garbage
# ``x-enc-len`` must NOT be able to force a whole-image per-byte XOR loop on the
# event loop. The verified scheme length is 4096; this gives 16x headroom.
_ENC_LEN_MAX = 65536
# Byte-cipher algorithm ids (``x-enc-algo``).
_ENC_ALGO_LCG = 1
_ENC_ALGO_XORSHIFT = 2
# Tile-scramble (spike 017): LegacyLcg PRNG params + ``x-scramble-algo`` ids.
_SCRAMBLE_LCG_MULTIPLIER = 1664525
_SCRAMBLE_LCG_INCREMENT = 1013904223
_SCRAMBLE_ALGO_LEGACY_LCG = (1, 2)  # both map to the LegacyLcg PRNG
_SCRAMBLE_ALGO_BUILDORDER_V2 = 3
_SCRAMBLE_GRID_DEFAULT = (5, 5)  # C# ParseGrid default when the header is absent
# Cap the tile grid so a hostile ``x-scramble-grid`` can't force a huge tile loop.
_SCRAMBLE_GRID_MAX = 64


def _xorshift32_step(state: int) -> int:
    """One xorshift32 step (Marsaglia 13/17/5, 32-bit). Shared by byte-algo-2 (XOR the
    low byte) and scramble-algo-3/BuildOrderV2 (permutation index = ``state % n``)."""
    state ^= (state << 13) & _ENC_MASK
    state &= _ENC_MASK
    state ^= state >> 17
    state ^= (state << 5) & _ENC_MASK
    return state & _ENC_MASK


def _scramble_permutation(seed: int, count: int, algo: int) -> list[int]:
    """Seeded Fisher-Yates permutation; PRNG per ``x-scramble-algo`` (spike 017).

    ``algo`` 1/2 → LegacyLcg (1664525/1013904223); ``algo`` 3 → BuildOrderV2 xorshift32
    (seed ``| 1``). Raises ``ValueError`` on an unknown algo (caller maps to fail-loud).
    """
    if algo in _SCRAMBLE_ALGO_LEGACY_LCG:
        state = seed & _ENC_MASK

        def _next(bound: int) -> int:
            nonlocal state
            state = (
                state * _SCRAMBLE_LCG_MULTIPLIER + _SCRAMBLE_LCG_INCREMENT
            ) & _ENC_MASK
            return state % bound

    elif algo == _SCRAMBLE_ALGO_BUILDORDER_V2:
        state = (seed | 1) & _ENC_MASK

        def _next(bound: int) -> int:
            nonlocal state
            state = _xorshift32_step(state)
            return state % bound

    else:
        raise ValueError(f"unknown x-scramble-algo: {algo}")

    values = list(range(count))
    for i in range(count - 1, 0, -1):
        j = _next(i + 1)
        values[i], values[j] = values[j], values[i]
    return values


def _unscramble_image(
    content: bytes, seed: int, cols: int, rows: int, algo: int
) -> bytes:
    """Un-permute a Comix tile-scrambled page image and re-encode LOSSLESS (spike 017).

    The scrambled bytes are a valid WebP, so the gateway would otherwise package the
    visually-shuffled page. We decode with Pillow, move each scrambled tile back to its
    original grid cell (mode: "scrambled position i holds original tile permutation[i]",
    the C# reference default), and re-encode **lossless** WebP — manga pages must never
    take an added lossy requant (PKG-02). A non-image / truncated body degrades to a
    passthrough (the downstream ``is_valid_image`` guard rejects it), never raising —
    but an UNKNOWN ``x-scramble-algo`` is allowed to propagate (caller fails loud).
    """
    perm = _scramble_permutation(seed, cols * rows, algo)  # may raise on unknown algo
    try:
        with Image.open(io.BytesIO(content)) as src_img:
            img = src_img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return content
    width, height = img.size
    tile_w, tile_h = width // cols, height // rows
    if tile_w <= 0 or tile_h <= 0:
        return content  # image too small for the advertised grid — leave untouched
    out = img.copy()
    for s_idx in range(cols * rows):
        # Scrambled position ``s_idx`` holds the original tile ``perm[s_idx]`` → move it
        # back to original cell ``perm[s_idx]``. Last row/col remainder pixels (beyond
        # the fixed-size grid) are preserved by cloning ``img`` into ``out`` above.
        dst = perm[s_idx]
        sx, sy = (s_idx % cols) * tile_w, (s_idx // cols) * tile_h
        dx, dy = (dst % cols) * tile_w, (dst // cols) * tile_h
        out.paste(img.crop((sx, sy, sx + tile_w, sy + tile_h)), (dx, dy))
    buf = io.BytesIO()
    out.save(buf, format="WEBP", lossless=True, quality=100, method=6)
    return buf.getvalue()


def _make_deferred_composite(hid: str, slug: str, chapter_number: str) -> str:
    """Build a recent-feed handle's composite ``chapter_id`` (issue #42).

    Shape: ``"DEFERRED|{hid}|{slug}|{chapter_number}"``. Every segment must be
    non-empty so the existing :meth:`ComixSource._parse_composite_chapter_id`
    accepts it unchanged (locked decision 8). The ``DEFERRED`` sentinel takes
    the place of the numeric chapter id; ``fetch_manifest`` substitutes the
    real id by re-fetching the series page at download time.
    """
    if not (hid and slug and chapter_number):
        raise ValueError("hid, slug, chapter_number must all be non-empty")
    return _CID_SEP.join((_DEFERRED_SENTINEL, hid, slug, chapter_number))


class _DeferredResolutionError(Exception):
    """Internal — translated to ``SourceError('source_unavailable', ...)`` by caller.

    Raised by :func:`_resolve_deferred` when the chapter number promised by a
    recent-feed-minted handle is no longer on the series page (the chapter was
    deleted/replaced upstream) or when the matching row carries no id. The
    strict-match staleness policy (locked decision 4) requires this to surface
    as an explicit failure — never a silent rebind to a different chapter.
    """


def _id_sort_key(raw: Any) -> tuple[int, str]:
    """Sort numeric ids numerically; non-numeric ids fall back to string compare.

    Returns ``(category, value)`` so numeric ids always sort before string ids.
    Lifted verbatim from spike 002 ``deferred_resolver.py`` (test-covered).
    """
    if raw is None:
        return (2, "")
    s = str(raw)
    try:
        return (0, str(int(s)).zfill(20))
    except ValueError:
        return (1, s)


def _relative_seconds(text: str | None) -> int | None:
    """Approximate ``"Nu ago"`` text into seconds for tie-break ordering only.

    Calendar-naive; precision is not load-bearing here — only the *ordering*
    matters. Returns ``None`` for unparseable input so the tie-break treats it
    as "infinitely old." Lifted verbatim from spike 002 ``deferred_resolver``.
    """
    if not text:
        return None
    # CodeRabbit PR #50: `mos`/`mo` MUST precede `m` in the alternation — Python
    # regex alternation is left-to-right with no longest-match, so the original
    # `(s|m|h|d|w|mo|mos|y)\w*` matched `m` for "5mo ago" (with `\w*` eating "o"),
    # making `u.startswith("mo")` below unreachable and scoring months as minutes
    # (43,200× error). The richer `_RELATIVE_TIME_RE` below escapes this via
    # backtracking on its surrounding context; this simpler regex needs the
    # explicit ordering.
    m = re.match(
        r"^\s*(\d+)\s*(mos|mo|s|m|h|d|w|y)\w*\s*ago\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    n = int(m.group(1))
    u = m.group(2).lower()
    if u.startswith("mo"):
        return n * 30 * 86400
    return {
        "s": n,
        "m": n * 60,
        "h": n * 3600,
        "d": n * 86400,
        "w": n * 7 * 86400,
        "y": n * 365 * 86400,
    }.get(u)


def _pick_among_duplicates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Tie-break when multiple rows share the same chapter number (issue #42).

    Order of preference (locked decision 6):
      1. Smallest ``publishedAtRelative`` seconds (newest upload).
      2. Non-empty ``groups`` over empty.
      3. Lowest numeric ``id`` (stable, deterministic — last resort).

    Choosing newest-upload first matches the ``latestChapter`` semantics the
    recent feed already encoded — if Comix's sort key for
    ``chapter_updated_at`` picked the newest upload of that chapter, the
    resolver follows the same choice.
    """
    return min(
        rows,
        key=lambda r: (
            # CodeRabbit PR #50: guard on `is None`, not falsy — `_relative_seconds`
            # returns `int >= 0` and "0s ago" yields 0, which `or 10**12` would
            # collapse to the oldest slot, inverting the "newest wins" rule.
            (
                s
                if (s := _relative_seconds(r.get("publishedAtRelative"))) is not None
                else 10**12
            ),
            0 if r.get("groups") else 1,
            _id_sort_key(r.get("id")),
        ),
    )


def _resolve_deferred(
    chapter_number: str, series_chapters: list[dict[str, Any]]
) -> str:
    """Strict-match a recent-feed chapter_number against a series-page row list.

    Decimal-aware (locked decision 5): ``'23'`` matches series row chapter
    ``'23.0'`` and vice versa via :meth:`ComixSource._parse_decimal`. Multi-
    group ties are broken by :func:`_pick_among_duplicates` (locked decision
    6). On no-match OR a matching row without an ``id``, raises
    :class:`_DeferredResolutionError` (the caller translates it to
    ``SourceError('source_unavailable', ...)`` per locked decision 4 —
    strict-match staleness; never silently rebind).
    """
    target = ComixSource._parse_decimal(chapter_number)
    if target is None:
        raise ValueError(
            f"deferred chapter_number not Decimal-parseable: {chapter_number!r}"
        )
    matches: list[dict[str, Any]] = []
    for row in series_chapters:
        if not isinstance(row, dict):
            continue
        row_num = ComixSource._parse_decimal(row.get("chapter") or row.get("number"))
        if row_num is None:
            continue
        if row_num == target:
            matches.append(row)
    if not matches:
        raise _DeferredResolutionError(
            f"deferred resolution: chapter {chapter_number} not present on series page"
        )
    chosen = matches[0] if len(matches) == 1 else _pick_among_duplicates(matches)
    cid = chosen.get("id")
    if not cid:
        raise _DeferredResolutionError(
            f"deferred resolution: matching row carries no chapter id: {chosen!r}"
        )
    return str(cid)


# SSRF allowlist for image-CDN URLs returned by the browser-DOM extractor
# (CLAUDE.md: never fetch client-supplied / DOM-supplied URLs blindly). The JS
# regex in the extractor already enforces the `/{seg}/{token}/{NN}.{ext}` path
# shape, but it cannot tell us anything about the *host* — a poisoned DOM
# response (or a future extractor regression) could still surface a path of
# the right shape on an off-domain host. Restrict the manifest to the
# observed Comix CDN: ``https://{sub}.wowpic\d+.store/{seg}/{token}/{NN}.{ext}``.
# Subdomains seen live across recon: ``jdpw``, ``jloo``, etc. The pattern
# tolerates any non-empty alphanumeric subdomain and any wowpic shard digit.
#
# Path segment (2026-06-02): Comix ROTATES the leading path segment — observed
# ``/si/`` historically, then ``/i3/`` live (e.g.
# ``https://jloo.wowpic5.store/i3/bEqPbYfoMT0GmyXlE2KfoBZAzoUdauw/01.webp``).
# Pinning a new literal would just re-break on the next rotation, so the
# segment is WILDCARDED to a short alphanumeric run (``[a-z0-9]{2,4}``). The
# real security anchor stays the ``*.wowpic{N}.store`` HOST pin + the
# token/filename shape; the segment carries no trust. HTTPS-only, anchored
# ``^…$`` (no path traversal), image extensions only.
_COMIX_CDN_HOST_RE = re.compile(r"^[a-z0-9-]+\.wowpic\d+\.store$", re.IGNORECASE)
_COMIX_CDN_PATH_RE = re.compile(
    # Two observed shapes: the historical ``/{seg}/{token}/{NN}.{ext}`` and the
    # 2026-06 scheme ``/{seg}/{per-page-token}`` (NO filename / extension — each
    # page is one opaque token; debug comix-cdn-scheme-rotation). The trailing
    # ``/{NN}.{ext}`` is therefore OPTIONAL. The security anchors are unchanged:
    # the ``*.wowpic{N}.store`` host pin, the {16,}-char token shape, HTTPS-only,
    # anchored ``^…$`` (no traversal), and image-only extension WHEN one is present.
    r"^/[a-z0-9]{2,4}/[A-Za-z0-9_-]{16,}(?:/\d+\.(?:webp|jpg|jpeg|png))?$",
    re.IGNORECASE,
)


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` looks like a Comix CDN page image (SSRF allowlist).

    Belt-and-suspenders defense atop the JS extractor's path filter: rejects
    cross-domain hosts, non-HTTPS schemes, and anything whose path does not
    match the expected ``/{seg}/{token}/{NN}.{ext}`` shape (``{seg}`` is a
    rotating short path segment, e.g. ``si``/``i3``). Called on every URL
    returned by the browser-DOM page-list extractor before the framework
    fetches it.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and bool(_COMIX_CDN_HOST_RE.match(host))
        and bool(_COMIX_CDN_PATH_RE.match(parsed.path))
    )


# Issue #30: relative-time → approximate-ISO parser. The chapter-list DOM only
# exposes ``<span class="mchap-row__time">``'s rendered text (the upstream's
# ``createdAtFormatted`` value), never an absolute timestamp. We approximate
# the absolute date so REL-01 (``publishDate: format: date-time``) holds.
#
# Forms observed in live recon (2026-05-30 _recon_out/network.jsonl):
#   "Nh ago" / "Nd ago" / "Nw ago" / "Nmo ago" / "Nmos ago" / "Ny ago"
# We also tolerate seconds/minutes/years for forward-compatibility.
#
# Unit conversions are calendar-naive (1 month = 30 days, 1 year = 365 days) —
# the upstream rendering is itself approximate ("3mos ago"), so a calendar-
# accurate parse would overstate precision. The result is always a UTC ISO
# 8601 string with a trailing "Z" for consistency with MangaDex's RFC 3339
# emission.
_RELATIVE_TIME_RE = re.compile(
    r"""
    ^\s*
    (?P<n>\d+)              # quantity
    \s*
    (?P<unit>
        s(?:ec(?:ond)?s?)?              # s / sec / secs / second / seconds
      | m(?:in(?:ute)?s?)?              # m / min / mins / minute / minutes
      | h(?:r|rs|our|ours)?             # h / hr / hrs / hour / hours
      | d(?:ay|ays)?                    # d / day / days
      | w(?:eek|eeks|k|ks)?             # w / wk / wks / week / weeks
      | mo(?:n|ns|nth|nths|s)?          # mo / mon / mons / month / months / mos
      | y(?:r|rs|ear|ears)?             # y / yr / yrs / year / years
    )
    \s*
    ago
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _parse_relative_time(raw: str | None, *, now: datetime | None = None) -> str | None:
    """Convert ``"3d ago"`` / ``"2mos ago"`` etc. to an ISO 8601 UTC string.

    Returns ``None`` for any input that doesn't parse (empty, whitespace,
    unrecognised unit), so callers can pick an alternate (or leave the field
    empty and let REL-01 validation fail loudly). The ``now`` argument is
    test-only — production callers omit it and the parser anchors on the
    current UTC time. Calendar-naive month/year conversion is deliberate
    (see module docstring).
    """
    if not raw:
        return None
    match = _RELATIVE_TIME_RE.match(raw)
    if match is None:
        return None
    try:
        n = int(match.group("n"))
    except ValueError:  # pragma: no cover - regex already requires \d+
        return None
    unit = match.group("unit").lower()
    seconds: float
    if unit.startswith("s"):
        seconds = n
    elif unit.startswith("mo"):  # MUST precede the bare "m" branch
        seconds = n * 30 * 86400
    elif unit.startswith("m"):
        seconds = n * 60
    elif unit.startswith("h"):
        seconds = n * 3600
    elif unit.startswith("d"):
        seconds = n * 86400
    elif unit.startswith("w"):
        seconds = n * 7 * 86400
    elif unit.startswith("y"):
        seconds = n * 365 * 86400
    else:  # pragma: no cover - regex enumerates the prefixes above
        return None
    base = now if now is not None else datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    try:
        when = base - timedelta(seconds=seconds)
    except OverflowError:
        return None
    # Emit ``YYYY-MM-DDTHH:MM:SSZ`` (no microseconds) so the string parses
    # cleanly with ``datetime.fromisoformat`` AND the trailing ``Z`` matches
    # MangaDex's RFC 3339 emission for cross-source sorting in /recent.
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# JS extractor that resolves a chapter's ordered page-image URLs from comix's OWN
# internal API (spike 019), replacing the lazy-reader-DOM scrape. The chapter
# reader fetches ``GET /api/v1/chapters/{id}`` and the env module's axios instance
# (its ``ro(ri)`` interceptors) signs the ``_=`` token + decrypts the ``{"e":...}``
# envelope, returning the chapter object with ``pages:{baseUrl, items:[{width,
# height, url}]}`` — EVERY page in order, in ONE call.
#
# Why this replaced the DOM scrape (debug comix-cdn-scheme-rotation, 2026-06):
# comix changed the page-image CDN URL scheme from ``/{seg}/{token}/{NN}.{ext}``
# to ``/{seg}/{per-page-token}`` (no filename, no extension — each page a distinct
# opaque token). That broke BOTH the old extractor's ``/{NN}.{ext}`` capture regex
# (zero matches → "malformed chapter manifest") AND its Step-4 synthesis (you can
# no longer derive page N's URL by substituting a filename number — there is no
# filename). The single-page reader also keeps only ~3 imgs DOM-resident, so
# scraping every page meant scrolling N times (the #32/comix-manifest-60s-timeout
# pain). The internal-API read sidesteps all of it: it returns the full decrypted
# page list regardless of the CDN URL shape, so it is robust to the next rotation.
# The env-*.js URL hash + the axios instance are discovered at RUNTIME (never
# hardcoded). The image BYTES are still fetched via httpx (CLAUDE.md), and the
# per-page byte/tile decryption in ``fetch_image`` is unchanged. raw-string literal.
_CHAPTER_PAGES_API_EXTRACT_JS = r"""
  // (1) Discover the API-client module (env-*.js) at RUNTIME — hash rotates per
  // deploy, so NEVER hardcode it; it is already in the Resource-Timing buffer.
  const envUrl = performance.getEntriesByType('resource')
    .map(e => e.name).find(n => /\/env-[\w-]+\.js(?:\?|$)/.test(n));
  if (!envUrl) throw new Error('comix: env-*.js module URL not found');

  // (2) import() returns the LIVE cached singleton whose axios instance already
  // has comix's request-SIGN + response-DECRYPT interceptors wired (ro(ri)).
  const mod = await import(envUrl);
  const isAxios = (v) => v && (typeof v === 'object' || typeof v === 'function')
    && typeof v.get === 'function' && v.interceptors && v.interceptors.request;
  // spike 019: m.x is the axios instance (ri); fall back to scanning exports
  // (minified export names can rotate per deploy).
  const findAxios = (m) => {
    if (!m) return null;
    if (isAxios(m.x)) return m.x;
    for (const v of Object.values(m)) if (isAxios(v)) return v;
    return null;
  };
  const ax = findAxios(mod) || findAxios(mod.default);
  if (!ax) throw new Error('comix: axios instance not found in env module');

  // (3) Chapter numeric id from the path: /title/{hid}-{slug}/{id}-chapter-{number}.
  const idm = location.pathname.match(/\/(\d+)-chapter-/);
  if (!idm) throw new Error('comix: no chapter id in ' + location.pathname);

  // Timeout-only retry: comix's OWN axios instance bakes in a 15s timeout we
  // can't set. An intermittently-slow comix backend (>15s) throws AxiosError
  // 'timeout of 15000ms exceeded' (e.code === 'ECONNABORTED') — a transient
  // flake, so retry it with a small backoff. ANY OTHER error (sign/decrypt/
  // connectivity) MUST still fail-closed immediately; do NOT broaden the catch.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const MAX_RETRIES = 2;
  const RETRY_BACKOFF_MS = [500, 1000];
  const isTimeout = (e) =>
    !!e && (e.code === 'ECONNABORTED' || /timeout/i.test(String(e.message || e)));
  const withTimeoutRetry = async (fn) => {
    for (let attempt = 0; ; attempt++) {
      try {
        return await fn();
      } catch (e) {
        if (attempt < MAX_RETRIES && isTimeout(e)) {
          await sleep(RETRY_BACKOFF_MS[attempt] || 1000);
          continue;
        }
        throw e;
      }
    }
  };

  // (4) GET /chapters/{id} (axios baseURL is /api/v1) -> the decrypted chapter
  // with pages:{baseUrl, items:[{url}]}. A thrown request fails closed (the extract
  // throws → fetch_manifest raises → SourceError), never a partial manifest.
  const res = await withTimeoutRetry(() => ax.get('/chapters/' + idm[1]));
  const data = (res && res.data !== undefined) ? res.data : res;
  const pages = data && data.pages;
  const items = (pages && Array.isArray(pages.items)) ? pages.items
    : (Array.isArray(data && data.images) ? data.images
      : (Array.isArray(pages) ? pages : []));
  const baseUrl = (pages && typeof pages.baseUrl === 'string') ? pages.baseUrl : '';

  // (5) Ordered absolute URLs. Items may be strings or {url|src}; a relative url
  // is prefixed with baseUrl. SSRF + extension are re-checked Python-side
  // (_is_allowed_image_url) before any byte fetch.
  const urls = [];
  for (const it of items) {
    let u = (typeof it === 'string') ? it : (it && (it.url || it.src));
    if (typeof u !== 'string' || !u) continue;
    if (baseUrl && !/^https?:\/\//i.test(u)) u = baseUrl + u;
    urls.push(u);
  }
  return urls;
"""

# CSS selector ``solver.fetch_via_browser`` waits for before the chapter-pages
# extract runs. The reader scaffolds `<div class="rpage-page" data-page="N">`
# once it has fetched + decrypted the page list — which means the SPA's API ES
# module is loaded and its axios interceptors are wired, exactly what
# ``_CHAPTER_PAGES_API_EXTRACT_JS`` needs before it ``import()``s the module and
# calls ``/chapters/{id}`` itself (we read the API, not the rendered <img>s).
_CHAPTER_PAGES_WAIT_FOR = ".rpage-page[data-page]"

# Issue #171: how many times fetch_manifest navigates the chapter page before
# giving up on an EMPTY capture. A cold Patchright context (right after a restart
# / Cloudflare re-warm) can race the page's lazy-loaded <img>s — the extractor's
# two-scroll capture sees ``seen.size === 0`` and deliberately returns [] (a real
# chapter manifest is never legitimately empty). One bounded re-navigation warms
# the context so the imgs populate; 2 attempts total (1 retry).
_MANIFEST_COLD_RACE_ATTEMPTS = 2

# JS extractor that enumerates the FULL chapter list via comix's OWN internal API
# loader (spike 019), replacing the slow browser-DOM "Next-walk". The chapter-list
# endpoint ``/api/v1/manga/{hid}/chapters`` is gated by a JS-minted ``_=`` request
# signature (binds page+limit since #232) AND returns an encrypted ``{"e":...}``
# envelope decrypted only in-page by the VM-obfuscated ``secure-*.js`` — judged
# un-crackable statically (spikes 010/012/015). Spike 019's bypass: don't crack the
# VM, call comix's own loader. The API-client ES module (``env-*.js``) wires the
# request-SIGN + response-DECRYPT interceptors onto its axios instance (``ro(ri)``);
# ES modules are cached singletons, so ``await import('<env-*.js url>')`` returns the
# LIVE warm instance and ``<api>.chapters(hid, {limit:100})`` signs limit=100 AND
# returns DECRYPTED rows (proven live: 100 rows in ~511ms). Dynamic same-origin
# import is NOT CSP-blocked from page.evaluate (unlike the init scripts Patchright
# suppresses). limit=100 cuts One Piece from ~236 pages (limit=20 DOM walk) to ~47,
# each a ~500ms in-page API call fanned out (bounded) in ONE warm tab.
#
# Emits the SAME ``{id, chapter, lang, groups, publishedAtRelative, likes, volume}``
# row shape the prior DOM extractor produced, so the ``_to_release`` consumer and the
# newest-first sort / enum-cache wrapping in ``_fetch_series_chapters_raw`` are
# unchanged. The env-*.js URL hash and minified export names can rotate per deploy,
# so both are discovered at RUNTIME (Resource-Timing buffer + export scan), never
# hardcoded. raw-string literal — the JS regexes use single-backslash escapes.
_CHAPTER_LIST_API_EXTRACT_JS = r"""
  // Bounded parallel page-fetch tuning. limit=100 (comix signs the token for any
  // limit); CONCURRENCY caps simultaneous in-page API calls; MAX_PAGES is a
  // runaway guard (200*100 = 20k rows — far past comix's longest series).
  const LIMIT = 100;
  const CONCURRENCY = 8;
  const MAX_PAGES = 200;

  // (1) Discover the API-client module (env-*.js) URL at RUNTIME — the hash
  // changes per deploy, so NEVER hardcode it. It is in the Resource-Timing buffer
  // because the SPA already loaded it on boot.
  const envUrl = performance.getEntriesByType('resource')
    .map(e => e.name)
    .find(n => /\/env-[\w-]+\.js(?:\?|$)/.test(n));
  if (!envUrl) throw new Error('comix: env-*.js module URL not found');

  // (2) import() returns the LIVE cached singleton — its axios instance already
  // has comix's own request-SIGN + response-DECRYPT interceptors wired (ro(ri)),
  // so chapters() signs the _= token for limit=100 AND decrypts the {"e":...}
  // envelope for free. Dynamic same-origin import is NOT CSP-blocked here.
  const mod = await import(envUrl);

  // (3) Find the manga API object (exposes chapters(hid, params)). Prefer the
  // proven `c` export; fall back to scanning exports (minified names can rotate).
  const findApi = (m) => {
    if (!m) return null;
    if (m.c && typeof m.c.chapters === 'function') return m.c;
    for (const v of Object.values(m)) {
      if (v && typeof v === 'object' && typeof v.chapters === 'function') return v;
    }
    return null;
  };
  const api = findApi(mod) || findApi(mod.default);
  if (!api) throw new Error('comix: chapters() API not found in env module');

  // (4) Series hid from the path (/title/{hid}-{slug}); hids carry no hyphen.
  const hm = location.pathname.match(/\/title\/([^/-]+)/);
  if (!hm) throw new Error('comix: could not read hid from ' + location.pathname);
  const hid = hm[1];

  // Timeout-only retry: comix's OWN axios instance bakes in a 15s timeout we
  // can't set. When comix's backend is intermittently slow (>15s for one page)
  // the call throws AxiosError 'timeout of 15000ms exceeded' (e.code ===
  // 'ECONNABORTED'). That is a transient flake — retry it with a small backoff.
  // ANY OTHER error (token/sign failure, decrypt failure, connectivity) MUST
  // still fail-closed immediately (no error masking — a partial chapter list is
  // never returned). Do NOT broaden this catch beyond the axios-timeout class.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const MAX_RETRIES = 2;
  const RETRY_BACKOFF_MS = [500, 1000];
  const isTimeout = (e) =>
    !!e && (e.code === 'ECONNABORTED' || /timeout/i.test(String(e.message || e)));
  const withTimeoutRetry = async (fn) => {
    for (let attempt = 0; ; attempt++) {
      try {
        return await fn();
      } catch (e) {
        if (attempt < MAX_RETRIES && isTimeout(e)) {
          await sleep(RETRY_BACKOFF_MS[attempt] || 1000);
          continue;
        }
        throw e;
      }
    }
  };

  const getPage = (p) =>
    withTimeoutRetry(() =>
      api.chapters(hid, { page: p, limit: LIMIT, order: { number: 'desc' } }));
  const rowsOf = (res) => {
    if (!res) return [];
    if (Array.isArray(res)) return res;
    return res.items || (res.result && res.result.items) || res.data || [];
  };
  const num = (o, ks) => {
    if (!o) return null;
    for (const k of ks) {
      const v = o[k];
      if (v != null && Number.isFinite(+v)) return +v;
    }
    return null;
  };

  const byId = new Map();
  const add = (rows) => {
    for (const it of rows) {
      if (!it || it.id == null || it.number == null) continue;
      if (byId.has(it.id)) continue;
      byId.set(it.id, {
        id: String(it.id),
        chapter: String(it.number),
        lang: it.language || 'en',
        groups: (it.group && it.group.name) ? [{ name: it.group.name }] : [],
        publishedAtRelative: it.createdAtFormatted || null,
        likes: (typeof it.votes === 'number') ? it.votes : null,
        volume: (it.volume != null) ? it.volume : null
      });
    }
  };

  // (5) Page 1 — NO catch: a real connectivity/token/decrypt failure here must
  // surface (fail-closed; a partial chapter list is never returned).
  const first = await getPage(1);
  const firstRows = rowsOf(first);
  add(firstRows);

  // (6) Determine the last page P from pagination meta when the decrypted
  // response carries it (Laravel-style: last_page / total). meta may sit at the
  // top level, under .meta, or under .result(.meta).
  const meta =
    (first && (first.meta || (first.result && (first.result.meta || first.result)))) ||
    first ||
    {};
  let lastPage = num(
    meta, ['lastPage', 'last_page', 'pages', 'totalPages', 'total_pages']);
  if (lastPage == null) {
    const total = num(meta, ['total', 'totalCount', 'total_count', 'count']);
    if (total != null) lastPage = Math.max(1, Math.ceil(total / LIMIT));
  }

  if (lastPage != null) {
    // Exact, fail-closed: fan pages 2..P out in bounded chunks (no overshoot, no
    // error masking — a thrown page rejects Promise.all and fails the whole read).
    const P = Math.min(lastPage, MAX_PAGES);
    for (let start = 2; start <= P; start += CONCURRENCY) {
      const batch = [];
      for (let p = start; p < start + CONCURRENCY && p <= P; p++) batch.push(p);
      const fetched = await Promise.all(batch.map(p => getPage(p).then(rowsOf)));
      for (const rows of fetched) add(rows);
    }
  } else if (firstRows.length >= LIMIT) {
    // Degraded fallback (no pagination meta): wave-probe pages 2.. until a page
    // returns < LIMIT rows. Tolerate per-page rejection past the end (overshoot)
    // by treating it as an empty page, so a healthy long series still completes.
    let next = 2;
    let done = false;
    while (!done && next <= MAX_PAGES) {
      const batch = [];
      for (let p = next; p < next + CONCURRENCY && p <= MAX_PAGES; p++) batch.push(p);
      const fetched = await Promise.all(
        batch.map(p => getPage(p).then(rowsOf).catch(() => []))
      );
      for (const rows of fetched) {
        add(rows);
        if (rows.length < LIMIT) done = true;
      }
      next += CONCURRENCY;
    }
  }

  return Array.from(byId.values());
"""

# CSS selector ``solver.fetch_via_browser`` waits for before reading the series
# page DOM — any anchor whose href contains ``-chapter-``. Once at least one
# such anchor has rendered, the chapter-list SPA component has hydrated.
# JS predicate (not CSS selector — routes to page.wait_for_function): chapter
# anchors carry class ``mchap-row__primary`` once the live recon confirmed the
# series-page reader rendered them. We poll DOM attachment (not visibility)
# because some anchors render off-screen / inside scroll containers and the
# default CSS-selector wait_for would block on visibility forever (the e2e
# test uncovered this — a[href*="-chapter-"] timed out at 20s).
_CHAPTER_LIST_WAIT_FOR = (
    "() => document.querySelectorAll('a.mchap-row__primary').length > 0"
)


def _title_to_slug(title: str) -> str:
    """Best-effort title → URL slug fallback (lowercase + hyphenate non-alnum runs).

    Used only when the search item is missing a ``url`` field (defensive — the
    live ``/api/v1/manga`` response always carries one). Idempotent; never
    raises; collapses runs of non-alnum to single ``-`` and strips leading/
    trailing hyphens.
    """
    out: list[str] = []
    in_dash = True  # treat leading non-alnum as a leading dash to be stripped
    for ch in title.lower():
        if ch.isalnum():
            out.append(ch)
            in_dash = False
        elif not in_dash:
            out.append("-")
            in_dash = True
    slug = "".join(out).rstrip("-")
    return slug or "manga"


class ComixSource(Source):
    """Comix — antibot ``cloudflare+encrypted`` (SRC-06).

    Comix's live read path is Option A browser-DOM (``solver.fetch_via_browser``)
    + plaintext httpx (``get_json_plain``/``get_bytes_plain``), so the framework
    decrypt seam is never invoked here. ``decrypt_scheme`` is inherited as
    ``None`` from the base.
    """

    key = "comix"
    name = "Comix"
    base_url = "https://comix.to"
    # Title-search fallback only — no external id namespace (SRCH-07).
    id_types: list[str] = []
    languages = ["en"]
    # Probe-measured (PR #102 rate-limit probe, 2026-06-03), NOT the conservative
    # CLAUDE.md "Comix ~10" guess: comix's PLAINTEXT search API + image CDN each
    # sustained 240 calls/min at concurrency 4 with ZERO throttle signals (no
    # 429/403/CF-challenge/Retry-After, flat latency) — a FLOOR ("no limit hit
    # across the full tested grid"), not a true ceiling. 120 is the harness's
    # conservative ~50%-of-floor OVERALL SUGGESTED value. The per-source
    # aiolimiter (AsyncLimiter token bucket) is keyed to this attr and gates ONLY
    # the plaintext call path; the browser-nav manifest read is gated by
    # ``cloudflare_fetch_concurrency`` and image bytes are
    # ``get_bytes(limited=False)``-exempt.
    rate_limit_per_minute = 120
    # caps.AntibotLevel already carries this literal (CAPS-02). The framework injects
    # clearance (D-40) + reconciles a challenge 403 (D-35) for any cloudflare* source.
    antibot = "cloudflare+encrypted"
    # The URL the framework solver navigates to so Cloudflare issues a
    # ``cf_clearance`` cookie. Read by the application wiring (app.py lifespan),
    # not by the framework solver itself.
    cloudflare_challenge_url = "https://comix.to/"
    # Issue #42: flipped from False — recent uses a plaintext one-call feed
    # with deferred chapter-id resolution; see ``recent()`` below. Late-binding
    # of the chapter id happens in ``fetch_manifest`` (one extra browser nav
    # per first download), keeping the per-poll cost to one cheap plaintext
    # call (1 of 120/min rate-limit budget) rather than the N+1 series-page
    # drill-down PR #41 originally rejected as too expensive.
    supports_recent = True

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Title-search → series candidates → chapter-list enumeration (SRCH-01..07).

        Comix has no external id namespace (``id_types == []``), so this is always
        the title-search path: resolve candidate series for the query (PLAINTEXT
        ``/api/v1/manga`` per live recon), then enumerate each series' chapter
        list via a browser-DOM read of the series page (Option A, Plan 04-04).

        Identical contract shape to MangaDex (one Release per chapter upload, an
        opaque ``comix:`` handle minted per release). The encrypted
        ``/api/v1/manga/{hid}/chapters`` endpoint is bypassed for the same
        Option A reason as the chapter-pages endpoint: rather than maintain two
        decrypt paths (one statically-decrypted list endpoint and one
        browser-driven pages endpoint), funnel both through the warm Patchright
        page that the SPA already drives correctly.

        Concurrency: the per-candidate ``_series_chapters`` browser
        navigations are awaited CONCURRENTLY via :func:`asyncio.gather` so
        /search's wall-clock is bounded by ``max(individual)`` rather than
        ``sum(individual)``. The fan-out is bounded entirely by the framework's
        existing ``CloudflareSolver._browser_lock`` — an
        ``asyncio.Semaphore(cloudflare_fetch_concurrency)`` — so it self-throttles
        to whatever the deployment configures; this source adds no second gate.
        ``return_exceptions=True`` isolates per-candidate failures: a single
        ``_series_chapters`` raise surfaces as an item in the result list (logged
        at WARNING and skipped), so one stale/missing series does not blank the
        whole search response. ``asyncio.gather`` preserves coroutine-launch
        order, so the returned ``releases`` keep the relevance-sorted candidate
        ordering the upstream ``/api/v1/manga`` already provides.

        Concurrency safety is ENGINE-specific (debug session
        ``comix-parallel-engine-probe``, 2026-06-01). With the default
        ``cloudflare_fetch_concurrency=1`` the Semaphore admits one nav at a
        time, so this is behaviourally identical to the historic sequential
        loop — zero change for the default deployment. Parallelism is OPT-IN:
        bumping the cap is only safe on ``engine=patchright`` (Chromium), which
        runs N concurrent CF navigations on one warm context cleanly (4/4 proven
        on residential-IP Windows + Linux, and through a residential proxy).
        ``engine=camoufox`` (Firefox) STALLS such navigations at goto-commit, so
        ``cloudflare_fetch_concurrency`` MUST stay at 1 there — the earlier
        "Cloudflare per-IP burst" diagnosis (issue #59) was an artifact of
        testing only Camoufox and is REFUTED. See ``config.py`` and the README
        for the engine/concurrency constraint.
        """
        # Comix is English-only (live recon); ``languages`` is honored downstream
        # and is the language half of both cache keys (T-09-01).
        languages = req.languages or []
        count = _DEFAULT_SERIES_CANDIDATES

        # Layer 1 (D-01): cache the title → pruned-candidate resolution so a repeat
        # search on the same (query, languages) skips the PLAINTEXT ``/api/v1/manga``
        # call entirely (genuinely zero upstream calls on a HIT). The key normalizes
        # the query the SAME way the relevance scorer does so punctuation/case
        # variants collapse onto one entry.
        async def _resolve_fn() -> list[
            tuple[str, str, str, list[str], dict[str, Any] | None]
        ]:
            found = await self._search_series(req.query or "", count, ctx)
            # Prune obviously-irrelevant candidates BEFORE the per-candidate browser
            # chapter fan-out (#126) — each wasted candidate is a 7-18s nav (#101).
            # An exact-match query narrows to the one correct series; ambiguous
            # queries fan out to ``count`` (the prune only narrows, never widens).
            # ``count`` is mode-invariant at 5 (#162) — interactive no longer widens it.
            return prune_candidates(
                found,
                req.query or "",
                # Score over the series title (t[2]) OR any alt title (t[3]) (#139):
                # a query matching only the native/alt name still prunes to it.
                keys=lambda t: [t[2], *t[3]],
                cap=count,
            )

        # WR-01: the search mode no longer affects candidate width — ``count`` is
        # mode-invariant at 5 (#162), so the resolved candidate set is identical
        # whether or not ``req.interactive`` is set. The resolve key is therefore
        # mode-agnostic ``(source_key, normalized_query, languages)`` with NO
        # candidate-count discriminator, so a mode flip (interactive↔non-interactive)
        # for a warmed query is a HIT, not a deliberate MISS — there is no width
        # difference to reconcile, so no wrong-width list can be served.
        series = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(req.query or ""), languages),
            _resolve_fn,
        )
        # 260605-e9a deliverable 5: report how many series candidates we deep-
        # enumerate (one browser fan-out each; correct on a HIT too).
        ctx.candidates_enumerated = len(series)

        # Per-series OUTPUT window — honors the CALLER's requested limit, NOT the
        # per-page upstream fetch ceiling. ``_MAX_FEED_LIMIT`` is the page size the
        # ``route_limit_rewrite`` requests from upstream (the ``/chapters`` API), so
        # reusing it to clamp the result window capped EVERY comix search at 100
        # chapters regardless of ``req.limit`` (debug comix-page-walker-100-cap).
        # The canonical ``req.limit`` truncation is the route's
        # ``releases[: req.limit]`` over the merged, newest-first result (the same
        # point MangaDex relies on) — the source must NOT pre-clamp to the fetch
        # ceiling. Per-series windowing to ``req.limit`` still bounds handle minting
        # (the merged top-``limit`` can never need more than ``limit`` from any one
        # series) without starving a >100-chapter series.
        #
        # 260620-ki0 (CodeRabbit): clamp non-negative. A negative ``req.limit`` would
        # otherwise leave ``result_window`` negative and turn the
        # ``chapters[: offset + result_window]`` bound into a Python negative slice,
        # minting all-but-last as dropped handles even though the route clamps limit
        # and returns an empty page. A zero limit windows to 0 (route returns empty).
        result_window = max(req.limit, 0)

        # Layer 2 (CACHE-02/03): cache the UNFILTERED, newest-first raw chapter list
        # per (series_hid, languages). The browser-DOM read is the SINGLE biggest cost
        # on this source (7-18s/nav), so a HIT here is the headline win — a repeat
        # same-series chapter search skips the navigation entirely. The DOM read is the
        # COMPLETE v1 enumeration (there is no deeper-limit fetch on Comix), so the
        # Enumeration is marked ``exhausted=True`` — ``covers_floor`` therefore never
        # forces a pointless re-nav for a below-window chapter.
        async def _enum_for(series_hid: str, series_slug: str) -> Enumeration:
            async def _enum_fn() -> Enumeration:
                items = await self._fetch_series_chapters_raw(
                    series_hid, series_slug, ctx
                )
                parsed = [
                    d
                    for c in items
                    if (d := self._parse_decimal(c.get("chapter") or c.get("number")))
                    is not None
                ]
                return Enumeration(
                    items=items,
                    chapter_numbers=tuple(parsed),
                    exhausted=True,
                    # The per-page upstream fetch ceiling (the route_limit_rewrite
                    # target), NOT the output window — the browser walk is the
                    # COMPLETE enumeration so this is informational only
                    # (``covers_floor`` short-circuits on ``exhausted=True``).
                    requested_limit=_MAX_FEED_LIMIT,
                )

            # IN-02: Comix's chapter enumeration is language-AGNOSTIC — the
            # browser-DOM read is English-only and the language filter is applied
            # post-cache (in ``search()``), so the cached walk does not depend on
            # ``languages``. Key on ``[]`` so different-language requests for the
            # same series share the one cached (expensive) browser walk.
            return await ctx.cached_enumerate(
                ctx.cached_enumerate_key(series_hid, []), _enum_fn
            )

        # Parallel fan-out across series candidates. ``return_exceptions=True``
        # isolates per-candidate failures — a SourceError on one candidate
        # surfaces as an item in ``results`` rather than cancelling the gather,
        # so the other candidates still flow. The actual concurrency is bounded
        # by the framework's ``CloudflareSolver._browser_lock`` Semaphore
        # (``cloudflare_fetch_concurrency``), so the default deployment
        # (concurrency=1) runs these one-at-a-time exactly as before. A cache HIT
        # never reaches the solver, so concurrent same-series misses collapse to ONE
        # nav via the SingleFlightCache (D-04) on top of that Semaphore.
        coros = [
            _enum_for(series_hid, series_slug)
            for series_hid, series_slug, _series_title, _alt, _links in series
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        releases: list[Release] = []
        for (series_hid, series_slug, series_title, _alt, _links), result in zip(
            series, results, strict=True
        ):
            if isinstance(result, BaseException):
                # One bad candidate must not blank the whole search response.
                # The exception is logged at WARNING with the hid+slug so a
                # systemic issue (Comix DOM change, regex drift) is still
                # visible in ops dashboards. Other candidates continue to flow.
                _log.warning(
                    "comix search: candidate %r (slug=%r) failed: %s",
                    series_hid,
                    series_slug,
                    result,
                )
                continue
            # CACHE-02: the ``chapter_matches`` floor filter lives in search() at the
            # enumeration boundary (never the engine/cache) — keep only the requested
            # whole-number/floor family BEFORE the offset/feed_limit window so matches
            # are not starved (gate-off type=manga = pass-through). This mirrors the
            # filter+slice the pre-cache ``_series_chapters`` applied, now over the
            # cached raw rows.
            chapters = [
                c
                for c in result.items
                if self.chapter_matches(
                    req, self._parse_decimal(c.get("chapter") or c.get("number"))
                )
            ]
            # Phase-13 (R2/R4/R5): the series' tracker links rode the SAME cached
            # Layer-1 candidate tuple (``_links`` = the search payload's
            # ``result.items[].links``, full tracker URLs — Pitfall 4), so we stash
            # the raw dict on the per-request scratch map (ZERO added HTTP — no second
            # ``/api/v1/manga`` call). ``resolve_external_links`` then parses it ONCE
            # per series (the framework owns the cache/timeout/swallow), and the single
            # resolved object is stamped onto every release below (identical-object,
            # R4). The default-arg idiom binds ``series_hid`` to dodge the loop-var
            # closure bug.
            if _links is not None:
                ctx.external_links_raw[series_hid] = _links

            async def _parse_links(_hid: str = series_hid) -> ExternalLinks | None:
                return await self.fetch_external_links(_hid, ctx)

            links_obj = await ctx.resolve_external_links(series_hid, _parse_links)

            series_releases: list[Release] = []
            # 260620-ki0: req.offset is NO LONGER consumed per-source — offset is now a
            # ROUTE concern (search.py pages the cross-source merged newest-first list).
            # The per-series window is kept only as a handle-minting bound, but widened
            # to offset+result_window so the route's merged window
            # [offset:offset+limit] can never be starved of items that belong to it
            # from THIS series (a single series can contribute at most offset+limit
            # items to the global newest-first window). result_window
            # (= req.limit or _MAX_FEED_LIMIT) is unchanged.
            for chapter in chapters[: max(req.offset, 0) + result_window]:
                # Inject the series-page-known title into the chapter dict so the
                # SOURCE-AGNOSTIC ``_to_release`` (which reads ``seriesTitle`` /
                # ``series`` / ``title`` keys) does not need to know whether the
                # data came from the browser DOM or the legacy encrypted API. Each
                # surviving chapter mints a FRESH handle per serve (CACHE-03/05) — the
                # cache stores only raw rows, never minted handles.
                if "seriesTitle" not in chapter and series_title:
                    chapter = {**chapter, "seriesTitle": series_title}
                rel = self._to_release(series_hid, series_slug, chapter, ctx)
                if rel is not None:
                    series_releases.append(rel)
            # Stamp the single resolved ExternalLinks (or None) onto every release of
            # the series — all N share the identical object (R4).
            for rel in series_releases:
                rel.external_links = links_obj
            releases.extend(series_releases)
        return releases

    # IN-02: unlike the other five sources, comix recent() DOES populate
    # Release.externalLinks — the tokenized /api/v1/manga feed carries the same
    # per-item ``links`` dict search uses, so they ride through with NO added HTTP
    # (WR-01).
    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        """Newest-first recent chapters via the list-mangas feed (RCNT-01/02).

        Issue #42 (supersedes #31): synthesizes one ``Release`` per viable item
        from the ``/api/v1/manga?order[chapter_updated_at]=desc`` feed. #232: that
        feed is now ``_=``-token-gated (a plain httpx call 403s "Missing token."),
        so instead of a direct ``get_json_plain`` we navigate ``/browse`` in the
        warm browser, PASSIVELY capture the tokenized feed URL the SPA mints off
        the Resource-Timing buffer, and replay it verbatim (see ``_BROWSE_PATH`` /
        ``_RECENT_TOKEN_URL_EXTRACT_JS``). The plaintext response shape is
        unchanged, so the per-item synthesis below is identical. Each Release
        carries a ``:DEFERRED`` guid suffix and a deferred composite ``chapter_id``
        whose numeric id is late-bound by :meth:`fetch_manifest` at download time
        (one extra browser nav per FIRST download of a recent-minted Release — not
        per ``/recent`` poll).

        Recent and search Releases for the same chapter intentionally do not
        dedup — they are different objects (a late-binding promise vs a
        concrete upload). See locked decisions 1, 3, 7 in the PLAN.

        ``since``, ``languages`` and ``limit`` are noted unused: Comix is
        English-only (live recon); ``since`` is enforced upstream by the
        route-level cut already; and the ``/browse`` feed's page size is
        SPA-fixed (``limit=28``, token-bound — we cannot vary it without
        invalidating the signature), so the route's merged-list
        ``releases[:limit]`` cut (recent.py) is what honors the caller's limit.
        Items missing ``hid``, ``hasChapters: false``, an unparseable
        ``latestChapter``, no slug, or an unparseable
        ``chapterUpdatedAtFormatted`` are SKIPPED rather than faked (REL-01
        requires ``format: date-time``).
        """
        _ = (languages, since, limit)  # see docstring — all deliberately unused
        # #232: capture the SPA-minted tokenized recent-feed URL off /browse and
        # replay it verbatim (the token is a signature over the EXACT query string,
        # so no param is added or rewritten). NEVER mint/rewrite the token.
        solver = self._solver_from_ctx(ctx)
        token_url = await solver.fetch_via_browser(
            f"{self.base_url}{_BROWSE_PATH}",
            extract=_RECENT_TOKEN_URL_EXTRACT_JS,
            wait_for=_BROWSE_FEED_WAIT_FOR,
            timeout=_RECENT_NAV_TIMEOUT,
        )
        if not isinstance(token_url, str) or "/api/v1/manga" not in token_url:
            # The browse nav ran but no tokenized recent XHR was captured — surface
            # as the source's own failure (WR-06 per-source warning), not a crash.
            raise SourceError(
                "source_unavailable",
                "comix recent token capture failed (no tokenized "
                "order[chapter_updated_at] /api/v1/manga URL)",
            )
        # PLAINTEXT endpoint — replay the tokenized URL verbatim (same path the
        # search fix uses); the unchanged ``result.items`` parse follows.
        data = await ctx.get_json_plain(token_url)
        items = self._result_items(data)

        releases: list[Release] = []
        for item in items:
            if not isinstance(item, dict) or item.get("hasChapters") is False:
                continue
            hid = item.get("hid")
            if not hid:
                continue
            ch_dec = self._parse_decimal(item.get("latestChapter"))
            if ch_dec is None:
                continue
            slug = self._slug_from_item(item)
            if not slug:
                continue
            publish_date = _parse_relative_time(item.get("chapterUpdatedAtFormatted"))
            if not publish_date:
                continue  # REL-01 requires format: date-time — skip rather than fake
            # Decimal-normalize per locked decision 5: ``Decimal('23')`` ->
            # ``"23"`` (not ``"23.0"``), ``Decimal('1.20')`` -> ``"1.2"``.
            ch_str = format(ch_dec.normalize(), "f")
            series_title = str(item.get("title") or "Unknown")
            language = "en"
            composite = _make_deferred_composite(str(hid), slug, ch_str)
            # Phase-13 (WR-01): the tokenized ``/api/v1/manga`` feed carries the SAME
            # per-item ``links`` tracker dict that ``search`` stashes — so the recent
            # path can populate ``externalLinks`` with ZERO added HTTP by routing it
            # through the identical parse-only normalize seam. Stash the raw dict, then
            # resolve ONCE per series via the framework (cache/timeout/swallow owner);
            # each recent item is a distinct series so this is one resolve per row.
            links_raw = item.get("links")
            if isinstance(links_raw, dict) and links_raw:
                ctx.external_links_raw[str(hid)] = links_raw

            async def _parse_links(_hid: str = str(hid)) -> ExternalLinks | None:
                return await self.fetch_external_links(_hid, ctx)

            ext_links = await ctx.resolve_external_links(str(hid), _parse_links)
            title = self._build_title(
                series_title, ch_str, volume=None, language=language, group=None
            )
            # Locked decision 1: literal ``:DEFERRED`` suffix in the guid's
            # chapter_id segment. NEVER rewrite this when the composite
            # resolves at download time — recent and search intentionally do
            # not dedup (different resolution states). The search-path guid
            # shape ``…:{chapter_id}`` stays unchanged for D-21 multi-group
            # uniqueness.
            guid = f"comix:{hid}:ch-{ch_str}:{language}:DEFERRED"
            handle = ctx.handle_store.mint(
                ResolutionRecord(
                    source_key=self.key,
                    chapter_id=composite,
                    language=language,
                    title=title,
                    manga_title=series_title,
                    chapter_number=ch_dec,
                    volume=None,
                    scanlation_group=None,  # populated at download-resolve
                    page_count=None,  # populated at download-resolve
                )
            )
            releases.append(
                Release(
                    guid=guid,
                    title=title,
                    source_key=self.key,
                    download_handle=handle,
                    publish_date=publish_date,
                    manga_title=series_title,
                    chapter_number=ch_dec,
                    volume=None,
                    language=language,
                    scanlation_group=None,
                    page_count=None,
                    # votes intentionally None on the recent path: the
                    # /api/v1/manga series-list item carries no per-chapter
                    # likes (likes live only on the chapter-list DOM row).
                    ids={"comixSeriesId": str(hid)},
                    external_links=ext_links,
                )
            )
        return releases

    # ───────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered page-image URLs, INTERNALLY (PKG-01/R6).

        Spike 019 (2026-06): navigate the chapter page in the warm Patchright
        browser, then run comix's OWN internal ``chapters/{id}`` API loader in-page
        (``_CHAPTER_PAGES_API_EXTRACT_JS``) to get the decrypted page list. The env
        module's axios instance (``ro(ri)``) signs the ``_=`` token + decrypts the
        ``{"e":...}`` envelope, returning ``pages.items[].url`` — every page in one
        call. This replaced the prior lazy-reader-DOM scrape, which broke when comix
        rotated its image-CDN URL scheme from ``/{seg}/{token}/{NN}.{ext}`` to
        ``/{seg}/{per-page-token}`` (no filename → the capture regex matched nothing
        AND filename-substitution synthesis became impossible; debug
        comix-cdn-scheme-rotation). The API read is robust to that rotation — it
        returns whatever URL comix serves.

        Composite-id contract: ``chapter_id`` is the
        ``"{numeric_id}|{hid}|{slug}|{number}"`` composite the search step
        encoded into the handle's ``ResolutionRecord.chapter_id``. We decode
        here, construct the live chapter URL, and call
        :meth:`solver.fetch_via_browser` with the internal-API extractor. A
        malformed composite or an empty page list raises
        ``SourceError("source_unavailable")`` so it surfaces as a contract
        warning, never a raw KeyError (WR-06). The manifest is consumed only by
        the gateway's own engine — never returned to a caller (R6).

        The image-byte fetch (the next step in the engine) still runs through
        httpx (``ctx.get_bytes``) — the browser is NEVER used for bulk image
        fetch (CLAUDE.md).

        Issue #42 — composites whose ``numeric_id == 'DEFERRED'`` are recent-
        feed-minted handles that re-resolve their chapter id at download time
        via :meth:`_series_chapters` + :func:`_resolve_deferred`; one extra
        browser nav on the first download per recent-minted Release. The
        resolved id is local to this call; the ``ResolutionRecord`` stored
        under the opaque handle is NOT mutated (locked decision 1 — the
        ``:DEFERRED`` guid suffix is permanent).
        """
        try:
            numeric_id, hid, slug, number = self._parse_composite_chapter_id(chapter_id)
        except ValueError as exc:
            raise SourceError(
                "source_unavailable", f"malformed comix chapter id: {exc}"
            ) from None
        # Issue #42: recent-feed handles defer chapter-id resolution to download
        # time. One extra browser nav (re-reads the series page chapter list,
        # the same path search uses) replaces the DEFERRED sentinel with the
        # real numeric id. Strict-match staleness (locked decision 4): a
        # missing chapter surfaces as SourceError, never a silent rebind.
        if numeric_id == _DEFERRED_SENTINEL:
            chapters = await self._series_chapters(hid, slug, _MAX_FEED_LIMIT, 0, ctx)
            try:
                numeric_id = _resolve_deferred(number, chapters)
            except _DeferredResolutionError as exc:
                raise SourceError("source_unavailable", str(exc)) from None
        solver = self._solver_from_ctx(ctx)
        chapter_url = (
            f"{self.base_url}/title/{hid}-{slug}/{numeric_id}-chapter-{number}"
        )
        # Issue #171: a cold browser can race the page's readiness and yield an
        # EMPTY page list. Retry only that signature once (a warm re-nav lets the
        # SPA module load + interceptors wire); a genuinely empty chapter returns
        # [] again and falls through to the malformed-manifest raise, while a
        # populated-but-invalid manifest is a real fault that does NOT retry.
        urls: Any = None
        for attempt in range(_MANIFEST_COLD_RACE_ATTEMPTS):
            try:
                urls = await solver.fetch_via_browser(
                    chapter_url,
                    # spike 019: call comix's OWN internal ``chapters/{id}`` loader
                    # in-page — returns the decrypted ``pages.items[].url`` list,
                    # robust to the image-CDN URL-scheme rotation that broke the old
                    # lazy-DOM scrape (debug comix-cdn-scheme-rotation).
                    extract=_CHAPTER_PAGES_API_EXTRACT_JS,
                    # Wait for the reader scaffold so the SPA has fetched+decrypted
                    # the page list — which guarantees the API ES module is loaded
                    # and its axios interceptors are wired before the extract
                    # ``import()``s it and calls ``/chapters/{id}`` itself.
                    wait_for=_CHAPTER_PAGES_WAIT_FOR,
                    # The in-page API call is a couple of decrypted requests, not an
                    # O(pages) DOM walk; 60s stays as a generous margin for the
                    # scaffold wait + Cloudflare/first-paint tail. The per-source
                    # rate limiter bounds outer cadence.
                    timeout=60.0,
                )
            except Exception as exc:  # noqa: BLE001 — surface as a typed source failure
                raise SourceError(
                    "source_unavailable", f"browser manifest fetch failed: {exc}"
                ) from exc
            # Cold-race signature: an EMPTY list on a non-final attempt → re-nav once.
            if (
                isinstance(urls, list)
                and not urls
                and attempt < _MANIFEST_COLD_RACE_ATTEMPTS - 1
            ):
                continue
            break
        if (
            not isinstance(urls, list)
            or not urls
            or not all(
                isinstance(u, str) and u and _is_allowed_image_url(u) for u in urls
            )
        ):
            raise SourceError("source_unavailable", "malformed chapter manifest")
        return urls

    @staticmethod
    def _decode_enc_prefix(
        data: bytes, seed: int, enc_len: int, algo: int = _ENC_ALGO_LCG
    ) -> bytes:
        """Return ``data`` with its first ``enc_len`` bytes keystream-XOR-decoded.

        ``seed == 0`` is a no-op (the ~75% plaintext pages). The PRNG is selected by
        ``x-enc-algo`` (defaulting to the legacy LCG so an absent header / old call site
        is byte-identical):

        * ``algo == 1`` (``_ENC_ALGO_LCG``, spike 012 / PR #170) — 32-bit LCG, XOR the
          TOP byte (``state >> 24``) of each advanced state.
        * ``algo == 2`` (``_ENC_ALGO_XORSHIFT``, spike 017) — xorshift32 seeded
          ``(seed | 1)``, XOR the LOW byte (``state & 0xFF``).

        An UNKNOWN algo raises ``ValueError`` (the caller maps it to a loud SourceError)
        rather than silently applying the wrong cipher — that silent mismatch is exactly
        how PR #170 regressed (#169). Pure stdlib, bit-exact against the spike vectors.
        """
        if seed == 0:
            return data
        out = bytearray(data)
        limit = min(enc_len, len(out))
        if algo == _ENC_ALGO_LCG:
            state = seed & _ENC_MASK
            for i in range(limit):
                state = (state * _ENC_MULTIPLIER + _ENC_INCREMENT) & _ENC_MASK
                out[i] ^= (state >> 24) & 0xFF
        elif algo == _ENC_ALGO_XORSHIFT:
            state = (seed | 1) & _ENC_MASK
            for i in range(limit):
                state = _xorshift32_step(state)
                out[i] ^= state & 0xFF
        else:
            raise ValueError(f"unknown x-enc-algo: {algo}")
        return bytes(out)

    @staticmethod
    def _enc_header_int(raw: str | None) -> int:
        """Parse an ``x-enc-*`` header to a non-negative int, failing SAFE to ``0``
        (T-iy5-02).

        A missing, empty, non-numeric, OR signed/negative header returns ``0`` and never
        raises — so a corrupt/hostile ``x-enc-seed``/``x-enc-len`` degrades to plaintext
        passthrough rather than producing broken output. Rejecting negatives matters
        because a negative ``x-enc-len`` would make ``range(min(enc_len, …))`` empty —
        skipping the XOR loop and leaving an encrypted page as undecodable ciphertext —
        and a negative ``x-enc-seed`` would still decode (corrupting a plaintext page).
        ``str.isdecimal()`` rejects ``-1``/``+1``/``0x..``/whitespace-only while still
        admitting the large unsigned 32-bit seeds Comix sends. The ``len > 10`` guard
        rejects over-long digit runs BEFORE ``int()``: a valid 32-bit value is at most
        ``"4294967295"`` (10 digits), and the bound stops ``int()`` raising on a hostile
        >4300-digit string (CPython's int-conversion limit), which would escape the
        fail-safe. The residual ``> _ENC_MASK`` check rejects the in-10-digit-but-over
        case (``"4294967296"``): ``_decode_enc_prefix`` masks with ``_ENC_MASK``, so an
        out-of-range header would alias to a different in-range state and decode-corrupt
        a page instead of failing safe.
        """
        if raw is None:
            return 0
        value = raw.strip()
        if not value.isdecimal() or len(value) > 10:
            return 0
        parsed = int(value)
        if parsed > _ENC_MASK:
            return 0
        return parsed

    @staticmethod
    def _algo_header(raw: str | None, default: int) -> int:
        """Parse an ``x-*-algo`` header. Missing/blank → ``default`` (back-compat for
        pages that predate the algo header); a well-formed value is returned verbatim so
        the decode dispatch resolves it (and FAILS LOUD on an unknown id). A malformed
        (non-decimal / over-long) value returns ``-1`` — a guaranteed-unknown id that
        also fails loud rather than silently falling back to the default cipher."""
        if raw is None:
            return default
        value = raw.strip()
        if not value:
            return default
        if not value.isdecimal() or len(value) > 3:
            return -1
        return int(value)

    @staticmethod
    def _scramble_grid(raw: str | None) -> tuple[int, int]:
        """Parse ``x-scramble-grid`` (e.g. ``"5x5"`` or ``"5"``) → ``(cols, rows)``.

        Missing/blank/malformed → the ``5x5`` default (C# ``ParseGrid``). Each dimension
        is clamped to ``[1, _SCRAMBLE_GRID_MAX]`` so a hostile header can't force a huge
        tile loop. ``"5x5"``, ``"5X5"``, ``"5,5"``, and ``"5"`` are all accepted.
        """
        if raw is None:
            return _SCRAMBLE_GRID_DEFAULT
        parts = re.split(r"[x,\s]+", raw.strip().lower())
        nums = [p for p in parts if p.isdecimal() and len(p) <= 3]
        if not nums:
            return _SCRAMBLE_GRID_DEFAULT
        cols = int(nums[0])
        rows = int(nums[1]) if len(nums) > 1 else cols
        if cols <= 0 or rows <= 0:
            return _SCRAMBLE_GRID_DEFAULT
        return min(cols, _SCRAMBLE_GRID_MAX), min(rows, _SCRAMBLE_GRID_MAX)

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's bytes + reverse Comix's page protection (PKG-02).

        Delegates to ``ctx.get_bytes_plain_with_headers`` — cleared by the framework
        seam (D-40), the framework decrypt seam opted out — so the source sees the raw
        CDN bytes PLUS the response headers, and dispatches on them (spike 012 + 017):

        1. **Byte cipher** (``x-enc-seed != 0``): decode the first ``x-enc-len`` bytes
           via the PRNG per ``x-enc-algo`` (1 = LCG, 2 = xorshift32; absent → 1). The
           clamped (≤``_ENC_LEN_MAX``) integer XOR loop is sub-millisecond and runs
           inline on the event loop. Output is still WebP — no re-encode.
        2. **Tile scramble** (``x-scramble-seed`` present): decode → un-permute the
           ``x-scramble-grid`` tiles (PRNG per ``x-scramble-algo``; 1/2 = LCG, 3 =
           BuildOrderV2) → re-encode LOSSLESS. This is Pillow CPU work → offloaded via
           ``asyncio.to_thread`` (ruff ASYNC), like the packaging path. Only scrambled
           pages pay this cost; plaintext / byte-cipher pages stay byte-identical WebP.

        The two transforms are applied decrypt-THEN-unscramble (the C# reference order)
        so a future page carrying both still decodes. An UNKNOWN ``x-enc-algo`` /
        ``x-scramble-algo`` raises a loud ``SourceError`` rather than silently applying
        the wrong transform — PR #170 regressed (#169) by blind-applying algo-1 to
        algo-2. ``httpx.Headers.get`` is case-insensitive, so casing does not matter.
        """
        data, headers = await ctx.get_bytes_plain_with_headers(url)

        # 1. byte cipher
        seed = self._enc_header_int(headers.get("x-enc-seed"))
        if seed != 0:
            enc_len = self._enc_header_int(headers.get("x-enc-len"))
            if enc_len <= 0:
                enc_len = _ENC_LEN_DEFAULT
            enc_len = min(enc_len, _ENC_LEN_MAX)
            algo = self._algo_header(headers.get("x-enc-algo"), _ENC_ALGO_LCG)
            try:
                data = self._decode_enc_prefix(data, seed, enc_len, algo)
            except ValueError as exc:
                raise SourceError("source_unavailable", str(exc)) from exc

        # 2. tile scramble
        raw_scramble_seed = headers.get("x-scramble-seed")
        scramble_seed = self._enc_header_int(raw_scramble_seed)
        if scramble_seed == 0:
            # A present-but-MALFORMED ``x-scramble-seed`` must NOT silently skip
            # unscrambling: unlike the byte cipher (where un-decrypted ciphertext fails
            # the downstream ``is_valid_image`` guard), a scrambled page is a *valid*
            # WebP, so skipping it would silently package visually-corrupt pixels — the
            # exact bug class this fix exists to close. Fail loud instead. An absent /
            # blank / explicit ``"0"`` header is a genuine "not scrambled" signal and
            # passes through (mirrors the byte cipher's seed==0 no-op).
            if raw_scramble_seed is not None and raw_scramble_seed.strip() not in (
                "",
                "0",
            ):
                raise SourceError("source_unavailable", "invalid x-scramble-seed")
        else:
            cols, rows = self._scramble_grid(headers.get("x-scramble-grid"))
            scramble_algo = self._algo_header(
                headers.get("x-scramble-algo"), _SCRAMBLE_ALGO_LEGACY_LCG[0]
            )
            try:
                data = await asyncio.to_thread(
                    _unscramble_image, data, scramble_seed, cols, rows, scramble_algo
                )
            except ValueError as exc:
                raise SourceError("source_unavailable", str(exc)) from exc

        return data

    # ─────────────────────────── composite chapter-id ────────────────────────────

    @staticmethod
    def _make_composite_chapter_id(
        numeric_id: str, hid: str, slug: str, number: str
    ) -> str:
        """Pack the four URL-construction fields into one opaque-to-engine string.

        The framework treats ``chapter_id`` as source-opaque (engine just passes
        it through to ``fetch_manifest``), so the composite is contained inside
        ComixSource. Every field is non-empty by precondition; an empty ``slug``
        produces a still-valid composite but the resulting chapter URL would be
        invalid — guarded at parse time.
        """
        return _CID_SEP.join((numeric_id, hid, slug, number))

    @staticmethod
    def _parse_composite_chapter_id(composite: str) -> tuple[str, str, str, str]:
        """Unpack ``{numeric_id}|{hid}|{slug}|{number}``.

        Raises ``ValueError`` for a malformed composite (wrong segment count,
        empty segments) so :meth:`fetch_manifest` can translate it to a
        :class:`SourceError` (WR-06).
        """
        parts = composite.split(_CID_SEP)
        if len(parts) != 4 or not all(parts):
            raise ValueError(
                f"expected 4 non-empty segments separated by {_CID_SEP!r}, "
                f"got {len(parts)}: {composite!r}"
            )
        return parts[0], parts[1], parts[2], parts[3]

    @staticmethod
    def _solver_from_ctx(ctx: SourceContext, *, need_typed: bool = False) -> Any:
        """Pull the AntiBotSolver out of ``ctx`` for the browser-fetch paths.

        The framework wires the solver into ``SourceContext`` for any
        ``cloudflare*`` source (D-40 clearance injection). For Comix's browser-
        DOM reads we ALSO need off-Protocol browser primitives (D-41), same
        instance, distinct from the request-clearance use:

        * ``fetch_via_browser`` — the one-shot primitive used for BOTH the
          chapter-list enumeration (:meth:`_fetch_series_chapters_raw`, which runs
          comix's own internal ``chapters(hid, {limit:100})`` loader in the warm
          tab — spike 019) AND the chapter-pages manifest read
          (:meth:`fetch_manifest`); and
        * ``fetch_via_browser_typed`` — the real-keyboard typed search that mints
          the ``_=`` request token (``need_typed=True``, :meth:`_search_series`;
          debug ``comix-search-api-403``).

        Raises ``SourceError`` when the solver is missing OR lacks a REQUIRED
        primitive (a wiring bug, not a runtime condition). (Comix no longer needs
        ``fetch_via_browser_paginated`` — spike 019 replaced the DOM Next-walk with
        the in-page API loader on the one-shot ``fetch_via_browser`` — nor the
        PARALLEL ``fetch_via_browser_parallel_pages``; both framework primitives
        stay for other sources.)
        """
        solver = getattr(ctx, "_solver", None)
        if solver is None or not hasattr(solver, "fetch_via_browser"):
            raise SourceError(
                "source_unavailable",
                "comix browser-fetch requires a solver with fetch_via_browser",
            )
        if need_typed and not hasattr(solver, "fetch_via_browser_typed"):
            raise SourceError(
                "source_unavailable",
                "comix search requires a solver with fetch_via_browser_typed",
            )
        return solver

    # ─────────────────────────── External tracker links ─────────────────────────

    async def fetch_external_links(
        self, series_id: str, ctx: SourceContext
    ) -> ExternalLinks | None:
        """Parse the stashed candidate ``links`` → canonical bare-ID links (D-02/R5).

        PARSING ONLY (zero added HTTP): reads the raw ``links`` dict stashed during
        ``search`` on ``ctx.external_links_raw`` (carried verbatim off the Layer-1
        candidate tuple — the search payload's ``result.items[].links``, full tracker
        URLs, Pitfall 4) and routes it through the shared normalizer. Comix exposes
        FULL URLs, so ``normalize`` extracts the bare IDs (R2). Returns ``None`` when
        the series carried no tracker links. The framework owns the resolve-once cache
        + best-effort timeout/swallow (``resolve_external_links``).
        """
        raw = ctx.external_links_raw.get(series_id)
        if not raw:
            return None
        return normalize(raw, "comix")

    # ─────────────────────────── Comix fetch helpers ──────────────────────────

    async def _search_series(
        self, query: str, limit: int, ctx: SourceContext
    ) -> list[tuple[str, str, str, list[str], dict[str, Any] | None]]:
        """Token-gated ``/api/v1/manga`` search → ``(hid, slug, title, alt, links)``.

        Returns ``(hid, slug, title, alt_titles, links)`` tuples. The 5-char ``hid`` is
        the canonical series identifier; the ``slug`` is extracted from the
        item's ``url`` field (``/title/{hid}-{slug}`` per live recon); the
        ``title`` is the rendered series title and is threaded through to
        ``_to_release`` so the per-chapter Release carries the manga title (the
        browser-DOM chapter rows do not repeat the series title — it's on the
        series-page header). ``alt_titles`` are the item's ``altTitles`` (a clean
        ``list[str]`` from the same payload, e.g. the Korean native name) and feed
        the alt-title-aware prune (#139) — nothing extra is fetched.

        ``GET /api/v1/manga`` now requires a JS-minted ``_=`` request token (debug
        ``comix-search-api-403``; see ``_SEARCH_TOKEN_URL_EXTRACT_JS``). We drive
        the SPA's own search box so the page mints the token and fires the XHR,
        read the EXACT tokenized URL off the Resource-Timing buffer, then replay
        THAT url verbatim (the ``_=`` signature is bound to the exact query string,
        so no param is added or rewritten — ``get_json_plain`` GETs it as-is). The
        ``limit`` arg is no longer threaded into the request: the SPA autocomplete
        fixes ``limit=6`` and ``search()`` keeps only ``_DEFAULT_SERIES_CANDIDATES``
        candidates after the prune, so 6 covers it.
        """
        _ = limit  # SPA-controlled (autocomplete limit=6); see docstring.
        solver = self._solver_from_ctx(ctx, need_typed=True)
        token_url = await solver.fetch_via_browser_typed(
            f"{self.base_url}/",
            type_selector=_SEARCH_INPUT_SELECTOR,
            type_text=query,
            extract=_SEARCH_TOKEN_URL_EXTRACT_JS,
            wait_for=_SEARCH_REQUEST_FIRED_JS,
            timeout=_SEARCH_TYPED_TIMEOUT,
        )
        if not isinstance(token_url, str) or "/api/v1/manga" not in token_url:
            # The typed nav ran but no tokenized search XHR was captured — surface
            # as the source's own failure (WR-06 per-source warning), not a crash.
            raise SourceError(
                "source_unavailable",
                "comix search token capture failed (no tokenized /api/v1/manga URL)",
            )
        # PLAINTEXT endpoint (search is not encrypted) — get_json_plain keeps the
        # framework decrypt seam out of this path and replays the URL verbatim.
        data = await ctx.get_json_plain(token_url)
        items = self._result_items(data)
        out: list[tuple[str, str, str, list[str], dict[str, Any] | None]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            hid = item.get("hid")
            if not hid:
                continue
            # Skip series that have no published chapters yet (``hasChapters: false``
            # in the search response) — navigating their series page would hang the
            # chapter-list extractor's wait_for since ``a.mchap-row__primary`` never
            # renders. Real production case: e.g. announced-but-not-yet-released
            # series surface in keyword matches.
            if item.get("hasChapters") is False:
                continue
            slug = self._slug_from_item(item)
            title = str(item.get("title") or "")
            alt_titles = [
                s for s in (item.get("altTitles") or []) if isinstance(s, str)
            ]
            # Phase-13 (Pitfall 4): carry the search payload's ``links`` object
            # (full tracker URLs: ``al/mal/mu/md/mb``) through the cached candidate
            # tuple so ``fetch_external_links`` can parse it parse-only with ZERO
            # added HTTP (R5). Was fetched-then-discarded before this widening.
            raw_links = item.get("links")
            links = raw_links if isinstance(raw_links, dict) else None
            out.append((str(hid), slug, title, alt_titles, links))
        return out

    @staticmethod
    def _slug_from_item(item: dict[str, Any]) -> str:
        """Extract the title-derived slug from a Comix search item.

        The live ``/api/v1/manga`` response carries ``url=/title/{hid}-{slug}``.
        We strip the leading ``/title/{hid}-`` to get the slug. If the URL is
        missing or doesn't fit the pattern, we fall back to a slug derived from
        the item's ``title`` field (lowercased + non-alnum runs collapsed to
        hyphens) so the URL ComixSource builds is still navigable — the live
        site appears tolerant of slug drift as long as ``hid`` and chapter id
        are correct.
        """
        raw_url = item.get("url")
        hid = item.get("hid") or ""
        if isinstance(raw_url, str) and raw_url and hid:
            # Patterns: ``/title/{hid}-{slug}`` or ``title/{hid}-{slug}``.
            stripped = raw_url.lstrip("/")
            prefix = f"title/{hid}-"
            if stripped.startswith(prefix):
                slug = stripped[len(prefix) :]
                # Drop any trailing path segment (defensive).
                slug = slug.split("/", 1)[0]
                if slug:
                    return slug
        # Title-derived fallback (best-effort; real recon never hit this path).
        title = item.get("title")
        if isinstance(title, str) and title:
            return _title_to_slug(title)
        return "manga"  # last-resort placeholder; bare URL would still 404 cleanly

    async def _fetch_series_chapters_raw(
        self,
        series_hid: str,
        series_slug: str,
        ctx: SourceContext,
    ) -> list[dict[str, Any]]:
        """The RAW, newest-first, COMPLETE chapter list off the warm series page
        (no filter, no slice).

        This is the EXPENSIVE unit (one warm-tab browser navigation that ALWAYS
        enumerates the FULL chapter list, #146) and so the unit the Layer-2
        enumeration cache stores: it navigates ``{base_url}/title/{hid}-{slug}`` in
        the warm Patchright browser, waits for the chapter-list anchors to hydrate
        (so the SPA's API module is loaded + interceptors warm), runs comix's OWN
        internal ``chapters(hid, {limit:100})`` loader in-page to enumerate every
        chapter, normalizes the rows to the dict shape ``_to_release`` consumes, and
        sorts newest-first by chapter number. It applies NEITHER the
        ``chapter_matches`` floor filter NOR the offset/limit slice — those live one
        level up (``_series_chapters`` for the ``fetch_manifest`` path; ``search()``
        for the cached search path) so the cached enumeration is the complete,
        unfiltered list (CACHE-02). Because the enumeration is COMPLETE, ``search()``
        marks the cached ``Enumeration`` exhausted.

        #146 / spike 019: ALWAYS enumerate the FULL chapter list, not just the ~20
        rows on first paint. The prior approaches walked the rendered DOM page-by-
        page (the ``limit=20`` "Next" control — #232) which was correct but slow
        (One Piece ~236 pages, ~20-30s, blew the 30s budget on the longest series).
        Spike 019 replaced it: the chapter-list endpoint's ``_=`` request signature
        (binds page+limit) and encrypted ``{"e":...}`` response are un-crackable
        statically, so rather than crack them we call comix's OWN axios loader. The
        API-client ES module (``env-*.js``, discovered at runtime — the hash rotates
        per deploy) is a cached singleton whose request-SIGN + response-DECRYPT
        interceptors are already wired; ``await import()``-ing it and calling
        ``<api>.chapters(hid, {page, limit:100, order})`` signs limit=100 AND returns
        DECRYPTED rows. The extract (``_CHAPTER_LIST_API_EXTRACT_JS``) fans the pages
        out (bounded ``Promise.all``) in ONE warm tab and merges deduped by ``id``.
        limit=100 cuts One Piece to ~47 pages, each a ~500ms in-page API call. A page
        fetch that throws FAILS CLOSED (rejects ``Promise.all`` → the extract throws →
        ``SourceError`` below) — a partial list is never returned. All comix-side
        literals (the ``/env-*.js`` discovery, the ``chapters()`` call, ``limit=100``)
        live in the extract JS, never in the framework.

        PERF (spike 019, live): the in-page API fan-out replaces the per-page DOM
        Next-walk — One Piece drops from ~236 sequential limit=20 pages (timeout) to
        ~47 limit=100 in-page API calls fanned out in one tab. Bounded by the 30s
        ``fetch_via_browser`` timeout (≤ the framework's 30s per-source fan-out
        timeout, ``framework/fanout.py::_DEFAULT_TIMEOUT``); the per-source aiolimiter
        still bounds outer cadence. A failed fetch surfaces as
        ``SourceError("source_unavailable")`` → per-source warning (WR-06).
        """
        solver = self._solver_from_ctx(ctx)
        series_url = f"{self.base_url}/title/{series_hid}-{series_slug}"
        try:
            raw = await solver.fetch_via_browser(
                series_url,
                # Run comix's own internal ``chapters(hid, {limit:100})`` loader in
                # the warm cleared tab (spike 019) — signs the ``_=`` token for
                # limit=100 AND decrypts the response in-page, no VM crack.
                extract=_CHAPTER_LIST_API_EXTRACT_JS,
                # Wait for the chapter-list anchors so the SPA has booted and the
                # API ES module is loaded with its interceptors wired before import.
                wait_for=_CHAPTER_LIST_WAIT_FOR,
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001 — surface as typed source failure
            raise SourceError(
                "source_unavailable", f"browser chapter-list fetch failed: {exc}"
            ) from exc
        if not isinstance(raw, list):
            raise SourceError("source_unavailable", "malformed chapter list")
        # Normalize to the dict shape ``_to_release`` already consumes (the extract
        # emits the same shape the prior DOM extractor did, so the consumer is
        # source-agnostic). Sort newest-first by chapter number when parseable.
        chapters: list[dict[str, Any]] = [c for c in raw if isinstance(c, dict)]
        chapters.sort(
            key=lambda c: self._parse_decimal(c.get("chapter")) or Decimal(0),
            reverse=True,
        )
        return chapters

    async def _series_chapters(
        self,
        series_hid: str,
        series_slug: str,
        limit: int,
        offset: int,
        ctx: SourceContext,
        req: SearchRequest | None = None,
    ) -> list[dict[str, Any]]:
        """Browser-tab read of the FULL series chapter list (#146 / spike 019).

        Delegates to :meth:`_fetch_series_chapters_raw`, which navigates
        ``{base_url}/title/{hid}-{slug}`` in the warm Patchright browser and ALWAYS
        enumerates the FULL chapter list by running comix's OWN internal
        ``chapters(hid, {limit:100})`` loader in-page (spike 019) — yielding
        ``[{id, chapter, lang, groups, publishedAtRelative, likes, volume}, …]`` for
        every chapter, not just the ~20 rows on first paint. Before #146 this was a
        one-shot read of the first render (mostly the newest chapter's group-
        uploads), so a low/old chapter only enumerable deeper in the list was missed.
        The chapter id and chapter number are load-bearing — group/lang/date/likes
        are best-effort.

        We sort newest-first by chapter number and slice the
        ``offset..offset+limit`` window AFTER the full enumeration so the
        contract behaves identically to the prior path (now over the complete
        list). A failed browser fetch surfaces as
        ``SourceError("source_unavailable")`` → per-source warning (WR-06).

        Scanlation-group extraction: each API chapter row carries a
        ``group: {id, name}`` object; the extract maps it to ``groups: [{name}]``
        and ``_to_release`` reads the name (``scanlationGroup`` stays ``null`` when
        the chapter has no group).

        Publish-date extraction (Issue #30): each API row carries
        ``createdAtFormatted`` (the rendered relative time, e.g. "14h ago",
        "3mos ago"); the absolute timestamp is NOT exposed. The extract maps it to
        ``publishedAtRelative`` and ``_to_release``
        funnels it through :func:`_parse_relative_time` to approximate the
        REL-01 ISO 8601 ``publishDate``.
        """
        # #146: the FULL paginated walk + normalize + newest-first sort is the
        # EXPENSIVE unit and now lives in ``_fetch_series_chapters_raw`` (so the
        # search-path Layer-2 cache wraps the complete walk — a repeat same-series
        # search costs ZERO browser navs). This method just applies the
        # chapter-family filter + offset/limit slice on top of the complete list.
        chapters = await self._fetch_series_chapters_raw(series_hid, series_slug, ctx)
        # 260606-2ff: in the SEARCH path (req is not None), keep only the requested
        # whole-number/floor family BEFORE the offset/feed_limit slice so matches are
        # not starved by the window. The deferred-id-resolution path (fetch_manifest)
        # passes req=None → never chapter-filtered (a specific chapter still resolves).
        # Gate-off (type=manga / chapterless) = pass-through.
        if req is not None:
            chapters = [
                c
                for c in chapters
                if self.chapter_matches(
                    req, self._parse_decimal(c.get("chapter") or c.get("number"))
                )
            ]
        feed_limit = min(limit or _MAX_FEED_LIMIT, _MAX_FEED_LIMIT)
        return chapters[offset : offset + feed_limit]

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        series_hid: str,
        series_slug: str,
        chapter: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        chapter_id = chapter.get("id")
        if not chapter_id:
            return None
        chapter_id = str(chapter_id)

        raw_chapter = chapter.get("chapter") or chapter.get("number")
        chapter_number = self._parse_decimal(raw_chapter)
        chapter_number_str = self._stringify(raw_chapter) or "0"
        volume = self._parse_int(chapter.get("volume"))
        language = chapter.get("lang") or chapter.get("language") or "en"
        page_count = self._parse_int(chapter.get("pages") or chapter.get("pageCount"))
        # REL-03: per-chapter DOM likes -> display-only votes (None when absent).
        # NOT added to the minted ResolutionRecord (display-only, no resolve role).
        votes = self._parse_int(chapter.get("likes"))
        # Issue #30: prefer any absolute timestamp the source surfaces
        # (``publishedAt`` / ``date``), then fall back to parsing the rendered
        # relative time from ``publishedAtRelative`` ("3d ago", "2mos ago").
        # The Comix browser-DOM read only exposes the relative form; ``""``
        # would violate REL-01 (``publishDate`` required + ``format: date-time``).
        publish_date_raw = chapter.get("publishedAt") or chapter.get("date")
        if not publish_date_raw:
            publish_date_raw = _parse_relative_time(chapter.get("publishedAtRelative"))
        publish_date = publish_date_raw or ""

        series_title = self._series_title(chapter)
        group = self._scanlation_group(chapter)

        title = self._build_title(
            series_title or "Unknown",
            self._stringify(raw_chapter),
            volume=volume,
            language=language,
            group=group,
        )
        # Per-upload uniqueness across groups (mirrors MangaDex's D-21 guid shape).
        # D-46: ``hid`` is the canonical series identifier — NOT the numeric id.
        guid = (
            f"comix:{series_hid}:ch-{self._stringify(raw_chapter) or '?'}"
            f":{language}:{chapter_id}"
        )

        # Composite chapter id (Plan 04-04 Option A): pack the URL-construction
        # fields ComixSource.fetch_manifest needs into the stored chapter_id so
        # the stateless ``fetch_manifest(chapter_id, ctx)`` hook can reconstruct
        # the chapter URL without a framework-wide signature change. Engine
        # treats chapter_id as source-opaque (only stores + passes through).
        composite_id = self._make_composite_chapter_id(
            chapter_id, series_hid, series_slug, chapter_number_str
        )

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=composite_id,
                language=language,
                title=title,
                manga_title=series_title,
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
            manga_title=series_title,
            chapter_number=chapter_number,
            volume=volume,
            language=language,
            scanlation_group=group,
            page_count=page_count,
            votes=votes,
            # D-46: ``comixSeriesId`` carries the hid (canonical series slug).
            ids={"comixChapterId": chapter_id, "comixSeriesId": series_hid},
        )

    @staticmethod
    def _build_title(
        series_title: str,
        chapter: str | None,
        *,
        volume: int | None,
        language: str | None,
        group: str | None,
    ) -> str:
        """Title template — MUST stay MangaParser-parseable (REL-02), like MangaDex."""
        parts = [series_title, "-"]
        if volume is not None:
            parts.append(f"Vol. {volume}")
        parts.append(f"Chapter {chapter}" if chapter else "Chapter ?")
        if language:
            parts.append(f"({language})")
        if group:
            parts.append(f"[{group}]")
        return " ".join(parts)

    # ─────────────────────────── parse helpers ───────────────────────────

    @staticmethod
    def _result_items(data: Any) -> list[Any]:
        """Extract ``result.items`` (live-recon search/index shape) tolerantly."""
        if not isinstance(data, dict):
            return []
        result = data.get("result") if isinstance(data.get("result"), dict) else None
        if result is None:
            # Some endpoints may return items at the top level.
            items = data.get("items") or data.get("data") or []
        else:
            items = result.get("items") or result.get("data") or []
        return items if isinstance(items, list) else []

    @staticmethod
    def _chapter_list(data: Any) -> list[dict[str, Any]]:
        """Tolerant chapter-list extraction (decrypted JSON shape).

        The exact decrypted key is pinned by the live smoke; we accept the common
        ``chapters`` / ``data`` / nested ``result.{items|chapters|data}`` shapes.
        """
        if not isinstance(data, dict):
            return []
        # Try a nested result wrapper first (matches the plaintext-endpoint shape).
        result = data.get("result")
        if isinstance(result, dict):
            items = (
                result.get("chapters")
                or result.get("items")
                or result.get("data")
                or []
            )
        else:
            items = data.get("chapters") or data.get("data") or []
        return [c for c in items if isinstance(c, dict)]

    @staticmethod
    def _extract_pages(data: Any) -> list[Any]:
        """Decrypted chapter-pages → ordered raw entries (objects or strings).

        Pin-by-tolerance: prefer ``pages`` (likely a list of ``{url}`` dicts per the
        rendered DOM); fall back to ``images`` (string list per CDN URL pattern).
        Nested under ``result`` like every other Comix endpoint.
        """
        if not isinstance(data, dict):
            return []
        result = data.get("result") if isinstance(data.get("result"), dict) else None
        source = result if result is not None else data
        pages = source.get("pages") if isinstance(source, dict) else None
        if isinstance(pages, list) and pages:
            return pages
        images = source.get("images") if isinstance(source, dict) else None
        if isinstance(images, list) and images:
            return images
        return []

    @staticmethod
    def _series_title(chapter: dict[str, Any]) -> str | None:
        for key in ("seriesTitle", "series", "title"):
            value = chapter.get(key)
            if isinstance(value, dict):  # e.g. {"name": "..."}
                name = value.get("name") or value.get("title")
                if name:
                    return str(name)
            elif value:
                return str(value)
        return None

    @staticmethod
    def _scanlation_group(chapter: dict[str, Any]) -> str | None:
        # Live recon chapter-indexes carry ``groups: [{id, name}, …]``; the
        # encrypted chapter feed likely mirrors that shape (pinned by smoke).
        groups = chapter.get("groups")
        if isinstance(groups, list) and groups:
            first = groups[0]
            if isinstance(first, dict):
                name = first.get("name")
                if name:
                    return str(name)
            elif isinstance(first, str) and first:
                return first
        for key in ("group", "scanlationGroup", "team"):
            value = chapter.get(key)
            if isinstance(value, dict):
                name = value.get("name")
                if name:
                    return str(name)
            elif value:
                return str(value)
        return None

    @staticmethod
    def _page_url(page: Any) -> str | None:
        """Extract a page URL from a manifest entry (str or ``{"url": ...}`` object).

        Image CDN pattern: ``https://{cdn}.store/{seg}/{token}/{NN}.webp``
        (``{seg}`` is a rotating short path segment — ``si``/``i3``).
        """
        if isinstance(page, str) and page:
            return page
        if isinstance(page, dict):
            url = page.get("url") or page.get("src") or page.get("href")
            if isinstance(url, str) and url:
                return url
        return None

    @staticmethod
    def _stringify(raw: Any) -> str | None:
        if raw is None or raw == "":
            return None
        return str(raw)

    @staticmethod
    def _parse_decimal(raw: Any) -> Decimal | None:
        """Parse a chapter number STRING to Decimal (SRCH-06 / Pitfall 1)."""
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
