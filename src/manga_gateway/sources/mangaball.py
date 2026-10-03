"""MangaBall source — the third declarative source + a third prep style (SRC-01).

MangaBall (``https://mangaball.com``) is a **MangaDex-class** source: a clean
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

Anti-bot (D-12; debug mangaball-cloudflare-csrf-243, 2026-06-15): MangaBall
ORIGINALLY fronted Cloudflare in **passive** mode only on the residential recon IP —
no interactive challenge fired, so ``antibot = "none"`` was correct. As of 2026-06-15
MangaBall enabled a **site-wide managed challenge** (``cf-mitigated: challenge``,
HTTP 403 on ``/``, ``/manga``, ``/search``), which broke the bare-httpx csrf-bootstrap
GET: it received the interstitial → no ``meta[name=csrf-token]`` → ``ValueError`` →
``source_unavailable`` on every search/recent/download. Per the documented
one-attribute escalation, ``antibot`` is now ``"cloudflare"`` +
``cloudflare_challenge_url = "https://mangaball.com/"`` — routing BOTH the
csrf-bootstrap GET and every data POST through the shared clearance seam. Desktop
Chromium/Patchright could NOT clear the new managed challenge from our Linux
fingerprint (CI + the deploy both timed out at 60s), so ``solver_engine = "android"``
routes clearance to the redroid-WebView solver that already clears kagane/mangadot
(resolved debug ``mangadot-cf-linux-fingerprint``). The framework already unions cf +
csrf-bootstrap (``context.py:_clearance_kwargs``); ``session_prep.py:CsrfBootstrap``
now threads cf_clearance into the bootstrap GET too, so MangaBall stays
zero-networking-glue. The ``rate_limit_per_minute = 480`` is a
conservative ~50%-of-floor value set from the 2026-06-04 probe (no hard limit found;
manifest/image sustained 960/min at c=8), mirroring the mangadot precedent (#101).

ponytail: known limit (261003-mangaball-api-v2) — the ``*.poke-black-and-white.net``
CDN zone serves a Cloudflare managed challenge (403 ``cf-mitigated: challenge``) to
non-browser TLS fingerprints even with the Referer and via residential proxies, and
hard-blocks top-level navigation, so the android solver cannot mint a clearance for
it. Pages on that zone fail with ``upstream 403`` (after one D-35 forced re-solve
against mangaball.com, since the 403 is a CF challenge) until a WebView-side image
body capture exists (follow-up issue). ``*.red-and-blue.net`` serves plaintext.
Upgrade path = WebView-side image body capture.

ENDPOINT SHAPES (live-recon-pinned, ``07-RECON-mangaball.md`` / GAP-1 probe):

* base: ``https://mangaball.com``
  (moved from the ``.net`` host 2026-10-03, ``261003-mangaball-dotcom``: the old host
  301s every request and the new host 308s trailing-slash paths; the transport does
  NOT follow redirects, so every request path below is slash-free).
* search: ``POST /api/v1/title/search-advanced`` (form) →
  ``{code,message,data:[Title…],pagination}``. **TITLE-ONLY** — a Title carries
  NO ``chapters`` key (GAP-1 ground truth). Chapters/translations live ONLY in
  ``chapter-listing-by-title-id``; ``search`` deep-enumerates each candidate.
* recent: ``POST /api/v1/title/search`` (form,
  ``search_type=getRecentlyUpdatedChapter``) → same TITLE-ONLY shape, newest-first.
  The newest chapter is an HTML blob in each title's ``last_chapter`` field — it
  carries the real ``translation_id`` (``href=".../chapter-detail/{id}/"``),
  number, language flag, and group anchor. ``recent`` parses it and mints DIRECT
  releases (no deferral — MangaBall exposes the stable id, unlike Comix).
* chapter listing: ``POST /api/v1/chapter/chapter-listing-by-title-id`` (form,
  ``title_id``) → the FLAT ``{code,message,ALL_CHAPTERS:[…],…}`` envelope (NOT
  the standard ``data`` envelope — :func:`_items_and_pagination` dispatches both,
  D-09).
* manifest: ``GET /chapter-detail/{translation_id}`` (HTML) → the ordered page
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
import logging
import posixpath
import re
import string
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
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

# Default content rating + sort filters observed on the live search-advanced XHR
# (RECON §1). The keyword rides ``keyword`` (261003-mangaball-api-v2: the v2 backend
# IGNORES the old ``search_input`` key and returns every title); the rest are the
# page's defaults.
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

# WAF trigger-word denylist (260620-5yq). mangaball.com's WAF 403s ANY search POST
# whose ``keyword`` contains a SQL-injection-flavoured token with the
# ``Malicious payload detected`` body; the framework turns that into a catchable
# ``waf_blocked`` SourceError (context.is_waf_block). Only ``"system"`` is
# live-confirmed — add tokens here as more false-positives surface (a small frozenset
# kept trivially extensible; single strip-and-retry is the live-verified approach,
# NOT progressive multi-word stripping).
_log = logging.getLogger("manga_gateway")

_WAF_TRIGGER_WORDS = frozenset({"system"})

# Characters stripped off a token's edges when normalizing it to a denylist "core"
# (trailing comma/period/quote etc.), plus the unicode curly quotes the possessive
# strip below also handles.
_WAF_STRIP_CHARS = string.punctuation + "’‘"


def _sanitize_waf_query(query: str) -> str:
    """Drop WAF-trigger tokens from a search query (260620-5yq).

    Tokenizes on whitespace and, for each token, computes a normalized core —
    lowercased, with edge punctuation and a trailing possessive ``'s``/``’s``
    stripped — so ``"system's"`` and ``"system,"`` both normalize to ``"system"``.
    A token whose core is in :data:`_WAF_TRIGGER_WORDS` is DROPPED whole; every other
    token is kept VERBATIM (original casing/punctuation preserved). Plural/embedded
    forms (``"systems"``, ``"systemic"``) are NOT stripped — only the literal word
    matches. Survivors re-join with single spaces; the result is stripped.

    The caller (:meth:`MangaBallSource.search`) treats an empty or unchanged result as
    "nothing to retry" and short-circuits to a soft ``[]``.
    """
    survivors: list[str] = []
    for token in query.split():
        core = token.lower().strip(_WAF_STRIP_CHARS)
        if core.endswith(("'s", "’s")):
            core = core[:-2]
        if core in _WAF_TRIGGER_WORDS:
            continue
        survivors.append(token)
    return " ".join(survivors).strip()


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

# 261003-mangaball-api-v2: the v2 rows carry site language codes that are mostly
# BCP-47 already; only these two diverge from the codes Mangarr expects.
_LANG_MAP = {"kr": "ko", "cn": "zh"}


def _row_language(row: dict[str, Any]) -> str:
    """A v2 listing/recent row's ``lang`` → release language (lowercased, mapped)."""
    lang = str(row.get("lang") or "en").lower()
    return _LANG_MAP.get(lang, lang)


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
# ``avif`` added 260721: MangaBall's ``*.red-and-blue.net`` CDNs now serve page images
# as AVIF, and the extension-only pin was false-rejecting every real page at the SSRF
# allowlist (debug avif-image-ssrf-allowlist). Matches kagane's avif-inclusive pin.
_MANGABALL_IMG_PATH_RE = re.compile(
    r"^/storage/[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp|avif)$",
    re.IGNORECASE,
)
_MANGABALL_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)
# Internal/metadata host suffixes that the broad host regex would otherwise accept
# (``metadata.google.internal``, ``foo.local`` etc.). The host is NOT pinned to a
# literal, so we must reject the non-public namespaces explicitly (CR-01 / SSRF).
_MANGABALL_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".localhost")

# 261003-mangaball-api-v2: the CDN hotlink-blocks a bare image GET (403); the reader's
# Referer is required. Mirrors comix's ``_IMAGE_FETCH_HEADERS``.
_IMAGE_FETCH_HEADERS = {"Referer": "https://mangaball.com/"}


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

    The v2 backend sends ``alternateName`` as a LIST of plain strings
    (261003-mangaball-api-v2) — return its stripped non-empty items. The legacy
    shape (a ``/``-separated HTML string, e.g. ``ワンピース<span>/</span>OP``) is still
    accepted: strip the HTML, split on ``/``, strip each piece, drop empties.
    Returns ``[]`` for ``None``/empty input so the candidate carries no alt titles.
    """
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if isinstance(x, str) and x.strip()]
    stripped = _strip_html(raw)
    if not stripped:
        return []
    return [piece.strip() for piece in stripped.split("/") if piece.strip()]


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` looks like a MangaBall CDN page image (SSRF allowlist).

    Belt-and-suspenders defence on every chapter-detail manifest URL before the
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


class MangaBallSource(Source):
    """MangaBall (mangaball.com) — antibot cloudflare + csrf-bootstrap session prep.

    A MangaDex-class clean-JSON source whose requirements are the ``csrf-bootstrap``
    session-prep style (D-06: the framework GETs an HTML page, harvests the
    ``meta[name=csrf-token]`` + ``PHPSESSID``, and injects ``X-CSRF-Token`` + the
    cookie on every ``/api/v1`` POST) AND — since the 2026-06-15 site-wide Cloudflare
    managed-challenge escalation (debug mangaball-cloudflare-csrf-243) — the shared
    Patchright cf_clearance seam. It is the FIRST source to use the cf + csrf-bootstrap
    UNION; the framework still owns BOTH (this source adds zero networking glue — see
    the module docstring).
    """

    key = "mangaball"
    name = "MangaBall"
    base_url = "https://mangaball.com"
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
    # Cloudflare ESCALATION (debug mangaball-cloudflare-csrf-243, 2026-06-15):
    # MangaBall enabled a SITE-WIDE managed challenge (``cf-mitigated: challenge``,
    # HTTP 403 on /, /manga, /search — direct-probe confirmed). The D-07/D-12
    # residential-IP "passive Cloudflare" classification no longer holds: a bare httpx
    # GET now receives the interstitial, so the csrf-bootstrap GET failed with "no
    # meta[name=csrf-token]" → source_unavailable. Flipped "none" → "cloudflare" per
    # the documented one-attribute escalation (module docstring + the live profile):
    # this routes BOTH the csrf-bootstrap GET and every data POST through the shared
    # Patchright cf_clearance seam. MangaBall is the first source to use the cf +
    # csrf-bootstrap UNION (framework/context.py:_clearance_kwargs +
    # session_prep.py:CsrfBootstrap). No decrypt (plain .jpg, D-06).
    antibot = "cloudflare"
    decrypt_scheme = None
    # Host the shared CloudflareSolver solves the managed challenge against (#88
    # per-domain challenge URL), mirroring comix's "https://comix.to/". Required for a
    # cloudflare source — without it the solver falls back to the framework placeholder
    # host and never earns clearance for mangaball.com.
    cloudflare_challenge_url = "https://mangaball.com/"
    # #243: route clearance to the Android-WebView solver, NOT desktop Patchright.
    # MangaBall's 2026-06-15 managed-challenge escalation is the same strict Turnstile
    # that desktop Chromium cannot clear from our Linux fingerprint — both the branch
    # nightly AND the 192.168.0.246 deploy timed out at 60s (``cf_clearance not
    # captured``), exactly like kagane/mangadot (resolved debug
    # ``mangadot-cf-linux-fingerprint``); the real Android WebView clears it. The shared
    # ``get_clearance`` wiring dispatches the csrf-bootstrap GET + every data POST via
    # this engine, so MangaBall is the first android + csrf-bootstrap UNION source. CI
    # has no redroid sidecar, so the nightly disables mangaball (with kagane/mangadot).
    solver_engine = "android"
    # On-demand clearance (debug pooltimeout-recurrence, 2026-06-18): MangaBall's
    # site-wide managed challenge is INTERMITTENT — it was off pre-2026-06-15, switched
    # ON 06-15 (the escalation above), and was OFF again by 06-18 (direct probe: GET /
    # returns 200 + the real csrf-token page, NO ``cf-mitigated`` header). With eager
    # clearance, every request blocked on the android-solver for a cf_clearance it could
    # never mint while no challenge was firing (the WebView found no
    # ``challenges.cloudflare.com`` OOPIF → solve timed out → every download
    # ReadTimeout-failed). This flag makes the cf half LAZY: attach held clearance if
    # the sidecar already has one, but do NOT block on a fresh solve up front — let the
    # request go out and only force a real solve when a response is an actual challenge
    # (``is_cf_challenge`` → the D-35 reconcile in context._request_response). So
    # MangaBall now self-heals whether or not the challenge is live, and the antibot
    # config never needs flipping back and forth. ``antibot``/``solver_engine``/
    # ``session_prep`` stay as-is — only the SOLVE is deferred.
    cloudflare_challenge_optional = True
    # v2 backend has no CSRF token; the API accepts cookie-less, token-less requests
    # (261003-mangaball-api-v2 retired the csrf-bootstrap prep).
    session_prep = None
    supports_search = True
    supports_recent = True

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-listing-row Releases (SRCH-01..07, D-08).

        Two-call live flow (GAP-1 lock): ``search-advanced`` is TITLE-ONLY, so
        ``search`` ALWAYS deep-enumerates the first ``_DEFAULT_TITLE_CANDIDATES``
        title candidates via a per-candidate ``chapter-listing-by-title-id`` JSON POST.
        The MangaDex 15-interactive escalation is DROPPED — ``req.interactive``
        does NOT change the candidate count for MangaBall.

        261003-mangaball-api-v2: the listing is FLAT — one row per (chapter ×
        language × group) — and ONE Release is minted per row via
        :meth:`_to_release` (two ``en`` groups of one chapter → two distinct guids).
        Releases are language-filtered by ``req.languages``, ordered NEWEST-FIRST by
        row ``created_at``, and sliced to ``req.limit`` PER candidate (mirror
        MangaDex's per-candidate feed bound). ZERO networking glue — the POSTs are
        ``ctx.post_json`` / ``ctx.post_json_body`` (SRC-01/02).
        """

        async def _resolve_fn() -> list[dict[str, Any]]:
            original = req.query or ""

            async def _post_search(keyword: str) -> dict[str, Any]:
                # Only which ``keyword`` value is posted ever changes (the retry).
                # 261003-mangaball-api-v2: ``keyword`` is the v2 query key and
                # ``limit`` (max 50) sets the page size.
                return await ctx.post_json(
                    f"{self.base_url}/api/v1/title/search-advanced",
                    data={"keyword": keyword, "limit": 50, **_SEARCH_DEFAULT_FILTERS},
                )

            # 260620-5yq: single sanitize-and-retry on a WAF false-positive 403. The
            # framework mints a distinct ``waf_blocked`` code (context.is_waf_block) for
            # mangaball.com's "Malicious payload" block — a SQL-injection false positive
            # on common tokens like "System". The SOURCE fully ABSORBS that signal
            # (returns releases or []), so it NEVER reaches fanout and fanout stays
            # unchanged. Any OTHER SourceError re-raises (still a real failure →
            # fanout/cooldown). Catch by ``exc.code == "waf_blocked"`` (mirror the
            # engine's ``e.status == 403`` style — never substring-match the message).
            try:
                body = await _post_search(original)
            except SourceError as exc:
                if exc.code != "waf_blocked":
                    raise
                sanitized = _sanitize_waf_query(original)
                if not sanitized or sanitized == original:
                    # Nothing usable to retry with → soft empty (NO 2nd POST). A
                    # waf_blocked absorbed to [] is INVISIBLE to fanout/cooldown, so it
                    # MUST be surfaced here (log + soft warning) or a silent coverage
                    # loss looks identical to a legitimate 0-results. The soft ``warn``
                    # rides the SUCCESS path (the source returns normally), so it shows
                    # in the response ``warnings[]`` WITHOUT feeding the cooldown.
                    if sanitized == original:
                        # 403 fired but our denylist matched nothing → a NEW WAF trigger
                        # word. Loud WARNING so _WAF_TRIGGER_WORDS can be extended.
                        _log.warning(
                            "mangaball WAF-blocked query %r but no known trigger token "
                            "matched — _WAF_TRIGGER_WORDS may need updating",
                            original,
                        )
                    else:
                        # Query was ONLY trigger words (e.g. "System") → nothing left.
                        _log.info(
                            "mangaball WAF-blocked query %r reduced to trigger-words"
                            "-only → no searchable terms remain",
                            original,
                        )
                    ctx.warn(
                        "waf_blocked",
                        f"mangaball WAF blocked search {original!r}; no results",
                    )
                    return []
                try:
                    body = await _post_search(sanitized)
                except SourceError as exc2:
                    if exc2.code != "waf_blocked":
                        raise
                    # Sanitized retry STILL blocked → surface (soft) and give up.
                    _log.warning(
                        "mangaball WAF still blocked sanitized query %r (from %r) — "
                        "no results returned",
                        sanitized,
                        original,
                    )
                    ctx.warn(
                        "waf_blocked",
                        f"mangaball WAF blocked search {original!r} (sanitized retry "
                        f"{sanitized!r} also blocked); no results returned",
                    )
                    return []
                # Recovered: the sanitized retry succeeded and returned results. A
                # query that ends up succeeding after the sanitize-and-retry surfaces
                # NO warning/error to the API consumer (user decision, debug
                # waf-metropolitan-system) — emitting one here read as a hard block even
                # though results came back. The recovery is still logged internally for
                # diagnosability, but never rides the response warnings[]. The two
                # genuinely-failed paths above (sanitized retry still blocked; nothing
                # left to retry) keep their soft warn — those return [] and MUST stay
                # visible (a silent coverage loss looks identical to a real 0-results).
                _log.info(
                    "mangaball WAF-blocked query %r; sanitized retry %r recovered",
                    original,
                    sanitized,
                )
            titles, _pagination = _items_and_pagination(body)

            dict_titles = [t for t in titles if isinstance(t, dict)]
            # Prune obviously-irrelevant candidates BEFORE the per-candidate
            # chapter-listing fan-out (#126): an exact-match query enumerates only
            # the one correct title; ambiguous queries still fan out to the cap (the
            # prune falls back to the historic ``[:_DEFAULT_TITLE_CANDIDATES]``).
            return prune_candidates(
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

        # Layer 1 (D-01): cache the title→pruned-candidate resolution so a repeat
        # chapter search on the same (query, languages) skips the search-advanced
        # POST entirely (genuinely zero upstream calls on a HIT). The key normalizes
        # the query the SAME way the relevance scorer does so case/punctuation
        # variants collapse onto one entry (T-09-01: never keys on type/chapter).
        candidates: list[dict[str, Any]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(req.query or ""), req.languages or []),
            _resolve_fn,
        )
        # 260605-e9a deliverable 5: how many title candidates we deep-enumerate (HIT
        # too — set AFTER the resolve).
        ctx.candidates_enumerated = len(candidates)
        wanted_langs = set(req.languages) if req.languages else None
        per_candidate_limit = req.limit or 50

        # Bound the per-candidate chapter-listing fan-out (one Semaphore shared across
        # the dispatch; constructed HERE so it binds to the running loop, never at
        # import time).
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(title_id: str, manga_title: str) -> list[Release]:
            # Layer 2 (CACHE-02/03): cache the UNFILTERED per-candidate listing rows
            # per (title_id, languages). The ``async with sem:`` + the chapter-listing
            # POST + the ``_items_and_pagination`` extraction live INSIDE ``_enum_fn``
            # so a HIT acquires NEITHER the fan-out semaphore NOR a rate-limit token
            # (the limiter lives inside ``post_json_body``, one level below the cache
            # check). ``exhausted=True``: the v2 listing is the COMPLETE flat feed (no
            # pagination), so ``covers_floor`` is always True (no refetch).
            # 261003-mangaball-api-v2: the listing now takes a JSON body (a form POST
            # returns 400).
            async def _enum_fn() -> Enumeration:
                async with sem:
                    listing = await ctx.post_json_body(
                        f"{self.base_url}/api/v1/chapter/chapter-listing-by-title-id",
                        body={"title_id": title_id},
                    )
                rows, _ = _items_and_pagination(listing)
                return Enumeration(
                    items=rows,
                    chapter_numbers=tuple(
                        d
                        for r in rows
                        if isinstance(r, dict)
                        and (d := self._row_number(r)) is not None
                    ),
                    exhausted=True,
                    requested_limit=per_candidate_limit,
                )

            # IN-02: the listing is the whole title's feed and does NOT
            # depend on ``languages`` (the language filter is applied post-cache), so
            # key on ``[]`` — different-language requests for one title share the
            # cached listing instead of re-fetching byte-identical data per language.
            enum = await ctx.cached_enumerate(
                ctx.cached_enumerate_key(title_id, []), _enum_fn
            )
            # _chapters_to_releases keeps the chapter_matches filter +
            # newest-first sort + [:limit] + GAP-2 mint-after-slice all stay; it
            # simply consumes ``enum.items`` (the cached raw rows) instead of the
            # raw fetch result.
            return self._chapters_to_releases(
                enum.items,
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
            title_id = title.get("_id") or title.get("id")
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
        listing_rows: list[Any],
        title_id: str,
        manga_title: str,
        wanted_langs: set[str] | None,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one candidate's flat v2 listing rows → per-row Releases.

        261003-mangaball-api-v2: each row is one (chapter × language × group) — the
        download unit. Rows are ``chapter_matches``-gated, language-filtered (on the
        mapped code), sorted NEWEST-FIRST by ``created_at`` (parsed via
        :func:`_parse_ts`), and sliced to ``limit``. Multi-group-same-language is
        preserved: distinct row ids → distinct guids.

        GAP-2 (live): mint handles ONLY for the post-slice survivors. A long-running
        title (One Piece ≈ 1382 chapters × many languages/groups) would otherwise mint
        tens of thousands of handles per candidate — blowing past the ``HandleStore``
        ``maxsize`` (default 200_000, GATEWAY_HANDLE_MAXSIZE) so the TTLCache EVICTS
        the very handles attached to the releases we return. Collect sort keys first,
        slice to ``limit``, THEN mint.
        """
        rows: list[tuple[datetime, Decimal | None, dict[str, Any]]] = []
        for row in listing_rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue  # no download unit → _to_release would drop it anyway
            number = self._row_number(row)
            # 260606-2ff: drop a non-matching chapter BEFORE it enters `rows` →
            # before the newest-first sort / [:limit] slice / mint (preserves the
            # GAP-2 mint-after-slice ordering). Gate-off = pass-through.
            if not self.chapter_matches(req, number):
                continue
            if wanted_langs is not None and _row_language(row) not in wanted_langs:
                continue
            rows.append((_parse_ts(str(row.get("created_at") or "")), number, row))
        rows.sort(key=lambda r: r[0], reverse=True)  # newest-first
        releases: list[Release] = []
        for _ts, number, row in rows[:limit]:  # mint AFTER slice (GAP-2)
            rel = self._to_release(title_id, manga_title, number, row, ctx)
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
        """Newest-first recent chapters → DIRECT releases (RCNT-01/02).

        POSTs ``/api/v1/title/search`` with ``search_type=getRecentlyUpdatedChapter``.
        261003-mangaball-api-v2: each title carries ``recent_chapters`` rows of the
        SAME shape as the chapter listing (no HTML to parse), so ONE DIRECT Release is
        minted per row via :meth:`_to_release` — ``ResolutionRecord.chapter_id`` is
        the row id. ``publishDate`` comes from the row ``created_at``, falling back to
        the title ``updated_at``, then now. ``languages`` filters on the mapped code.
        The route applies the authoritative newest-first sort + ``since`` cut
        (recent.py). Zero networking glue — ``ctx.post_json`` owns the transport.
        """
        form: dict[str, Any] = {"search_type": "getRecentlyUpdatedChapter", "page": 1}
        body = await ctx.post_json(f"{self.base_url}/api/v1/title/search", data=form)
        titles, _pagination = _items_and_pagination(body)

        wanted_langs = set(languages) if languages else None
        rows: list[tuple[datetime, str, str, dict[str, Any]]] = []
        for title in titles:
            if not isinstance(title, dict):
                continue
            title_id = title.get("_id") or title.get("id")
            if not title_id:
                continue
            manga_title = _strip_html(title.get("name")) or "Unknown"
            for row in title.get("recent_chapters") or []:
                if not isinstance(row, dict):
                    continue
                if wanted_langs is not None and _row_language(row) not in wanted_langs:
                    continue
                if not row.get("created_at"):
                    # Fallback publishDate: the title's updated_at (then now, via
                    # _normalize_publish_date).
                    row = {**row, "created_at": title.get("updated_at")}
                ts = _parse_ts(str(row.get("created_at") or ""))
                rows.append((ts, str(title_id), manga_title, row))
            # WR-03: do NOT break at ``limit`` over raw feed order. The route
            # (recent.py) applies the authoritative newest-first sort + ``since``
            # cut, then re-trims to the merged limit — an in-loop break in FEED
            # order would hide a genuinely-newer row sitting past position
            # ``limit`` from that sort. Consume the whole page; ``since`` is
            # intentionally ignored source-side (IN-01).
        rows.sort(key=lambda r: r[0], reverse=True)  # newest-first
        releases: list[Release] = []
        for _ts, title_id, manga_title, row in rows:
            number = self._row_number(row)
            rel = self._to_release(title_id, manga_title, number, row, ctx)
            if rel is not None:
                releases.append(rel)
        return releases

    # ───────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered page-image URLs, INTERNALLY (PKG-01/R6).

        Both ``search`` and ``recent`` mint the bare listing-row ``id`` as the
        ``chapter_id``; :meth:`_manifest_for_translation` resolves it via the v2
        ``chapter-detail`` JSON endpoint (261003-mangaball-api-v2), SSRF-allowlists
        every page URL, and applies the pages-count guard.

        #83/IN-03: the guard uses ``ctx.expected_pages`` forwarded by the engine. The
        v2 API exposes no page count, so new records carry ``None`` and the guard
        degrades to a no-op; it still runs whenever a count is known.
        """
        return await self._manifest_for_translation(chapter_id, ctx.expected_pages, ctx)

    async def _manifest_for_translation(
        self, translation_id: str, pages: int | None, ctx: SourceContext
    ) -> list[str]:
        """chapter-detail JSON → pages + SSRF allowlist + pages guard (PKG-01).

        GETs ``/api/v1/chapter-detail?chapter_id=<id>`` (261003-mangaball-api-v2) and
        reads ``data.chapter.pages`` — absolute CDN URLs in reading order. The CDN
        host is taken verbatim, NEVER reconstructed (RECON §4 / CLAUDE.md SSRF) — it
        varies per content. Every URL is SSRF-allowlisted
        (:func:`_is_allowed_image_url`) before return; a non-allowlisted URL raises
        ``SourceError`` (no blind fetch, T-07-07). The count is guarded against
        ``pages`` when known (integrity guard, mirror ``mangadex.fetch_manifest``).
        """
        body = await ctx.get_json(
            f"{self.base_url}/api/v1/chapter-detail", chapter_id=translation_id
        )
        data = body.get("data")
        chapter = data.get("chapter") if isinstance(data, dict) else None
        raw_pages = chapter.get("pages") if isinstance(chapter, dict) else None
        urls = [
            u.strip()
            for u in (raw_pages if isinstance(raw_pages, list) else [])
            if isinstance(u, str) and u.strip()
        ]
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

        261003-mangaball-api-v2: the CDN hotlink-blocks a bare GET (403), so the
        reader's ``Referer: https://mangaball.com/`` is required —
        ``get_bytes_plain_with_headers`` carries it (comix precedent). No decrypt;
        the response headers are discarded. Bounded by the per-job semaphore, NOT the
        per-source API limiter.
        """
        data, _headers = await ctx.get_bytes_plain_with_headers(
            url, extra_headers=_IMAGE_FETCH_HEADERS
        )
        return data

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        title_id: str,
        manga_title: str,
        chapter_number: Decimal | None,
        row: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single v2 listing/recent row (D-08).

        261003-mangaball-api-v2: the row ``id`` is the download unit
        (``chapter-detail?chapter_id=``); the v2 API exposes no page count anywhere,
        so ``page_count`` is ``None`` (the manifest count guard then no-ops).
        """
        row_id = row.get("id")
        if not row_id:
            return None
        row_id = str(row_id)

        language = _row_language(row)
        # REL-03: display-only votes from row views (recent rows lack it → None).
        votes = self._parse_int(row.get("views"))
        publish_date = self._normalize_publish_date(row.get("created_at"))
        group = row.get("group")
        group_name = _strip_html(row.get("group_name")) or (
            _strip_html(group.get("name")) if isinstance(group, dict) else None
        )
        volume = self._parse_int(row.get("volume")) or None  # 0 means "no volume"

        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        title = self._build_title(
            manga_title, ch_str, language=language, group=group_name
        )
        # D-08: language + row id needed — one chapter number maps to N rows (one
        # per language/group).
        guid = f"mangaball:{title_id}:ch-{ch_str}:{language}:{row_id}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=row_id,  # the chapter-detail/download unit
                language=language,
                title=title,
                manga_title=manga_title,
                chapter_number=chapter_number,
                volume=volume,
                scanlation_group=group_name,
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
            volume=volume,
            language=language,
            scanlation_group=group_name,
            page_count=None,
            votes=votes,
            # Keys kept from v1 (the row id is still the translation-level download
            # unit) — renaming would churn consumers.
            ids={
                "mangaballTitleId": title_id,
                "mangaballTranslationId": row_id,
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
        """Normalize a row ``created_at`` → RFC3339 ``date-time`` (REL-03, GAP-2).

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

    def _row_number(self, row: dict[str, Any]) -> Decimal | None:
        """A v2 row's chapter number (``chapter_number``, else ``number``)."""
        return self._parse_decimal(row.get("chapter_number") or row.get("number"))

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
