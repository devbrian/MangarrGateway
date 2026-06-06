"""MangaBall source — the third declarative source + a third prep style (SRC-01).

MangaBall (``https://mangaball.net``) is a **MangaDex-class** source: a clean
JSON-REST backend with no response encryption and plain-CDN ``.jpg`` images
(RECON TL;DR). ~90% of this module is the MangaDex shape (guid/mint,
``_parse_decimal``, the manifest-integrity guard).

The one genuinely-new framework capability MangaBall exercises is an
**HTML→CSRF/session bootstrap**: its ``POST /api/v1/...`` form endpoints reject
any call lacking a session-bound ``X-CSRF-Token`` (harvested from a
``<meta name="csrf-token">`` on any HTML page, alongside the ``PHPSESSID``
cookie). That bootstrap is declared here purely as the ``session_prep =
"csrf-bootstrap"`` class-attr (D-06) — the framework (Plans 01/02) owns the
GET-HTML, token harvest, header injection, and CSRF-403 refresh-and-retry. This
module adds **ZERO networking glue**: every outbound call is ``ctx.post_json`` /
``ctx.get_bytes``, exactly like MangaDex's ``ctx.get_json`` (SRC-01/SRC-02).

Anti-bot caveat (D-12): MangaBall fronts Cloudflare in **passive** mode only on
the residential IP recon was run from — no interactive challenge fired on any data
endpoint, so ``antibot = "none"`` is correct. A datacenter IP MAY trip a managed
challenge; the escalation path is the existing Patchright clearance seam (flip
``antibot`` to ``"cloudflare"`` + set ``cloudflare_challenge_url``). The
``rate_limit_per_minute = 480`` is a conservative ~50%-of-floor value set from the
2026-06-04 probe (no hard limit found; manifest/image sustained 960/min at c=8),
mirroring the mangadot precedent (#101).

ENDPOINT SHAPES (live-recon-pinned, ``07-RECON-mangaball.md`` / GAP-1 probe):

* base: ``https://mangaball.net``
* search: ``POST /api/v1/title/search-advanced/`` (form) →
  ``{code,message,data:[Title…],pagination}``. **TITLE-ONLY** — a Title carries
  NO ``chapters`` key (GAP-1 ground truth). Chapters/translations live ONLY in
  ``chapter-listing-by-title-id``; ``search`` deep-enumerates each candidate.
* recent: ``POST /api/v1/title/search/`` (form,
  ``search_type=getRecentlyUpdatedChapter``) → same TITLE-ONLY shape, newest-first.
  The newest chapter is an HTML blob in each title's ``last_chapter`` field — it
  carries the real ``translation_id`` (``href=".../chapter-detail/{id}/"``),
  number, language flag, and group anchor. ``recent`` parses it and mints DIRECT
  releases (no deferral — MangaBall exposes the stable id, unlike Comix).
* chapter listing: ``POST /api/v1/chapter/chapter-listing-by-title-id/`` (form,
  ``title_id``) → the FLAT ``{code,message,ALL_CHAPTERS:[…],…}`` envelope (NOT
  the standard ``data`` envelope — :func:`_items_and_pagination` dispatches both,
  D-09).
* manifest: ``GET /chapter-detail/{translation_id}/`` (HTML) → the ordered page
  URLs in the client-side ``const chapterImages = JSON.parse(`[…]`)`` array (GAP-3,
  live — NOT ``<img>`` tags). The CDN host VARIES per content
  (``chikorita.red-and-blue.net``, ``bulbasaur.poke-black-and-white.net``, …) —
  the host is read from that array, NEVER reconstructed (RECON §4 / CLAUDE.md SSRF).
* image: plain httpx ``GET`` of each absolute CDN ``.jpg``.

guid (D-08): ``mangaball:{title_id}:ch-{number_float}:{language}:{translation_id}``
— the language + translation id are required because one chapter number maps to N
translations (one per language/group). Both ``search`` and ``recent`` now mint a
real ``translation_id`` into ``ResolutionRecord.chapter_id`` (DIRECT). The
``:DEFERRED`` late-bind pattern remains a **Comix-only** technique (see comix.py);
MangaBall does not need it because its recent feed exposes the translation_id.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import lxml.html

from ..framework.base import Source
from ..framework.errors import SourceError
from ..framework.relevance import prune_candidates
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# Default content rating + sort filters observed on the live search-advanced XHR
# (RECON §1). The keyword rides ``search_input``; the rest are the page's defaults.
_SEARCH_DEFAULT_FILTERS: dict[str, Any] = {
    "filters[sort]": "updated_chapters_desc",
    "filters[page]": 1,
    "filters[tag_included_mode]": "and",
    "filters[tag_excluded_mode]": "and",
    "filters[contentRating]": "any",
    "filters[demographic]": "any",
    "filters[person]": "any",
    "filters[originalLanguages]": "any",
    "filters[publicationYear]": "",
    "filters[publicationStatus]": "any",
    "filters[userSettingsEnabled]": "false",
}

# search() ALWAYS deep-enumerates this many title candidates (GAP-1 lock). The
# MangaDex 15-interactive escalation is intentionally DROPPED for MangaBall —
# ``req.interactive`` does NOT change the candidate count (each candidate is a full
# chapter-listing fan-out, so the count is held fixed regardless of interactivity).
_DEFAULT_TITLE_CANDIDATES = 5

# Bounds the per-candidate ``chapter-listing-by-title-id`` fan-out in ``search`` —
# at most this many chapter-listing fetches run concurrently. The candidates were
# fetched serially before this change (the IDENTICAL shape MangaDot had before the
# #101 fix); 6 collapses the wall-clock while staying well under the per-source rate
# budget. Mirrors MangaDot's ``_CHAPTERS_FANOUT_CONCURRENCY``.
_CHAPTERS_FANOUT_CONCURRENCY = 6

# Floor for empty/malformed timestamps so they sort oldest and never crash the
# `since` comparison (mirrors recent.py:_TS_FLOOR / _parse_ts).
_TS_FLOOR = datetime.min.replace(tzinfo=UTC)

# Relative-time parse for the recent feed's ``last_chapter`` dates ("1d ago",
# "3h ago", "5m ago"). Best-effort → absolute ISO publishDate (see
# :func:`_relative_to_iso`). Dates in the recent feed are RELATIVE (GAP-1 probe).
# WR-02: the unit alternation tries the LONGER tokens first (``mo``/``min`` before
# the bare ``m``) so ``"2mo ago"`` → months and ``"5min ago"`` → minutes regardless
# of the bare-``m`` mapping. OBSERVED convention (GAP-1 recon TL;DR sampled
# ``1d/3h/5m``): a bare ``m`` is MINUTES on MangaBall, months render as ``mo``. If a
# live re-run shows MangaBall rendering months as a bare ``2m`` instead, remap the
# bare ``"m"`` below to months — the explicit ``min`` token already covers minutes
# so the swap is isolated to one entry. The approximation is intentional and coarse
# (units like ``mo`` ≈ 30d); the route's ``since`` cut is the authoritative filter.
_RELATIVE_AGO_RE = re.compile(r"(\d+)\s*(mo|min|[smhdwy])\b", re.IGNORECASE)
_RELATIVE_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,  # OBSERVED: bare ``m`` = minutes on MangaBall (recon ``5m``); see WR-02
    "min": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "mo": 2592000,  # ~30d
    "y": 31536000,  # ~365d
}

# ``last_chapter`` HTML: the chapter-detail anchor carries the real translation_id.
# Extract it, NEVER reconstruct (CLAUDE.md SSRF) — fetch_manifest re-uses it as the
# chapter-detail path segment, then SSRF-allowlists every resulting image URL.
_CHAPTER_DETAIL_HREF_RE = re.compile(r"/chapter-detail/([^/?#]+)/?")
# Chapter number after a ``Ch.`` / ``Chapter`` label in the anchor text. Anchored to
# a clean ``N`` / ``N.M`` shape (WR-04): a greedy ``[\d.]+`` accepted ``"1.2.3"`` /
# trailing dots, which ``_parse_decimal`` then rejects → a ``ch-?`` guid or a silent
# title drop. ``\d+(?:\.\d+)?`` guarantees a ``_parse_decimal``-clean capture.
_CHAPTER_NUMBER_RE = re.compile(r"(?:ch(?:apter)?\.?)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# Flag-image language token: a BCP-47-ish ``xx`` / ``xx-yy`` code (WR-01). The
# ``last_chapter`` blob is NOT guaranteed to hold only a flag <img> — a preceding
# group-icon img (``alt="Rayquaza Group"``) would otherwise poison ``language``
# (breaking the ``[a-z-]`` guid shape and wrongly failing the recent language
# filter). Validate the token shape before accepting it as a language.
_LANG_TOKEN_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2,})?$")


def _parse_ts(raw: str) -> datetime:
    """Parse a timestamp to an aware datetime (WR-01), mirroring ``recent.py``.

    The source-side ``since`` cut (RCNT-02) must compare PARSED datetimes, never
    raw strings: MangaBall translation dates are space-separated
    (``"2026-06-01 23:33:42"``) while Mangarr's ``since`` is normally ISO-8601
    with a ``T`` separator (``"2026-06-01T20:00:00+00:00"``). A lexical compare
    sorts the space byte (0x20) before ``T`` (0x54), so a genuinely-newer
    space-separated date would compare ``<= since`` and be silently dropped.
    ``datetime.fromisoformat`` accepts BOTH separators (Python 3.11+), so parsing
    both sides removes the mismatch. Empty/malformed values floor to epoch-min so
    they compare as oldest rather than raising.
    """
    if not raw:
        return _TS_FLOOR
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return _TS_FLOOR
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# SSRF allowlist for the extracted page-image URLs (T-07-07/T-07-09, CLAUDE.md).
# The CDN host VARIES per content (RECON §4, live e.g. ``chikorita.red-and-blue.net``,
# ``bulbasaur.poke-black-and-white.net``, ``jigglypuff.poke-black-and-white.net``) so
# — unlike Comix — it cannot be pinned to one literal. GAP-3 (live W-04): the PAGE
# FILENAME also varies per upload source and CANNOT be pinned either — the live array
# carries ``01.jpg`` (zero-padded), ``{translationId}-001.jpg`` (id-prefixed),
# ``HRK0MmP.png`` (opaque token), ``…-001.webp``, etc. across the ``daomeoden`` /
# ``comick`` / ``mangadex`` group dirs. Pinning a filename shape only false-rejects
# real pages and adds NO real SSRF protection (a host-compromise attacker controls the
# filename too). The meaningful, stable invariants are what we enforce: ``https`` +
# public host (host regex + internal-suffix reject + no traversal, see
# :func:`_is_allowed_image_url`) + the ``/storage/`` namespace + an image extension.
# The site logo (``/public/.../logo.svg`` — not ``/storage/``) and covers
# (``/covers/...``) still fail the ``/storage/`` prefix; group icons under
# ``/storage/`` are same-origin and harmless (and never reach here — extraction scopes
# to the ``chapterImages`` array, this is defense-in-depth).
_MANGABALL_IMG_PATH_RE = re.compile(
    r"^/storage/[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_MANGABALL_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)
# Internal/metadata host suffixes that the broad host regex would otherwise accept
# (``metadata.google.internal``, ``foo.local`` etc.). The host is NOT pinned to a
# literal, so we must reject the non-public namespaces explicitly (CR-01 / SSRF).
_MANGABALL_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".localhost")

# Page images are injected client-side as a JS template-literal JSON array (GAP-3,
# live W-04): ``const chapterImages = JSON.parse(`["https://<cdn>/.../01.jpg", …]`)``.
# Capture the bracketed JSON array — page-image URLs never contain ``]``, so the
# non-greedy ``\[.*?\]`` stops exactly at the array close. DOTALL so a multi-line
# array still matches. See :func:`_extract_chapter_image_urls`.
_CHAPTER_IMAGES_RE = re.compile(
    r"chapterImages\s*=\s*JSON\.parse\(\s*`(?P<json>\[.*?\])`",
    re.DOTALL,
)


def _items_and_pagination(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Two-envelope dispatch (D-07/D-09): standard ``data`` vs flat ``ALL_CHAPTERS``.

    Most MangaBall endpoints return the standard ``{code,message,data,pagination}``
    envelope, but ``chapter-listing-by-title-id`` is FLAT — the chapter list is a
    top-level ``ALL_CHAPTERS`` key with no ``pagination`` (RECON §3, Pitfall 5).
    Returns ``(items, pagination)``; ``ALL_CHAPTERS`` takes precedence and yields a
    ``None`` pagination. A non-list ``data`` degrades to ``[]`` rather than raising
    (defensive — a malformed envelope must not crash the parse).
    """
    if "ALL_CHAPTERS" in body:
        items = body.get("ALL_CHAPTERS", [])
        return (items if isinstance(items, list) else []), None
    data = body.get("data")
    return (data if isinstance(data, list) else []), body.get("pagination")


# ─────────────────────── recent() last_chapter HTML parse (GAP-1) ────────────


def _relative_to_iso(raw: str) -> str | None:
    """Best-effort convert a relative ``last_chapter`` date → absolute ISO-8601.

    The recent feed renders chapter dates RELATIVE ("1d ago", "3h ago", "5m ago",
    "2mo ago"; GAP-1 probe), so there is no absolute timestamp to read. We subtract
    the parsed offset from ``now(UTC)`` to get an APPROXIMATE absolute publishDate
    — close enough for the route's newest-first sort + ``since`` cut (which is the
    authoritative filter). Returns ``None`` when no relative token is found so the
    caller can fall back to the title's ``updated_at`` or ``now``. The approximation
    is intentional and documented (units are coarse, e.g. "mo" ≈ 30d).
    """
    if not raw:
        return None
    match = _RELATIVE_AGO_RE.search(raw)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = _RELATIVE_UNIT_SECONDS.get(unit)
    if seconds is None:
        return None
    moment = datetime.now(UTC) - timedelta(seconds=amount * seconds)
    return moment.isoformat()


def _parse_last_chapter(html: str) -> dict[str, Any] | None:
    """Parse ONE recent-feed ``last_chapter`` HTML blob → release fields (GAP-1).

    Blocking (lxml C-parse) by design — the caller offloads it via
    ``asyncio.to_thread`` (RESEARCH Pitfall 6 / ruff ASYNC), mirroring
    :func:`_extract_chapter_image_urls`. Extracts, defensively:

    * ``translation_id`` — the trailing path segment of the ``chapter-detail/{id}/``
      anchor href (regex-captured, NEVER reconstructed — CLAUDE.md SSRF). REQUIRED;
      returns ``None`` when absent.
    * ``number`` — the digits/decimal after ``Ch.``/``Chapter`` in the anchor text.
      REQUIRED; returns ``None`` when absent (a chapter without a number cannot mint
      a sane guid).
    * ``language`` — the ``alt``/``title`` of the flag ``<img>``, accepted ONLY when
      it matches a BCP-47-ish ``xx``/``xx-yy`` token (a preceding non-flag img must
      not poison it — WR-01); falls back to ``"en"``.
    * ``group`` — the ``title`` (or text) of the ``/group/{slug}/`` anchor; ``None``
      when absent.
    * ``date_raw`` — the raw relative date text (e.g. "1d ago"), for
      :func:`_relative_to_iso`.

    Returns the field dict, or ``None`` when the blob has no resolvable
    chapter-detail id or no parseable chapter number.
    """
    if not html:
        return None
    try:
        doc = lxml.html.fragment_fromstring(html, create_parent="div")
    except Exception:
        return None

    translation_id: str | None = None
    number: str | None = None
    group: str | None = None
    date_raw = ""
    language = "en"

    for anchor in doc.iter("a"):
        href = anchor.get("href") or ""
        detail = _CHAPTER_DETAIL_HREF_RE.search(href)
        if detail is not None and translation_id is None:
            translation_id = detail.group(1)
            # The chapter number rides the chapter-detail anchor's text.
            num_match = _CHAPTER_NUMBER_RE.search(anchor.text_content())
            if num_match is not None:
                number = num_match.group(1)
        elif "/group/" in href and group is None:
            group = _strip_html(anchor.get("title")) or _strip_html(
                anchor.text_content()
            )

    if translation_id is None or number is None:
        return None

    # Language flag: alt/title on a flag <img>. Accept ONLY a BCP-47-ish token
    # (WR-01) — a preceding non-flag img (e.g. a group icon ``alt="Rayquaza Group"``)
    # must not poison ``language`` (which flows into the guid + the recent language
    # filter). Off-shape alt/title values are skipped; ``language`` stays ``"en"``.
    for img in doc.iter("img"):
        flag = (img.get("alt") or img.get("title") or "").strip().lower()
        if flag and _LANG_TOKEN_RE.match(flag):
            language = flag
            break

    # Best-effort relative date text anywhere in the blob.
    text = doc.text_content()
    ago = _RELATIVE_AGO_RE.search(text)
    if ago is not None:
        date_raw = ago.group(0)

    return {
        "translation_id": translation_id,
        "number": number,
        "language": language,
        "group": group,
        "date_raw": date_raw,
    }


class _TextExtractor(HTMLParser):
    """Stdlib HTML→text stripper for the HTML-string Title fields (RECON Gotchas).

    ``alternateName`` / ``status`` / ``last_chapter`` arrive as HTML strings; they
    must be stripped to plain text before flowing into a Release field. stdlib
    ``html.parser`` matches the Plan-01 session-prep parser choice (cheap, no new
    dependency for the small field strip — lxml is reserved for the large manifest
    parse). Collapses inter-tag whitespace to single spaces.
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._parts.append(text)

    @property
    def text(self) -> str:
        return " ".join(self._parts)


def _strip_html(raw: Any) -> str | None:
    """Strip HTML tags from a Title string field → plain text (or None).

    Returns ``None`` for ``None`` / empty / whitespace-only input so callers can
    fall back. Never raises (RECON Gotchas: ``alternateName`` / ``status`` /
    ``last_chapter`` are HTML and must never flow raw into a Release).
    """
    if raw is None:
        return None
    parser = _TextExtractor()
    parser.feed(str(raw))
    text = parser.text.strip()
    return text or None


def _split_alt(raw: Any) -> list[str]:
    """Split the ``alternateName`` HTML field into plain-text alt titles (#139).

    ``alternateName`` arrives as a ``/``-separated HTML string (e.g.
    ``ワンピース<span>/</span>OP``). Strip the HTML, split on ``/``, strip each
    piece, and drop empties. Returns ``[]`` for ``None``/empty input so the
    candidate simply carries no alt titles (behavior unchanged).
    """
    stripped = _strip_html(raw)
    if not stripped:
        return []
    return [piece.strip() for piece in stripped.split("/") if piece.strip()]


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` looks like a MangaBall CDN page image (SSRF allowlist).

    Belt-and-suspenders defence on every DOM-extracted manifest URL before the
    framework fetches it (T-07-07/T-07-09). Rejects non-HTTPS schemes, empty /
    malformed hosts, internal/metadata hostnames, path-traversal, and any path
    that does not match the observed ``/storage/.../{id}-{NNN}.jpg`` shape. The
    host is NOT pinned to a literal — the MangaBall CDN host varies per content
    (RECON §4) — so the path shape + ``https`` + host-namespace guard carry it.

    CR-01: validate the path httpx will ACTUALLY fetch, not the raw one. httpx
    normalizes ``..`` segments before issuing the request, so a raw path like
    ``/storage/../../etc/passwd-001.jpg`` would match the allowlist regex yet
    fetch ``/etc/passwd-001.jpg`` on that host. We reject any literal ``..``
    segment outright AND validate the ``posixpath.normpath``-resolved path. We
    also reject internal/non-public host namespaces (``.internal``/``.local``/
    ``.localhost``) and bare dotless hostnames (no public-TLD shape), which the
    broad host regex alone would accept (e.g. ``metadata.google.internal``).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith(_MANGABALL_INTERNAL_HOST_SUFFIXES):
        return False
    # Reject traversal outright; validate the NORMALIZED path httpx will fetch.
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return (
        parsed.scheme == "https"
        and bool(_MANGABALL_HOST_RE.match(host))
        and bool(_MANGABALL_IMG_PATH_RE.match(norm_path))
    )


def _extract_chapter_image_urls(html: bytes) -> list[str]:
    """Extract the ordered page-image URLs from chapter-detail HTML (GAP-3, live).

    The reader is rendered CLIENT-SIDE: the page images are NOT ``<img>`` tags
    (live W-04 — the recon ``img[data-src]`` assumption was wrong; the only ``<img>``
    on the page are the site logo + a group icon). The real page URLs live in a JS
    template-literal JSON array::

        const chapterImages = JSON.parse(`["https://<cdn>/storage/.../en/01.jpg", …]`);

    We capture that array and ``json.loads`` it — the array order IS the page order,
    so no DOM/document-order walk is needed. The host is taken verbatim from the CDN
    URL, NEVER reconstructed (RECON §4 / CLAUDE.md SSRF); every URL is allowlisted
    downstream by :func:`_is_allowed_image_url`. Blocking-free string work, but kept
    behind ``asyncio.to_thread`` at the call site for parity with the prior lxml path
    and to stay future-proof if a larger parse is reintroduced. Returns ``[]`` on any
    miss (no marker / malformed JSON), which the caller turns into a clear
    ``source_unavailable``.
    """
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    match = _CHAPTER_IMAGES_RE.search(text)
    if match is None:
        return []
    try:
        parsed = json.loads(match.group("json"))
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [u.strip() for u in parsed if isinstance(u, str) and u.strip()]


class MangaBallSource(Source):
    """MangaBall (mangaball.net) — antibot none + csrf-bootstrap session prep.

    A MangaDex-class clean-JSON source whose only new requirement is the
    ``csrf-bootstrap`` session-prep style (D-06): the framework GETs an HTML page,
    harvests the ``meta[name=csrf-token]`` + ``PHPSESSID``, and injects
    ``X-CSRF-Token`` + the cookie on every ``/api/v1`` POST. This source adds zero
    networking glue — see the module docstring.
    """

    key = "mangaball"
    name = "MangaBall"
    base_url = "https://mangaball.net"
    # Title-search only — MangaBall has no external metadata-id namespace (SRCH-07).
    id_types: list[str] = []
    # ALL_LANGUAGES recon set (14 langs; chapter-listing-by-title-id §3).
    languages = [
        "ar",
        "ca",
        "de",
        "en",
        "es",
        "es-la",
        "fr",
        "id",
        "it",
        "ja",
        "ko",
        "pt-br",
        "ru",
        "vi",
    ]
    # Probe-tuned (2026-06-04, PR #102 harness): two sweeps, ~6000 requests across
    # 6 residential proxy IPs found NO hard limit on MangaBall — zero
    # 429/403/Cloudflare-challenge/Retry-After on any endpoint (consistent with
    # ``antibot = "none"``). The manifest + image endpoints sustained 960/min cleanly
    # at concurrency 8 (a FLOOR — the true ceiling is higher). ``480`` is the
    # conservative ~50%-of-floor value, the same shape as the mangadot precedent
    # (#101). The CSRF-bootstrap ``search`` path (~3-5.5s/call) was latency/proxy-bound,
    # NOT a site throttle, so ``480`` here is gated by latency, not a rate ceiling.
    rate_limit_per_minute = 480
    # Per-source download-job concurrency (D-30 override): the 2026-06-04 probe
    # found manifest + image sustaining 960/min cleanly at concurrency 8 with zero
    # throttling, so chapter downloads (manifest/image paths) parallelize safely.
    # 3 mirrors the mangadot precedent (#101); the job manager clamps it to the
    # global max_concurrent_chapters. (The CSRF-bootstrap search path stays
    # sequential — this override governs downloads, not search.)
    max_concurrent_jobs = 3
    # Passive Cloudflare only on a residential IP (D-07/D-12) — see module
    # docstring's anti-bot caveat. No decrypt (plain .jpg, D-06).
    antibot = "none"
    decrypt_scheme = None
    # NEW class-attr (Plan 01, D-06): the framework maps this onto the shared
    # CsrfBootstrap provider (Plan 02). ``post_json`` injects the harvested
    # X-CSRF-Token + PHPSESSID on every /api/v1 form POST.
    session_prep = "csrf-bootstrap"
    supports_search = True
    supports_recent = True

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-(chapter×translation) Releases (SRCH-01..07, D-08).

        Two-call live flow (GAP-1 lock): ``search-advanced`` is TITLE-ONLY, so
        ``search`` ALWAYS deep-enumerates the first ``_DEFAULT_TITLE_CANDIDATES``
        title candidates via a per-candidate ``chapter-listing-by-title-id`` POST.
        The MangaDex 15-interactive escalation is DROPPED — ``req.interactive``
        does NOT change the candidate count for MangaBall.

        For each candidate the flat ``ALL_CHAPTERS`` listing is walked and ONE
        Release is minted per ``(chapter × translation)`` via :meth:`_to_release`
        (preserving multi-group-same-language: two ``en`` translations of one
        chapter → two distinct guids). Releases are language-filtered by
        ``req.languages``, ordered NEWEST-FIRST by translation ``date``, and sliced
        to ``req.limit`` PER candidate (mirror MangaDex's per-candidate feed bound).
        ZERO networking glue — both POSTs are ``ctx.post_json`` (SRC-01/02).
        """
        form: dict[str, Any] = {
            "search_input": req.query or "",
            **_SEARCH_DEFAULT_FILTERS,
        }
        body = await ctx.post_json(
            f"{self.base_url}/api/v1/title/search-advanced/", data=form
        )
        titles, _pagination = _items_and_pagination(body)

        dict_titles = [t for t in titles if isinstance(t, dict)]
        # Prune obviously-irrelevant candidates BEFORE the per-candidate
        # chapter-listing fan-out (#126): an exact-match query enumerates only
        # the one correct title; ambiguous queries still fan out to the cap (the
        # prune falls back to the historic ``[:_DEFAULT_TITLE_CANDIDATES]``).
        candidates = prune_candidates(
            dict_titles,
            req.query or "",
            # Score over the main name OR any alternate name (#139): a query that
            # matches only a title's native/alt name still prunes to it.
            keys=lambda t: [
                _strip_html(t.get("name")),
                *_split_alt(t.get("alternateName")),
            ],
            cap=_DEFAULT_TITLE_CANDIDATES,
        )
        # 260605-e9a deliverable 5: how many title candidates we deep-enumerate.
        ctx.candidates_enumerated = len(candidates)
        wanted_langs = set(req.languages) if req.languages else None
        per_candidate_limit = req.limit or 50

        # Bound the per-candidate chapter-listing fan-out (one Semaphore shared across
        # the dispatch; constructed HERE so it binds to the running loop, never at
        # import time).
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(title_id: str, manga_title: str) -> list[Release]:
            async with sem:
                listing = await ctx.post_json(
                    f"{self.base_url}/api/v1/chapter/chapter-listing-by-title-id/",
                    data={"title_id": title_id, "userSettingsEnabled": "false"},
                )
            all_chapters, _ = _items_and_pagination(listing)
            return self._chapters_to_releases(
                all_chapters,
                title_id,
                manga_title,
                wanted_langs,
                per_candidate_limit,
                ctx,
                req,
            )

        # Pre-filter candidates lacking a usable ``_id`` BEFORE dispatching tasks — a
        # candidate with no ``_id`` must not produce a task (preserves the old
        # ``if not title_id: continue`` skip).
        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for title in candidates:
            title_id = title.get("_id")
            if not title_id:
                continue
            title_id = str(title_id)
            manga_title = _strip_html(title.get("name")) or "Unknown"
            tasks.append(_fetch_candidate(title_id, manga_title))

        # gather, NOT TaskGroup: gather (return_exceptions=False) re-raises the FIRST
        # child exception UNCHANGED, so a SourceError from ctx.post_json propagates out
        # of search() exactly as the old sequential loop did, and framework/fanout.py's
        # ``except SourceError`` classifies it as the source's own code. TaskGroup wraps
        # child exceptions in an ExceptionGroup → fanout's ``except Exception`` →
        # "unexpected error" (changed classification, regression). gather also returns
        # results in submission order, so cross-candidate aggregation order is
        # byte-identical to the old loop.
        results = await asyncio.gather(*tasks)

        releases: list[Release] = []
        for chunk in results:
            releases.extend(chunk)
        return releases

    def _chapters_to_releases(
        self,
        all_chapters: list[Any],
        title_id: str,
        manga_title: str,
        wanted_langs: set[str] | None,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one candidate's flat ``ALL_CHAPTERS`` → per-translation Releases.

        Language-filtered, NEWEST-FIRST by translation ``date`` (parsed via
        :func:`_parse_ts`), sliced to ``limit``. Multi-group-same-language is
        preserved: distinct translation ids → distinct guids.

        GAP-2 (live): mint handles ONLY for the post-slice survivors. A long-running
        title (One Piece ≈ 1382 chapters × thousands of translations) would otherwise
        mint tens of thousands of handles per candidate — blowing past the
        ``HandleStore`` ``maxsize`` (10_000) so the TTLCache EVICTS the very handles
        attached to the releases we return, and a later ``POST /downloads`` for
        ``releases[0]`` resolves to a miss ("release no longer resolvable"). Collect
        sort keys first, slice to ``limit``, THEN mint — handle count per candidate is
        bounded by ``limit`` and the returned releases' handles always survive.
        """
        rows: list[tuple[datetime, Decimal | None, dict[str, Any]]] = []
        for chapter in all_chapters:
            if not isinstance(chapter, dict):
                continue
            number = self._parse_decimal(chapter.get("number_float"))
            # 260606-2ff: drop a non-matching chapter (all its translations) BEFORE it
            # enters `rows` → before the newest-first sort / [:limit] slice / handle mint
            # (preserves the GAP-2 mint-after-slice ordering). Gate-off = pass-through.
            if not self.chapter_matches(req, number):
                continue
            for translation in chapter.get("translations") or []:
                if not isinstance(translation, dict):
                    continue
                if not translation.get("id"):
                    continue  # no resolve unit → _to_release would drop it anyway
                language = str(translation.get("language") or "en")
                if wanted_langs is not None and language not in wanted_langs:
                    continue
                rows.append(
                    (_parse_ts(str(translation.get("date") or "")), number, translation)
                )
        rows.sort(key=lambda row: row[0], reverse=True)  # newest-first
        releases: list[Release] = []
        for _ts, number, translation in rows[:limit]:  # mint AFTER slice (GAP-2)
            rel = self._to_release(title_id, manga_title, number, translation, ctx)
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
        """Newest-first recent chapters → DIRECT releases (RCNT-01/02, GAP-1 lock).

        POSTs ``/api/v1/title/search/`` with ``search_type=getRecentlyUpdatedChapter``
        (TITLE-ONLY shape — no ``chapters`` key). For each title the newest chapter
        is an HTML blob in ``last_chapter`` carrying the real ``translation_id``,
        number, language flag, and group anchor; :func:`_parse_last_chapter` (lxml,
        offloaded via ``asyncio.to_thread`` per ruff ASYNC) extracts them and we mint
        a DIRECT Release whose ``ResolutionRecord.chapter_id`` is the bare
        translation_id (NOT a ``:DEFERRED`` composite — MangaBall does not need the
        Comix late-bind because the recent feed exposes the stable id).

        Dates in the recent feed are RELATIVE ("1d ago") so the absolute
        ``publishDate`` is a best-effort approximation (:func:`_relative_to_iso`,
        falling back to the title's ``updated_at`` then ``now``). The route applies
        the authoritative newest-first sort + ``since`` cut (recent.py); the
        source-side ``since`` comparison is therefore best-effort and is left to the
        route (a release always carries a parseable publishDate so the route keeps
        it). ``languages`` filters by the parsed flag language when supplied. Zero
        networking glue — ``ctx.post_json`` owns the transport (SRC-02).
        """
        form: dict[str, Any] = {"search_type": "getRecentlyUpdatedChapter", "page": 1}
        body = await ctx.post_json(f"{self.base_url}/api/v1/title/search/", data=form)
        titles, _pagination = _items_and_pagination(body)

        wanted_langs = set(languages) if languages else None
        releases: list[Release] = []
        for title in titles:
            if not isinstance(title, dict):
                continue
            title_id = title.get("_id")
            if not title_id:
                continue
            title_id = str(title_id)
            manga_title = _strip_html(title.get("name")) or "Unknown"
            last = await asyncio.to_thread(
                _parse_last_chapter, str(title.get("last_chapter") or "")
            )
            if last is None:
                continue
            language = last["language"]
            if wanted_langs is not None and language not in wanted_langs:
                continue
            publish_date = (
                _relative_to_iso(last["date_raw"])
                or self._title_updated_iso(title)
                or datetime.now(UTC).isoformat()
            )
            translation = {
                "id": last["translation_id"],
                "language": language,
                "group": {"name": last["group"]} if last["group"] else None,
                "date": publish_date,
                "pages": None,
            }
            number = self._parse_decimal(last["number"])
            rel = self._to_release(title_id, manga_title, number, translation, ctx)
            if rel is not None:
                releases.append(rel)
            # WR-03: do NOT break at ``limit`` over raw feed order. The route
            # (recent.py) applies the authoritative newest-first sort + ``since``
            # cut, then re-trims to the merged limit — an in-loop break in FEED
            # order would (a) hide a genuinely-newer title sitting past position
            # ``limit`` from that sort, and (b) combined with skip-on-unparseable
            # (the ``continue`` above), shrink the result below ``limit`` even when
            # more parseable titles exist further down. Consume the whole page;
            # ``since`` is intentionally ignored source-side (IN-01).
        return releases

    @staticmethod
    def _title_updated_iso(title: dict[str, Any]) -> str | None:
        """Best-effort absolute publishDate from the title's ``updated_at``."""
        parsed = _parse_ts(str(title.get("updated_at") or ""))
        return None if parsed == _TS_FLOOR else parsed.isoformat()

    # ───────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered page-image URLs, INTERNALLY (PKG-01/R6).

        Both ``search`` and ``recent`` now mint a BARE ``translation_id`` as the
        ``chapter_id`` (GAP-1 lock — recent no longer defers), so this is a straight
        ``translation_id`` → chapter-detail HTML → ordered allowlisted page URLs
        resolve. The ``chapterImages`` JSON-array extract + SSRF allowlist +
        pages-count guard live in :meth:`_manifest_for_translation`. (The Comix-only
        ``:DEFERRED`` late-bind pattern does not apply to MangaBall — the chapter id
        stays a bare ``translation_id``, never a composite.)

        #83/IN-03: the page-count integrity guard runs on BOTH the search and recent
        paths. The chapter's declared ``pages`` is captured at search/recent time on
        the ``ResolutionRecord`` and forwarded here by the engine as
        ``ctx.expected_pages`` (``None`` only when search never recorded a count, or
        for a job rehydrated post-restart — the guard then degrades to a no-op). This
        keeps the chapter id bare while still mirroring ``mangadex.fetch_manifest``'s
        length check, closing the gap where a search-grabbed chapter skipped it.
        """
        return await self._manifest_for_translation(chapter_id, ctx.expected_pages, ctx)

    async def _manifest_for_translation(
        self, translation_id: str, pages: int | None, ctx: SourceContext
    ) -> list[str]:
        """``chapterImages`` JSON extract + SSRF allowlist + pages guard (PKG-01).

        GETs ``/chapter-detail/{translation_id}/`` (HTML) via ``ctx.get_bytes``,
        extracts the ordered page URLs from the client-side ``chapterImages`` JSON
        array (GAP-3 — NOT ``<img>`` tags), and returns them. The CDN host is taken
        from that array, NEVER reconstructed (RECON §4 / CLAUDE.md SSRF) — the host
        varies per content. Every extracted URL is
        SSRF-allowlisted (:func:`_is_allowed_image_url`) before return; a
        non-allowlisted URL raises ``SourceError`` (no blind fetch, T-07-07). The
        extracted count is guarded against the chapter's ``pages`` when known
        (integrity guard, mirror ``mangadex.fetch_manifest``). The large HTML parse
        is offloaded via ``asyncio.to_thread`` so it never blocks the event loop
        (RESEARCH Pitfall 6; ruff ASYNC).
        """
        html = await ctx.get_bytes(f"{self.base_url}/chapter-detail/{translation_id}/")
        urls = await asyncio.to_thread(_extract_chapter_image_urls, html)
        if not urls:
            raise SourceError(
                "source_unavailable",
                f"no page images found in chapter-detail for {translation_id}",
            )
        for url in urls:
            if not _is_allowed_image_url(url):
                # Never fetch a non-allowlisted (off-host / off-shape) URL. Name the
                # offending URL so an allowlist/CDN-shape divergence is diagnosable
                # rather than opaque (live W-04 — the recon path shape was wrong).
                raise SourceError(
                    "source_unavailable",
                    f"chapter-detail image URL failed the SSRF allowlist: {url!r}",
                )
        if pages is not None and len(urls) != pages:
            raise SourceError(
                "source_unavailable",
                f"manifest integrity: extracted {len(urls)} images, "
                f"chapter declares {pages} pages",
            )
        return urls

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's raw bytes via the shared session (PKG-02).

        Delegates to ``ctx.get_bytes`` (mirror ``mangadex.fetch_image``): bounded by
        the per-job semaphore (Plan 03), NOT the per-source API limiter. No decrypt
        — MangaBall serves plain ``.jpg``. A ``Referer: https://mangaball.net/`` is
        added ONLY if live-verify (Plan 04) shows the CDN hotlink-protects with a
        bare GET 403 (A7 / D-discretion — default is no Referer).
        """
        return await ctx.get_bytes(url)

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        title_id: str,
        manga_title: str,
        chapter_number: Decimal | None,
        translation: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single ``translation`` (D-08)."""
        translation_id = translation.get("id")
        if not translation_id:
            return None
        translation_id = str(translation_id)

        language = str(translation.get("language") or "en")
        page_count = self._parse_int(translation.get("pages"))  # reliable; size is not
        publish_date = self._normalize_publish_date(translation.get("date"))
        group = translation.get("group")
        group_name = _strip_html(group.get("name")) if isinstance(group, dict) else None

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(
            manga_title, ch_str, language=language, group=group_name
        )
        # D-08: language + translation id needed — one chapter number maps to N
        # translations (one per language/group).
        guid = f"mangaball:{title_id}:ch-{ch_str}:{language}:{translation_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=translation_id,  # the chapter-detail/download unit
                language=language,
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=self._parse_int(translation.get("volume")),
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
            volume=self._parse_int(translation.get("volume")),
            language=language,
            scanlation_group=group_name,
            page_count=page_count,
            ids={
                "mangaballTitleId": title_id,
                "mangaballTranslationId": translation_id,
            },
        )

    @staticmethod
    def _build_title(
        manga_title: str,
        chapter: str,
        *,
        language: str | None,
        group: str | None,
    ) -> str:
        """Compose a MangaParser-parseable release title (REL-02), MangaDex shape."""
        parts = [manga_title, "-", f"Chapter {chapter}"]
        if language:
            parts.append(f"({language})")
        if group:
            parts.append(f"[{group}]")
        return " ".join(parts)

    @staticmethod
    def _normalize_publish_date(raw: Any) -> str:
        """Normalize a translation ``date`` → RFC3339 ``date-time`` (REL-03, GAP-2).

        The contract's ``Release.publishDate`` is ``format: date-time`` (RFC3339, ``T``
        separator). The live ``chapter-listing-by-title-id`` ``date`` is
        space-separated (``"2026-06-01 23:33:42"``) which fails schema conformance, so
        search emitted an invalid ``publishDate`` (live W-04). :func:`_parse_ts` accepts
        both separators (Python 3.11+) and yields an aware datetime; ``.isoformat()``
        re-serializes with the ``T`` separator. Empty/unparseable values floor to
        ``_TS_FLOOR`` (year 1), which — while valid date-time — is a nonsense publish
        date, so we fall back to ``now(UTC)``. Idempotent for the recent() path, whose
        ``date`` is already an ISO string.
        """
        parsed = _parse_ts(str(raw or ""))
        if parsed == _TS_FLOOR:
            return datetime.now(UTC).isoformat()
        return parsed.isoformat()

    @staticmethod
    def _parse_decimal(raw: Any) -> Decimal | None:
        """Parse a chapter number to Decimal (copied from MangaDex; SRCH-06)."""
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
