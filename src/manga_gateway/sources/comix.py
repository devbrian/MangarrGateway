"""Comix source — the first ``cloudflare+encrypted`` declarative source (SRC-06).

Subclasses :class:`~manga_gateway.framework.base.Source` exactly like
``mangadex.py``: it declares its D-13 metadata as class attributes and overrides the
four hooks (``search``/``recent``/``fetch_manifest``/``fetch_image``). ALL networking,
rate-limiting, retry, Cloudflare clearance injection (D-40), challenge re-solve (D-35),
and response decryption (D-39) live in the injected ``ctx`` — this module is just
Comix param shaping + response parsing. This is the reusability proof of the phase
(criterion #1): a new cloudflare+encrypted source is a declarative subclass with ZERO
new networking/glue, riding the Wave-1/2 seams.

Two anti-bot declarations distinguish Comix from MangaDex:

* ``antibot = "cloudflare+encrypted"`` — the framework injects the captured
  ``cf_clearance`` + matching UA per request and re-solves a challenge 403 (D-40/D-35).
* ``decrypt_scheme = "comix-v1"`` — the framework routes every response body through
  ``framework.decrypt`` (D-39); the concrete cipher is a browser-evaluated decrypt
  delegated to the warm Patchright solver (D-45). NOTE: in the Option A pivot
  (Plan 04-04 commit 2/3, 2026-05-30), the chapter-pages and chapter-list paths
  switched to browser-DOM read via ``solver.fetch_via_browser`` — the ``comix-v1``
  decrypt seam is no longer in the hot path for Comix BUT the registration stays
  as the documented seam-shape proof for future encrypted sources whose token+
  cipher problem CAN be split. The ``get_json_plain`` opt-out is what the still-
  plaintext ``/api/v1/manga`` search endpoint uses.

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
  ``<img src="https://{cdn}.store/si/{token}/{NN}.webp">`` tags off the DOM.
  The page's own JS handles token-mint + encrypted-API call + decrypt + render;
  we just read the result.
* image CDN: ``https://{cdn}.store/si/{32-char-token}/{NN}.webp`` — fetched via
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
  Comix has no public all-recent-chapters feed (the public "Recently Added"
  UI is a list-mangas-sorted-by-``chapter_updated_at`` view, not a chapter
  feed). The source now declares ``supports_recent = False`` so ``/caps``
  advertises the gap explicitly and clients can branch on it instead of
  guessing from the empty array.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..framework.base import Source
from ..framework.decrypt import DecryptError, register_scheme
from ..framework.errors import SourceError
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest


# ─────────────────────────── comix-v1 cipher (D-45) ───────────────────────────
#
# Browser-evaluated decrypt of the Comix encrypted-response envelope. The cipher
# (``comix-v1``) is a jsdefender/jscrambler VM stream cipher that derives runtime
# keys from ``navigator.appCodeName`` + a timestamp-shaped fingerprint — not
# statically reversible in v1. We reuse the warm ``CloudflareSolver`` (which has
# already loaded ``secure-*.js`` to pass the Cloudflare challenge) and call its
# ``decrypt`` method, which executes ``await globalThis.t(ciphertext)`` on the
# warm comix.to page.
#
# The solver is threaded through ``decrypt_config["solver"]`` by the framework at
# every ``SourceContext`` construction site; a missing solver is a wiring bug,
# not a recoverable condition. Registered at module import time so the framework
# decrypt registry sees ``"comix-v1"`` as soon as :mod:`sources.comix` loads.


@register_scheme("comix-v1")
async def _comix_v1_decrypt(body: bytes, config: dict[str, Any]) -> bytes:
    solver = config.get("solver")
    if solver is None or not hasattr(solver, "decrypt"):
        raise DecryptError(
            "comix-v1 requires 'solver' in decrypt_config (browser-evaluated, D-45)"
        )
    plaintext: bytes = await solver.decrypt(body)
    return plaintext


# Bound a title search's candidate series; interactive widens it (mirrors MangaDex).
_DEFAULT_SERIES_CANDIDATES = 5
_INTERACTIVE_SERIES_CANDIDATES = 15
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


# SSRF allowlist for image-CDN URLs returned by the browser-DOM extractor
# (CLAUDE.md: never fetch client-supplied / DOM-supplied URLs blindly). The JS
# regex in the extractor already enforces the `/si/{token}/{NN}.{ext}` path
# shape, but it cannot tell us anything about the *host* — a poisoned DOM
# response (or a future extractor regression) could still surface a path of
# the right shape on an off-domain host. Restrict the manifest to the
# observed Comix CDN: ``https://{sub}.wowpic\d+.store/si/{token}/{NN}.{ext}``.
# Subdomains seen live across recon: ``jdpw``, ``jloo``, etc. The pattern
# tolerates any non-empty alphanumeric subdomain and any wowpic shard digit.
_COMIX_CDN_HOST_RE = re.compile(r"^[a-z0-9-]+\.wowpic\d+\.store$", re.IGNORECASE)
_COMIX_CDN_PATH_RE = re.compile(
    r"^/si/[A-Za-z0-9_-]{16,}/\d+\.(webp|jpg|jpeg|png)$", re.IGNORECASE
)


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` looks like a Comix CDN page image (SSRF allowlist).

    Belt-and-suspenders defense atop the JS extractor's path filter: rejects
    cross-domain hosts, non-HTTPS schemes, and anything whose path does not
    match the expected ``/si/{token}/{NN}.{ext}`` shape. Called on every URL
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
# Matches the live-recon-observed pattern ``/si/{token}/{NN}.{ext}`` and filters
# out cross-site ad imagery (gravatar, postimg, etc.) so the manifest is the
# chapter pages ONLY. Numbering may have gaps (01,02,04,05,07,…) — the recon
# shows real chapter pages with gaps, NOT lazy-load artifacts; we sort by
# embedded NN so the gaps survive.
#
# Issue #20 (2026-05-30): the previous strategy walked pages SEQUENTIALLY —
# scrollIntoView one div, poll up to 4s for its <img src>, advance. For a
# 10-page chapter that meant ~10 × (scroll + lazy-load round-trip) of in-page
# JS, blowing the wall-clock to ~25s. The fix below fires every scrollIntoView
# UP FRONT (the IntersectionObserver fires per-div in rapid succession) and
# polls every div IN PARALLEL via Promise.all. Each watcher captures the src
# the FIRST time it appears, so a Swiper.js eviction after later scrolling
# does not lose a URL we already saw. Wall-clock collapses from O(pages) ×
# 1s to ~max single-page latency.
_CHAPTER_PAGES_EXTRACT_JS = """
  // Comix's chapter reader is a Swiper.js long-strip component
  // (`class="rpage rpage--long-strip rpage--ttb"`). Each page is wrapped in a
  // `<div class="rpage-page" data-page="N">` whose <img> child is LAZY-LOADED
  // by an IntersectionObserver. `window.scrollTo()` does not move the inner
  // Swiper viewport, so a blind window scroll only renders head/tail pages.
  //
  // Strategy: enumerate every `.rpage-page[data-page]` div, fire every
  // `scrollIntoView` up front (no awaits between them) so the lazy loader
  // observers fire concurrently, then poll every div IN PARALLEL for the
  // first <img> whose src matches the CDN pattern. The Map keyed by
  // `data-page` preserves chapter ordering even if Swiper later evicts a
  // page's <img> — we captured the src on its first appearance.
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const rx = /\\/si\\/([A-Za-z0-9_-]{16,})\\/(\\d+)\\.(webp|jpg|jpeg|png)$/i;

  // Step 1: wait until the page scaffold exists. The reader populates
  // `.rpage-page[data-page]` divs once it has the decrypted page list.
  for (let i = 0; i < 50; i++) {
    if (document.querySelectorAll('.rpage-page[data-page]').length > 0) break;
    await sleep(200);
  }

  const pageDivs = Array.from(document.querySelectorAll('.rpage-page[data-page]'))
    .sort((a, b) => {
      const an = parseInt(a.getAttribute('data-page') || '0', 10);
      const bn = parseInt(b.getAttribute('data-page') || '0', 10);
      return an - bn;
    });

  // Step 2: fire every scroll trigger UP FRONT — synchronous loop, no awaits.
  // `scrollIntoView` returns immediately; the IntersectionObserver callback
  // runs asynchronously when the page actually crosses the viewport. Firing
  // them all in succession schedules the observers concurrently rather than
  // one-blocking-at-a-time.
  for (const div of pageDivs) {
    div.scrollIntoView({ behavior: 'instant', block: 'center' });
  }

  // Step 3: spawn ONE watcher per page div and Promise.all them. Each watcher
  // polls its own div on a tight cadence for the FIRST <img> whose src
  // matches the CDN pattern, then resolves. Per-page budget (~4s) matches
  // the previous sequential cap, but because watchers run concurrently the
  // wall-clock is O(slowest-single-page), not O(pages × per-page).
  const seen = new Map();
  const watchers = pageDivs.map(async (div) => {
    const n = parseInt(div.getAttribute('data-page') || '0', 10);
    for (let attempt = 0; attempt < 40; attempt++) {
      for (const img of div.querySelectorAll('img')) {
        const src = img.currentSrc || img.src || '';
        const m = src.match(rx);
        if (m) {
          if (!seen.has(n)) seen.set(n, src);
          return;
        }
      }
      await sleep(100);
    }
  });
  await Promise.all(watchers);

  // Step 4 (fallback): sweep the document for any <img> the per-div walk
  // missed (covers reader-shape variants where the canonical `data-page`
  // wrapper is absent but images still match the CDN pattern).
  for (const img of document.querySelectorAll('img')) {
    const src = img.currentSrc || img.src || '';
    const m = src.match(rx);
    if (m) {
      const n = parseInt(m[2], 10);
      if (!seen.has(n)) seen.set(n, src);
    }
  }

  return Array.from(seen.entries())
    .sort((a, b) => a[0] - b[0])
    .map(e => e[1]);
"""

# CSS selector ``solver.fetch_via_browser`` waits for before reading the DOM.
# The chapter reader scaffolds `<div class="rpage-page" data-page="N">` for
# every page in the chapter once the encrypted page-list arrives and decrypts;
# images inside are lazy-loaded later via IntersectionObserver. Waiting for
# the first scaffold div (not the first image) ensures the extract starts as
# soon as the reader knows the chapter length, and the per-div scrollIntoView
# loop in _CHAPTER_PAGES_EXTRACT_JS triggers the actual image loads.
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
    }
    out.push({
      id: id,
      chapter: m[2],
      lang: 'en',
      groups: group ? [{ name: group }] : [],
      publishedAtRelative: publishedAtRelative
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
    """Comix — antibot ``cloudflare+encrypted``, decrypt ``comix-v1`` (SRC-06)."""

    key = "comix"
    name = "Comix"
    base_url = "https://comix.to"
    # Title-search fallback only — no external id namespace (SRCH-07).
    id_types: list[str] = []
    languages = ["en"]
    # CLAUDE.md: "Comix ~10" req/min — the per-source aiolimiter is keyed to this.
    rate_limit_per_minute = 10
    # caps.AntibotLevel already carries this literal (CAPS-02). The framework injects
    # clearance (D-40) + reconciles a challenge 403 (D-35) for any cloudflare* source.
    antibot = "cloudflare+encrypted"
    # The URL the framework solver navigates to so Cloudflare issues a
    # ``cf_clearance`` cookie + the warm decrypt/fetch page loads ``secure-*.js``
    # (D-45 / Option A). Read by the application wiring (app.py lifespan), not
    # by the framework solver itself.
    cloudflare_challenge_url = "https://comix.to/"
    # D-39/D-45: every encrypted response body is routed through framework.decrypt
    # which delegates to solver.decrypt() (browser-evaluated). The framework injects
    # ``solver`` into ``decrypt_config`` at each SourceContext construction site, so
    # this declaration stays empty (no source-supplied key material in v1).
    decrypt_scheme = "comix-v1"
    decrypt_config: dict[str, Any] = {}
    # Issue #31 (2026-05-30): Comix has NO public all-recent-chapters feed.
    # The public "Recently Added" UI is a list-mangas-sorted-by-
    # ``chapter_updated_at`` view (a series feed), not a chapter feed. The
    # ``recent`` hook returns an empty list (see the override below); this
    # declaration is what makes ``/caps`` advertise the gap honestly so
    # clients can branch on ``supportsRecent: false`` instead of guessing
    # from a silently-empty release array. A per-followed-series fan-out
    # would close this gap in a future plan; for now we tell the truth.
    supports_recent = False

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
        """
        _ = req.languages  # Comix is English-only (live recon); honored downstream
        count = (
            _INTERACTIVE_SERIES_CANDIDATES
            if req.interactive
            else _DEFAULT_SERIES_CANDIDATES
        )
        series = await self._search_series(req.query or "", count, ctx)

        releases: list[Release] = []
        feed_limit = min(req.limit or _MAX_FEED_LIMIT, _MAX_FEED_LIMIT)
        for series_hid, series_slug, series_title in series:
            chapters = await self._series_chapters(
                series_hid, series_slug, feed_limit, req.offset, ctx
            )
            for chapter in chapters:
                # Inject the series-page-known title into the chapter dict so the
                # SOURCE-AGNOSTIC ``_to_release`` (which reads ``seriesTitle`` /
                # ``series`` / ``title`` keys) does not need to know whether the
                # data came from the browser DOM or the legacy encrypted API.
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
        """Newest-first recent chapters across all series (RCNT-01/02).

        Comix has no public "all-recent-chapters" feed (the public site's
        "Recently Added" UI is a list-mangas-sorted-by-``chapter_updated_at``
        view, not a chapter feed). This hook returns an empty list and the
        class declares ``supports_recent = False`` so ``/caps`` advertises
        the gap explicitly (Issue #31). Recent-Comix coverage will come via
        a per-followed-series fan-out in a future plan (deferred); the
        framework's per-source isolation means an empty Comix recent is a
        no-op, not a contract failure.
        """
        _ = (languages, limit, since, ctx)  # unused — see docstring
        return []

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
        rendered ``/si/{token}/{NN}.{ext}`` image URLs in NN order. A malformed
        composite or an empty page list raises
        ``SourceError("source_unavailable")`` so it surfaces as a contract
        warning, never a raw KeyError (WR-06). The manifest is consumed only by
        the gateway's own engine — never returned to a caller (R6).

        The image-byte fetch (the next step in the engine) still runs through
        httpx (``ctx.get_bytes``) — the browser is NEVER used for bulk image
        fetch (CLAUDE.md).
        """
        try:
            numeric_id, hid, slug, number = self._parse_composite_chapter_id(chapter_id)
        except ValueError as exc:
            raise SourceError(
                "source_unavailable", f"malformed comix chapter id: {exc}"
            ) from None
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
                # Issue #20: per-div watchers now run in PARALLEL (Promise.all)
                # so wall-clock collapses from O(pages × ~1s) to ~max single-page
                # latency. The 30s ceiling still gives plenty of headroom for an
                # initial Cloudflare round-trip + slow CDN warm-up; a chapter
                # whose evaluate exceeds this is genuinely degraded, not slow.
                timeout=30.0,
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

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes via the shared session (PKG-02).

        Delegates to ``ctx.get_bytes_plain`` — cleared by the framework seam
        (D-40) but the decrypt seam is opted out: the Comix CDN
        (``https://{cdn}.store/si/{token}/{NN}.webp``) serves plaintext WebP,
        and the ``comix-v1`` scheme is a browser-eval cipher that does not
        apply to image bytes (and would corrupt them on the UTF-8 boundary
        when handed to ``page.evaluate``). The browser is NEVER used for
        image fetch (CLAUDE.md): the cleared httpx client does the bulk fetch,
        bounded by the per-job semaphore. The host + token come from the
        browser-DOM page-list (Option A pivot — Plan 04-04).
        """
        return await ctx.get_bytes_plain(url)

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
        """Pull the AntiBotSolver out of ``ctx`` for the browser-fetch path.

        The framework wires the solver into ``SourceContext`` for any
        ``cloudflare*`` source (D-40 clearance injection). For Comix's browser-
        DOM read we ALSO need ``solver.fetch_via_browser`` — distinct from the
        request-clearance use, same instance. Raises ``SourceError`` when the
        solver is missing OR lacks the primitive (a wiring bug, not a runtime
        condition).
        """
        solver = getattr(ctx, "_solver", None)
        if solver is None or not hasattr(solver, "fetch_via_browser"):
            raise SourceError(
                "source_unavailable",
                "comix browser-fetch requires a solver with fetch_via_browser",
            )
        return solver

    # ─────────────────────────── Comix fetch helpers ──────────────────────────

    async def _search_series(
        self, query: str, limit: int, ctx: SourceContext
    ) -> list[tuple[str, str, str]]:
        """PLAINTEXT search → ``(hid, slug, title)`` (D-46) via ``/api/v1/manga``.

        Returns ``(hid, slug, title)`` tuples. The 5-char ``hid`` is the canonical
        series identifier; the ``slug`` is extracted from the item's ``url`` field
        (``/title/{hid}-{slug}`` per live recon); the ``title`` is the rendered
        series title and is threaded through to ``_to_release`` so the per-
        chapter Release carries the manga title (the browser-DOM chapter rows do
        not repeat the series title — it's on the series-page header). Plain
        query params; the ``order[relevance]=desc`` and
        ``content_rating=suggestive`` match what the public site sends.
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
        # PLAINTEXT endpoint (live recon) — bypass the comix-v1 decrypt seam.
        data = await ctx.get_json_plain(f"{self.base_url}/api/v1/manga", **params)
        items = self._result_items(data)
        out: list[tuple[str, str, str]] = []
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
            out.append((str(hid), slug, title))
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

    async def _series_chapters(
        self,
        series_hid: str,
        series_slug: str,
        limit: int,
        offset: int,
        ctx: SourceContext,
    ) -> list[dict[str, Any]]:
        """Browser-DOM read of the series page chapter list (Plan 04-04 Option A).

        Navigates ``{base_url}/title/{hid}-{slug}`` in the warm Patchright
        browser, waits for the chapter-list anchors to hydrate, and reads
        ``[{id, chapter, lang, groups, publishedAtRelative}, …]`` off the
        rendered DOM. The numeric chapter id (URL leading segment) and chapter
        number (URL trailing segment after ``-chapter-``) are load-bearing —
        group/lang/date are best-effort extracted and the live smoke pins
        selector refinements.

        We sort newest-first by chapter number and slice the
        ``offset..offset+limit`` window so the contract behaves identically to
        the prior encrypted-API path. A failed browser fetch surfaces as
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
        solver = self._solver_from_ctx(ctx)
        series_url = f"{self.base_url}/title/{series_hid}-{series_slug}"
        try:
            raw = await solver.fetch_via_browser(
                series_url,
                extract=_CHAPTER_LIST_EXTRACT_JS,
                wait_for=_CHAPTER_LIST_WAIT_FOR,
                # Series page renders the first 20 chapters in <2s on a warm
                # context (recon-measured). Cap at 15s so the call stays inside
                # the framework's 20s per-source fan-out timeout when search
                # enumerates a series' chapters.
                timeout=15.0,
            )
        except Exception as exc:  # noqa: BLE001 — surface as typed source failure
            raise SourceError(
                "source_unavailable", f"browser chapter-list fetch failed: {exc}"
            ) from exc
        if not isinstance(raw, list):
            raise SourceError("source_unavailable", "malformed chapter list")
        # Normalize to the dict shape ``_to_release`` already consumes (the
        # encrypted-API path produced the same shape, so the consumer is
        # source-agnostic). Sort newest-first by chapter number when parseable;
        # then apply the offset/limit window.
        chapters: list[dict[str, Any]] = [c for c in raw if isinstance(c, dict)]
        chapters.sort(
            key=lambda c: self._parse_decimal(c.get("chapter")) or Decimal(0),
            reverse=True,
        )
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

        Image CDN pattern: ``https://{cdn}.store/si/{token}/{NN}.webp``.
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
