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

Issue #32 (2026-05-31): the parallel ``scrollIntoView`` extractor introduced
by issue #20 was structurally broken: a synchronous burst of ``scrollIntoView``
calls leaves the viewport at the LAST element's position only — intermediate
pages never enter view, so the per-div IntersectionObserver never fires and
the lazy ``<img>`` never loads. The per-div Promise.all watchers then time
out polling for an ``<img src>`` the observer was never triggered on. Only
head pages (above the fold) + the final 1-2 pages (where the scroll lands)
were captured. This was a silent data-loss bug in the production download
path, not just a drift-test bug: every Comix CBZ shipped for a chapter
longer than ~4 pages was missing middle pages. The fix walks pages
SEQUENTIALLY again, scroll → small await → poll for the page's img with
a tight per-page budget, accepting an O(pages) wall-clock term in exchange
for correctness. The Step-1 scaffold wait is also hardened to wait for
COUNT STABILITY (count unchanged for 3 x 100ms ticks, capped at 8s) so
the extractor never snapshots a partial scaffold.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
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

_log = logging.getLogger("manga_gateway")


# Bound a title search's candidate series. The count is mode-invariant: interactive
# no longer widens the fan-out (#162, mirrors MangaDex). Exact-match queries already
# collapse to ~1 candidate after the #126 prune regardless of the ceiling, so 5 is the
# single source of truth for both modes (still referenced by the prune/cap below).
_DEFAULT_SERIES_CANDIDATES = 5
# Comix chapter-feed page-size ceiling (live recon: server default limit=20).
_MAX_FEED_LIMIT = 100
# Comix search page size (live recon: full-results page uses limit=28).
_SEARCH_PAGE_SIZE = 28
# Default content_rating param (live recon: "suggestive" pulls the same items
# that the public site shows — `safe` would drop suggestive titles).
_CONTENT_RATING = "suggestive"

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

# Page-image encryption (spike-012-verified). The CDN byte-encrypts the first
# ``x-enc-len`` (4096) bytes of every 4th page image, keyed by the per-response
# ``x-enc-seed`` header; ``x-enc-seed == 0`` means plaintext (no-op). The cipher
# is a fully static 32-bit LCG keystream XORed over the prefix — pure stdlib, no
# browser, no re-encode. These constants are the spike-012-verified LCG params.
_ENC_MASK = 0xFFFFFFFF
_ENC_MULTIPLIER = 1000005
_ENC_INCREMENT = 1234567891
_ENC_LEN_DEFAULT = 4096
# Defensive ceiling on the decoded prefix length (T-iy5-01): a hostile/garbage
# ``x-enc-len`` must NOT be able to force a whole-image per-byte XOR loop on the
# event loop. The verified scheme length is 4096; this gives 16x headroom.
_ENC_LEN_MAX = 65536


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
    r"^/[a-z0-9]{2,4}/[A-Za-z0-9_-]{16,}/\d+\.(webp|jpg|jpeg|png)$", re.IGNORECASE
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


# JS extractor that returns the rendered chapter-page image URLs in NN order.
# Matches the live-recon-observed pattern ``/{seg}/{token}/{NN}.{ext}`` (``{seg}``
# is a rotating short path segment — ``si``/``i3``/…, wildcarded) and filters
# out cross-site ad imagery (gravatar, postimg, etc.) so the manifest is the
# chapter pages ONLY. Numbering may have gaps (01,02,04,05,07,…) — the recon
# shows real chapter pages with gaps, NOT lazy-load artifacts; we sort by
# embedded NN so the gaps survive.
#
# Issue #32 (2026-05-31): the prior parallel-scrollIntoView strategy (issue
# #20) was structurally broken. ``scrollIntoView`` is a synchronous viewport
# mutation; issuing N calls in a tight loop leaves the viewport at the FINAL
# element's position only — intermediate pages never enter view, so the per-
# page IntersectionObserver never fires and the lazy ``<img>`` never loads.
# The per-div Promise.all watchers then timed out polling for an ``<img src>``
# the observer was never triggered on. Result: only head pages (above the
# fold) + the final 1-2 pages (where the scroll lands) were captured, every
# Comix CBZ longer than ~4 pages silently shipped truncated. The original fix
# walked pages SEQUENTIALLY (scrollIntoView → settle → poll the div's <img>
# for a CDN-matching src per page) which was correct but cost ~14-15s on a
# 10-page chapter because each page paid an IntersectionObserver + lazy-load
# round-trip serially.
#
# Issue #45 (2026-05-31): rewrite Step 2 as a two-scroll head+tail walk on
# the INNER Swiper scroll container. The spike
# ``.planning/debug/comix-warm-page-no-speedup-spike.md`` empirically showed
# that setting ``viewport_size = {800, 200000}`` loaded pages 1, 2, 13, AND
# 14 simultaneously — meaning the inner Swiper container (an ancestor of
# the ``.rpage-page`` divs with ``overflow:auto|scroll|hidden`` and
# ``scrollHeight > clientHeight``) is what gates middle pages independently
# of the window viewport. Two batched scrolls on THAT container — to
# ``scrollHeight/2`` then ``scrollHeight`` — capture every page image the
# reader has DOM-resident at the time, cheaply (~1s total). The inner-container
# detection is dynamic (ancestor walk reading ``getComputedStyle(el).overflowY``
# + ``scrollHeight``) so a Swiper wrapper-class rotation does not break us; a
# miss falls back to ``document.scrollingElement``.
#
# debug comix-manifest-60s-timeout (2026-06-03): Comix rotated the chapter
# reader from the long-strip variant (``rpage--long-strip rpage--ttb``) to a
# single-page paginated one (``rpage--single rpage--ltr``). In single-page mode
# only ~3 page ``<img>``s are EVER DOM-resident at once, so the old
# per-missing-page ``scrollIntoView`` fallback (issue #45 Step 2b) force-loaded
# nothing — it just burned ~1.55s/page polling for an ``<img>`` that never
# attached. A 51-page chapter thus walked ~74s and blew the 60s
# ``fetch_via_browser`` ceiling (``page.evaluate timed out after 60.0s``); a
# 33-page chapter squeaked under 60s but shipped a SILENTLY TRUNCATED 3-of-33
# CBZ (the issue #32 failure class, re-introduced by the reader rotation).
#
# FIX: drop the O(pages) per-missing walk entirely and SYNTHESIZE instead. The
# full ``data-page`` scaffold (1..N) is always present, and (verified live
# 2026-06-03) every page of a chapter shares ONE host + ONE token, differing
# only by the zero-padded filename number. So after the cheap two-scroll
# capture, derive the URL for every uncaptured scaffold page by substituting its
# filename number into a captured img's own ``/{NN}.{ext}`` tail (Step 4). This
# is O(1) in wall-clock, returns the complete 1..N manifest, fixes the
# truncation, and works for BOTH reader shapes (the same per-page template holds
# in long-strip too), so it is robust to this and future reader-shape rotations.
# Real captures always win over synthesized ones (first-sight in ``seen``), so a
# future per-page-token reader degrades to "captured pages correct, gaps filled"
# rather than silently wrong. The host/extension SSRF anchors are unchanged and
# every synthesized URL is still validated by ``_is_allowed_image_url`` before
# fetch.
#
# NEVER synthesize from an empty capture: if Step 2/3 matched zero CDN imgs the
# page is genuinely broken/gated and we return ``[]`` → fetch_manifest raises
# ``malformed chapter manifest`` (fast, correct) rather than fabricating URLs.
#
# Step-1 scaffold wait is unchanged: count-unchanged-for-3-consecutive-ticks
# (8s cap) so a partial scaffold cannot race the capture/synthesis.
_CHAPTER_PAGES_EXTRACT_JS = """
  // Comix's chapter reader is a Swiper.js component. Historically a long-strip
  // (`rpage--long-strip rpage--ttb`); as of 2026-06-03 a single-page paginated
  // variant (`rpage--single rpage--ltr`) is served, in which only ~3 page
  // <img>s are DOM-resident at once. Each page is wrapped in a
  // `<div class="rpage-page" data-page="N">` whose <img> child is LAZY-LOADED.
  //
  // Strategy (debug comix-manifest-60s-timeout, 2026-06-03):
  //   Step 1 — wait for the data-page scaffold COUNT to stabilize (gives N).
  //   Step 2 — find the inner Swiper scroll container by ancestor walk, then
  //            scrollTo(scrollHeight/2) + scrollTo(scrollHeight) with ~500ms
  //            settle each; capture every CDN-matching <img> after each scroll
  //            (first-sight wins via a Map keyed on the data-page integer).
  //            Cheap (~1s) — gets whatever the reader has DOM-resident.
  //   Step 3 — final document.querySelectorAll('img') sweep (issue #32 net).
  //   Step 4 — SYNTHESIZE the URL for every scaffold page not captured by
  //            substituting its filename number into a captured img's
  //            /{NN}.{ext} tail (all pages share one host+token, verified live
  //            2026-06-03). Replaces the old O(pages) per-missing-page
  //            scrollIntoView walk that blew the 60s budget in single-page mode.
  //   Step 5 — sort the Map entries by page number ascending and return URLs.
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  // Path segment (`si`/`i3`/…) is wildcarded — Comix rotates it; live
  // 2026-06-02: https://jloo.wowpic5.store/i3/<token>/01.webp. The host pin +
  // token/filename shape (and the _COMIX_CDN_PATH_RE allowlist) carry the trust.
  const rx = /\\/[a-z0-9]{2,4}\\/([A-Za-z0-9_-]{16,})\\/(\\d+)\\.(webp|jpg|jpeg|png)$/i;

  // Step 1: wait for the page scaffold COUNT TO STABILIZE. Returning on the
  // first .rpage-page[data-page] div would race Swiper's incremental
  // scaffold and snapshot a partial pageDivs list. Poll every 100ms;
  // declare the scaffold ready once the count has been unchanged for 3
  // consecutive ticks (with a non-zero count so "still zero" isn't stable).
  // Cap at 8s wall-clock — beyond that the page is genuinely broken.
  let stable = 0;
  let lastCount = -1;
  for (let i = 0; i < 80; i++) {
    const count = document.querySelectorAll('.rpage-page[data-page]').length;
    if (count > 0 && count === lastCount) {
      stable += 1;
      if (stable >= 3) break;
    } else {
      stable = 0;
      lastCount = count;
    }
    await sleep(100);
  }

  const pageDivs = Array.from(document.querySelectorAll('.rpage-page[data-page]'))
    .sort((a, b) => {
      const an = parseInt(a.getAttribute('data-page') || '0', 10);
      const bn = parseInt(b.getAttribute('data-page') || '0', 10);
      return an - bn;
    });

  const seen = new Map();
  const captureAll = () => {
    // Sweep every .rpage-page div's <img> children, recording the first
    // CDN-matching src per data-page. First-sight wins so a subsequent
    // Swiper eviction never loses a URL we already saw.
    for (const div of pageDivs) {
      const n = parseInt(div.getAttribute('data-page') || '0', 10);
      if (seen.has(n)) continue;
      for (const img of div.querySelectorAll('img')) {
        const src = img.currentSrc || img.src || '';
        const m = src.match(rx);
        if (m) {
          seen.set(n, src);
          break;
        }
      }
    }
  };

  // Step 2: identify the inner Swiper scroll container and do TWO batched
  // scrolls (midpoint then end). The spike's tall-viewport finding (pages
  // 1, 2, 13, 14 loaded but 3-12 stayed lazy) is the empirical evidence
  // that this container — NOT the window — is what gates middle pages.
  //
  // Ancestor walk: starting from the first .rpage-page div, climb parents
  // and test each for overflowY in {auto, scroll, hidden} AND
  // scrollHeight > clientHeight (so we pick the element that ACTUALLY
  // gates scrolling, not a decorative overflow:hidden wrapper). The CSS
  // class of the container is NOT pinned — bundle rotations may change
  // it; the dynamic check protects us. Fall back to
  // document.scrollingElement on miss (degraded path; Step 2b picks up
  // the slack).
  let container = null;
  if (pageDivs.length > 0) {
    let el = pageDivs[0].parentElement;
    while (el && el !== document.body) {
      const style = getComputedStyle(el);
      const overflowY = style.overflowY;
      const scrolls =
        overflowY === 'auto' ||
        overflowY === 'scroll' ||
        overflowY === 'hidden';
      if (scrolls && el.scrollHeight > el.clientHeight) {
        container = el;
        break;
      }
      el = el.parentElement;
    }
  }
  if (!container) {
    container = document.scrollingElement || document.documentElement;
  }

  // Two batched scrolls: midpoint then end. 500ms settle after each is
  // the empirically-grounded budget from the spike — enough for the
  // IntersectionObserver callback to fire AND for the lazy <img>'s src
  // to propagate. Capture after each scroll so a Swiper eviction
  // between the two passes can't lose a page we already saw.
  container.scrollTo({ top: container.scrollHeight / 2, behavior: 'instant' });
  await sleep(500);
  captureAll();
  container.scrollTo({ top: container.scrollHeight, behavior: 'instant' });
  await sleep(500);
  captureAll();

  // Step 3 (safety net): sweep the whole document for any CDN-matching <img>
  // the per-div capture missed (reader-shape variants where the canonical
  // `data-page` wrapper is absent but images still match the CDN pattern, OR a
  // page whose <img> only attached AFTER we scrolled past it). This is the
  // issue #32 silent-truncation safety net — DO NOT remove.
  // Key by the nearest .rpage-page[data-page] ancestor — the SAME key space
  // Step 2's captureAll uses — so a captured page is never double-recorded
  // under both its data-page (Step 2) and its filename number (here); that
  // would defeat Step 4's data-page→filename `offset` correction on a
  // 0-/1-indexed reader. Only orphan imgs with no scaffold wrapper (the
  // degenerate variant this net targets, where pageDivs is empty and Step 4
  // is skipped) fall back to the filename number.
  for (const img of document.querySelectorAll('img')) {
    const src = img.currentSrc || img.src || '';
    const m = src.match(rx);
    if (m) {
      const wrapper = img.closest('.rpage-page[data-page]');
      const n = wrapper
        ? parseInt(wrapper.getAttribute('data-page') || '0', 10)
        : parseInt(m[2], 10);
      if (!seen.has(n)) seen.set(n, src);
    }
  }

  // Step 4: synthesize the URL for every scaffold page we did NOT capture.
  // Comix's single-page reader keeps only ~3 imgs DOM-resident, so `seen` is
  // short even on a healthy chapter — but the full data-page scaffold (1..N) is
  // present and every page shares ONE host + ONE token, differing only by the
  // zero-padded filename number (verified live 2026-06-03, debug
  // comix-manifest-60s-timeout). Derive each missing page's URL by substituting
  // its filename number into a captured src's /{NN}.{ext} tail — O(1), no
  // per-page lazy-load round-trip. NEVER synthesize from an empty capture
  // (seen.size === 0) — return [] so fetch_manifest raises
  // "malformed chapter manifest" rather than fabricating URLs for a broken page.
  if (seen.size > 0 && pageDivs.length > 0) {
    // Lowest-data-page captured sample → template + data-page→filename offset
    // + zero-pad width. Offset is normally 0 (data-page 1 → "01"); deriving it
    // tolerates a 0-/1-indexed mismatch without guessing.
    const sampleEntry = Array.from(seen.entries()).sort((a, b) => a[0] - b[0])[0];
    const sampleN = sampleEntry[0];
    const sampleSrc = sampleEntry[1];
    const sm = sampleSrc.match(rx);
    if (sm) {
      const sampleFile = parseInt(sm[2], 10);
      const width = sm[2].length;
      const ext = sm[3];
      const offset = sampleFile - sampleN;
      for (const div of pageDivs) {
        const n = parseInt(div.getAttribute('data-page') || '0', 10);
        if (!n || seen.has(n)) continue;
        const fileNum = n + offset;
        if (fileNum < 0) continue;
        const padded = String(fileNum).padStart(width, '0');
        // Substitute into the sample's own src so host/seg/token (and any
        // future query suffix) carry over verbatim; only the NN filename moves.
        const synth = sampleSrc.replace(
          /\\/\\d+\\.(webp|jpg|jpeg|png)$/i,
          '/' + padded + '.' + ext
        );
        seen.set(n, synth);
      }
    }
  }

  // Step 5: page-number-ascending order, gaps preserved.
  return Array.from(seen.entries())
    .sort((a, b) => a[0] - b[0])
    .map(e => e[1]);
"""

# CSS selector ``solver.fetch_via_browser`` waits for before reading the DOM.
# The chapter reader scaffolds `<div class="rpage-page" data-page="N">` for
# every page in the chapter once the encrypted page-list arrives and decrypts;
# images inside are lazy-loaded later via IntersectionObserver. Waiting for
# the first scaffold div (not the first image) ensures the extract starts as
# soon as the reader knows the chapter length — the scaffold count IS N, which
# Step 4 of _CHAPTER_PAGES_EXTRACT_JS uses to synthesize the full 1..N manifest
# from a captured page-image template (the single-page reader only ever keeps
# ~3 imgs DOM-resident, so we no longer try to lazy-load every page).
_CHAPTER_PAGES_WAIT_FOR = ".rpage-page[data-page]"

# JS extractor that returns the rendered chapter list off the series page DOM.
# Selects ``<a>`` elements whose href matches the recon-pinned chapter URL
# pattern ``/title/{hid}-{slug}/{chapter_id}-chapter-{number}`` and emits a
# ``{id, chapter, lang, groups, publishedAtRelative}`` shape per chapter —
# match-compatible with the encrypted-API ``_to_release`` consumer. Lang
# defaults to "en" (Comix is English-only per live recon) and group is best-
# effort extracted from a sibling ``.scanlation`` / ``.group`` element when
# present; absent the live-smoke is allowed to refine the selector. Chapter
# IDs and numbers are load-bearing — the rest is advisory.
#
# Issue #30 (2026-05-30): also extract ``<span class="mchap-row__time">``'s
# text content per row. The chapter-list DOM does NOT expose a machine-
# readable absolute timestamp; the only public per-row date is the rendered
# relative string ("2d ago", "3mos ago", "14h ago"). We capture it raw here
# and parse it Python-side (:func:`_parse_relative_time`) into an approximate
# ISO 8601 UTC date-time so the REL-01 ``publishDate`` contract holds.
_CHAPTER_LIST_EXTRACT_JS = """
  const rx = /\\/title\\/[A-Za-z0-9_-]+\\/(\\d+)-chapter-([0-9.]+)(?:[/?#]|$)/i;
  const seen = new Set();
  const out = [];
  const anchors = Array.from(document.querySelectorAll('a[href*="-chapter-"]'));
  for (const a of anchors) {
    const href = a.getAttribute('href') || '';
    const m = href.match(rx);
    if (!m) continue;
    const id = m[1];
    if (seen.has(id)) continue;
    seen.add(id);
    // Each chapter row is a ``<div class="mchap-row">`` carrying an
    // ``<a class="mchap-row__group">…<span>{name}</span></a>``. Walk to the
    // row, find the group anchor, and prefer its inner <span> (the anchor
    // itself contains an SVG icon whose textContent is empty/whitespace).
    // A miss is still a valid chapter row (group simply omitted).
    let group = null;
    let publishedAtRelative = null;
    let likes = null;
    const row = a.closest(
      '.mchap-row, li, tr, [data-chapter], .chapter, .chapter-item'
    );
    if (row) {
      const g = row.querySelector(
        'a.mchap-row__group, .scanlation, .group, [data-group], .scanlator'
      );
      if (g) {
        const span = g.querySelector('span');
        const text = ((span && span.textContent) || g.textContent || '').trim();
        if (text) group = text;
      }
      // Issue #30: ``<span class="mchap-row__time">`` carries the rendered
      // ``createdAtFormatted`` value (e.g. "14h ago", "2d ago", "3mos ago").
      // The raw absolute timestamp is NOT exposed in the DOM — only this
      // relative string. We capture it for Python-side approximation.
      const t = row.querySelector('.mchap-row__time, time, [data-time]');
      if (t) {
        const text = (t.textContent || '').trim();
        if (text) publishedAtRelative = text;
      }
      // REL-03: ``<span class="mchap-row__likes"><svg/>27</span>`` carries the
      // per-chapter thumbs-up count. textContent is the inline SVG icon noise
      // followed by the integer (e.g. an abbreviated "1.2K" form). Strip the
      // non-digit noise and parse the trailing count; a row without the span is
      // still a valid chapter row (likes simply null).
      const lk = row.querySelector('.mchap-row__likes, [data-likes]');
      if (lk) {
        const raw = (lk.textContent || '').replace(/[^0-9.kKmM]/g, '');
        const km = raw.match(/^([0-9.]+)([kKmM]?)$/);
        if (km) {
          let n = parseFloat(km[1]);
          if (!isNaN(n)) {
            const suffix = km[2].toLowerCase();
            if (suffix === 'k') n *= 1000;
            else if (suffix === 'm') n *= 1000000;
            likes = Math.round(n);
          }
        }
      }
    }
    out.push({
      id: id,
      chapter: m[2],
      lang: 'en',
      groups: group ? [{ name: group }] : [],
      publishedAtRelative: publishedAtRelative,
      likes: likes
    });
  }
  return out;
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
        async def _resolve_fn() -> list[tuple[str, str, str, list[str]]]:
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
        result_window = req.limit or _MAX_FEED_LIMIT

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
            for series_hid, series_slug, _series_title, _alt in series
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        releases: list[Release] = []
        for (series_hid, series_slug, series_title, _alt), result in zip(
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
            for chapter in chapters[req.offset : req.offset + result_window]:
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
        """Newest-first recent chapters via the plaintext list-mangas feed (RCNT-01/02).

        Issue #42 (supersedes #31): synthesizes one ``Release`` per viable item
        from a single plaintext ``GET /api/v1/manga?order[chapter_updated_at]=desc``
        call (the same plaintext path search uses, one of 120/min rate budget).
        Each Release carries a ``:DEFERRED`` guid suffix and a deferred
        composite ``chapter_id`` whose numeric id is late-bound by
        :meth:`fetch_manifest` at download time (one extra browser nav per
        FIRST download of a recent-minted Release — not per ``/recent`` poll).

        Recent and search Releases for the same chapter intentionally do not
        dedup — they are different objects (a late-binding promise vs a
        concrete upload). See locked decisions 1, 3, 7 in the PLAN.

        ``since`` and ``languages`` are noted unused: Comix is English-only
        (live recon) and ``since`` is enforced upstream by the route-level cut
        already (same no-op pattern as today). Items missing ``hid``,
        ``hasChapters: false``, an unparseable ``latestChapter``, no slug, or
        an unparseable ``chapterUpdatedAtFormatted`` are SKIPPED rather than
        faked (REL-01 requires ``format: date-time``).
        """
        _ = (languages, since)  # see docstring — both deliberately unused here
        params: dict[str, Any] = {
            "order[chapter_updated_at]": "desc",
            "limit": min(limit or _MAX_FEED_LIMIT, _MAX_FEED_LIMIT),
            "page": 1,
            "content_rating": _CONTENT_RATING,
        }
        data = await ctx.get_json_plain(f"{self.base_url}/api/v1/manga", **params)
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
                )
            )
        return releases

    # ───────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered page-image URLs, INTERNALLY (PKG-01/R6).

        Option A (Plan 04-04, 2026-05-30): drive the chapter HTML page in the
        warm Patchright browser and read the rendered image-tag URLs off the DOM.
        The page's own JS does token-mint + encrypted-API call + decrypt + image
        rendering — we just read the result. Bypasses the encrypted
        ``/api/v1/chapters/{id}`` endpoint entirely because its ``_=`` request
        token is minted by the same VM-obfuscated ``secure-*.js`` that does
        decryption, and we cannot reliably mint it statically.

        Composite-id contract: ``chapter_id`` is the
        ``"{numeric_id}|{hid}|{slug}|{number}"`` composite the search step
        encoded into the handle's ``ResolutionRecord.chapter_id``. We decode
        here, construct the live chapter URL, and call
        :meth:`solver.fetch_via_browser` with a JS extractor that returns the
        rendered ``/{seg}/{token}/{NN}.{ext}`` image URLs in NN order (``{seg}``
        is a rotating short path segment — ``si``/``i3``). A malformed
        composite or an empty page list raises
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
        try:
            urls = await solver.fetch_via_browser(
                chapter_url,
                extract=_CHAPTER_PAGES_EXTRACT_JS,
                # Issue #20: pass wait_for=None and let the JS extractor's
                # own Step-1 scaffold wait do the readiness check. A Python-
                # side wait_for_selector AND a JS-side scaffold poll would
                # double-wait the same condition; the JS poll runs inside
                # page.evaluate which Playwright is happy to schedule
                # immediately after goto commits.
                wait_for=None,
                # debug comix-manifest-60s-timeout (2026-06-03): the extractor
                # no longer walks pages serially — it does a cheap two-scroll
                # capture (~1s) then SYNTHESIZES the full 1..N manifest from a
                # captured page-image template, so resolve is now O(1) in pages
                # (a couple seconds regardless of chapter length). The old
                # O(pages) scrollIntoView walk burned ~1.55s/page and blew this
                # ceiling on long chapters in the single-page reader. The 60s
                # ceiling stays as a generous safety margin for the scaffold
                # wait + Cloudflare/first-paint tail; the per-source rate
                # limiter bounds outer cadence.
                timeout=60.0,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a typed source failure
            raise SourceError(
                "source_unavailable", f"browser manifest fetch failed: {exc}"
            ) from exc
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
    def _decode_enc_prefix(data: bytes, seed: int, enc_len: int) -> bytes:
        """Return ``data`` with its first ``enc_len`` bytes LCG-XOR-decoded.

        Spike-012-verified static cipher: ``seed == 0`` is a no-op (returns ``data``
        unchanged — the ~75% plaintext pages); otherwise a 32-bit LCG keystream is
        XORed over the prefix, taking the TOP byte of each advanced state. Pure stdlib,
        bit-exact against the spike's captured (ciphertext, seed, plaintext) vector.
        """
        if seed == 0:
            return data
        out = bytearray(data)
        state = seed & _ENC_MASK
        for i in range(min(enc_len, len(out))):
            state = (state * _ENC_MULTIPLIER + _ENC_INCREMENT) & _ENC_MASK
            out[i] ^= (state >> 24) & 0xFF
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
        ``str.isdecimal()`` accepts only digit runs (rejecting ``-1``, ``+1``, ``0x..``,
        whitespace-only), and still admits the large unsigned 32-bit seeds Comix sends.
        """
        if raw is None:
            return 0
        value = raw.strip()
        if not value.isdecimal():
            return 0
        return int(value)

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes via the shared session (PKG-02).

        Delegates to ``ctx.get_bytes_plain_with_headers`` — cleared by the framework
        seam (D-40), the framework decrypt seam opted out — so the source sees the raw
        CDN bytes PLUS the response headers. The Comix CDN now serves byte-encrypted
        WebP on every 4th page (``x-enc-seed != 0``): we decode statically over the
        first ``x-enc-len`` bytes per the spike-012-verified cipher. The ~75% plaintext
        pages (``x-enc-seed == 0`` / missing / malformed header) pass through
        byte-for-byte (the fast path). Still NO browser for image bytes, NO re-encode —
        pages stay WebP.

        ``x-enc-len`` is clamped to ``_ENC_LEN_MAX`` (T-iy5-01) so a hostile header
        cannot force a whole-image XOR loop. The clamped (≤65536-byte) integer loop is
        trivial CPU (sub-millisecond) and runs inline on the event loop — NO
        ``asyncio.to_thread`` offload is needed (unlike the Pillow/zipfile packaging
        path). ``httpx.Headers.get`` is case-insensitive, so the lowercase header names
        match regardless of wire casing.
        """
        data, headers = await ctx.get_bytes_plain_with_headers(url)
        seed = self._enc_header_int(headers.get("x-enc-seed"))
        if seed == 0:
            return data  # plaintext page — untouched (fast path)
        enc_len = self._enc_header_int(headers.get("x-enc-len"))
        if enc_len <= 0:
            enc_len = _ENC_LEN_DEFAULT
        enc_len = min(enc_len, _ENC_LEN_MAX)
        return self._decode_enc_prefix(data, seed, enc_len)

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
    def _solver_from_ctx(ctx: SourceContext) -> Any:
        """Pull the AntiBotSolver out of ``ctx`` for the browser-fetch paths.

        The framework wires the solver into ``SourceContext`` for any
        ``cloudflare*`` source (D-40 clearance injection). For Comix's browser-
        DOM reads we ALSO need TWO off-Protocol browser primitives (D-41), same
        instance, distinct from the request-clearance use:

        * ``fetch_via_browser_paginated`` — the always-walk chapter-list
          enumeration in :meth:`_series_chapters` (#146); and
        * ``fetch_via_browser`` — the one-shot chapter-pages manifest read in
          :meth:`fetch_manifest`.

        Raises ``SourceError`` when the solver is missing OR lacks EITHER
        primitive (a wiring bug, not a runtime condition).
        """
        solver = getattr(ctx, "_solver", None)
        if (
            solver is None
            or not hasattr(solver, "fetch_via_browser")
            or not hasattr(solver, "fetch_via_browser_paginated")
        ):
            raise SourceError(
                "source_unavailable",
                "comix browser-fetch requires a solver with fetch_via_browser "
                "and fetch_via_browser_paginated",
            )
        return solver

    # ─────────────────────────── Comix fetch helpers ──────────────────────────

    async def _search_series(
        self, query: str, limit: int, ctx: SourceContext
    ) -> list[tuple[str, str, str, list[str]]]:
        """PLAINTEXT search → ``(hid, slug, title, alt_titles)`` via ``/api/v1/manga``.

        Returns ``(hid, slug, title, alt_titles)`` tuples. The 5-char ``hid`` is
        the canonical series identifier; the ``slug`` is extracted from the
        item's ``url`` field (``/title/{hid}-{slug}`` per live recon); the
        ``title`` is the rendered series title and is threaded through to
        ``_to_release`` so the per-chapter Release carries the manga title (the
        browser-DOM chapter rows do not repeat the series title — it's on the
        series-page header). ``alt_titles`` are the item's ``altTitles`` (a clean
        ``list[str]`` from the same payload, e.g. the Korean native name) and feed
        the alt-title-aware prune (#139) — nothing extra is fetched. Plain query
        params; the ``order[relevance]=desc`` and ``content_rating=suggestive``
        match what the public site sends.
        """
        params: dict[str, Any] = {
            "keyword": query,
            "limit": min(limit or _SEARCH_PAGE_SIZE, _SEARCH_PAGE_SIZE),
            "page": 1,
            "content_rating": _CONTENT_RATING,
            # httpx encodes ``order[relevance]`` as a bracketed key by default; the
            # live API tolerates either bracketed or repeated keys.
            "order[relevance]": "desc",
        }
        # PLAINTEXT endpoint (live recon) — use get_json_plain so the framework
        # decrypt seam stays out of this path.
        data = await ctx.get_json_plain(f"{self.base_url}/api/v1/manga", **params)
        items = self._result_items(data)
        out: list[tuple[str, str, str, list[str]]] = []
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
            out.append((str(hid), slug, title, alt_titles))
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
        """The RAW, newest-first, FULLY-WALKED chapter list off the warm series
        page (no filter, no slice).

        This is the EXPENSIVE unit (a 7-18s browser navigation that ALWAYS walks
        the FULL paginated chapter list, #146) and so the unit the Layer-2
        enumeration cache stores: it navigates ``{base_url}/title/{hid}-{slug}`` in
        the warm Patchright browser, waits for the chapter-list anchors to hydrate,
        walks every pagination page, normalizes the rows to the dict shape
        ``_to_release`` consumes, and sorts newest-first by chapter number. It
        applies NEITHER the ``chapter_matches`` floor filter NOR the offset/limit
        slice — those live one level up (``_series_chapters`` for the
        ``fetch_manifest`` path; ``search()`` for the cached search path) so the
        cached enumeration is the complete, unfiltered list (CACHE-02). Because the
        walk is COMPLETE, ``search()`` marks the cached ``Enumeration`` exhausted.

        #146: ALWAYS walk the FULL paginated chapter list, not just the ~20 rows on
        first paint. The series page renders only the newest chapter's group-uploads
        initially, so a low/old chapter (e.g. #5) lives on a later pagination page
        and was never enumerated by the old one-shot read. The generic paginated
        primitive (a) rewrites the chapter-list request's ``limit`` to 100 via
        ``page.route()`` before goto, and (b) walks the in-page Next control WITHIN
        ONE page nav (no extra goto, no extra Cloudflare cost). All comix-side
        literals — the ``/chapters`` URL substring, the ``100`` target limit, the
        ``button[aria-label*="Next"]`` selector — live HERE, never in the framework.

        PERF NOTE (accepted, user direction 2026-06-06): always-walking adds ~3-4
        in-page Next-clicks per series (a ~320-chapter series at 100 rows/page)
        WITHIN one navigation — a handful of JS-clicks + bounded DOM polls, no extra
        goto/clearance. Bounded by the 30s primitive timeout (≤ the framework's
        30s per-source fan-out timeout, ``framework/fanout.py::_DEFAULT_TIMEOUT``)
        with headroom over the ~7-18s live baseline; the per-source aiolimiter still
        bounds outer cadence. A failed fetch surfaces as
        ``SourceError("source_unavailable")`` → per-source warning (WR-06).
        """
        solver = self._solver_from_ctx(ctx)
        series_url = f"{self.base_url}/title/{series_hid}-{series_slug}"
        try:
            raw = await solver.fetch_via_browser_paginated(
                series_url,
                extract=_CHAPTER_LIST_EXTRACT_JS,
                wait_for=_CHAPTER_LIST_WAIT_FOR,
                next_selector='button[aria-label*="Next"]',
                route_limit_rewrite=("/chapters", _MAX_FEED_LIMIT),
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001 — surface as typed source failure
            raise SourceError(
                "source_unavailable", f"browser chapter-list fetch failed: {exc}"
            ) from exc
        if not isinstance(raw, list):
            raise SourceError("source_unavailable", "malformed chapter list")
        # Normalize to the dict shape ``_to_release`` already consumes (the
        # encrypted-API path produced the same shape, so the consumer is
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
        """Browser-DOM read of the FULL paginated series chapter list (#146).

        Navigates ``{base_url}/title/{hid}-{slug}`` in the warm Patchright
        browser and ALWAYS walks the FULL paginated chapter list via
        ``solver.fetch_via_browser_paginated`` — reading
        ``[{id, chapter, lang, groups, publishedAtRelative}, …]`` off every
        pagination page, not just the ~20 rows on first paint. Before #146 this
        was a one-shot read of the first render (mostly the newest chapter's
        group-uploads), so a low/old chapter that only appears on a later
        pagination page was never enumerated. The numeric chapter id (URL
        leading segment) and chapter number (URL trailing segment after
        ``-chapter-``) are load-bearing — group/lang/date are best-effort
        extracted and the live smoke pins selector refinements.

        We sort newest-first by chapter number and slice the
        ``offset..offset+limit`` window AFTER the full enumeration so the
        contract behaves identically to the prior path (now over the complete
        list). A failed browser fetch surfaces as
        ``SourceError("source_unavailable")`` → per-source warning (WR-06).

        Scanlation-group extraction: each chapter row is a
        ``<div class="mchap-row">`` carrying an ``<a class="mchap-row__group">
        …<span>{group_name}</span></a>``. The DOM extractor reads the name
        directly off that anchor; a row that omits the anchor simply yields
        an empty ``groups`` list (``scanlationGroup`` stays ``null``).

        Publish-date extraction (Issue #30): each chapter row carries
        ``<span class="mchap-row__time">`` whose text is the rendered relative
        time ("14h ago", "3mos ago"). The absolute timestamp is NOT in the
        DOM. The JS extractor captures the relative text and ``_to_release``
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
