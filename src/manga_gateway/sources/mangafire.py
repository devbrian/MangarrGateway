"""MangaFire source — a Comix-class browser-DOM × atsumaru-class fan-out hybrid.

MangaFire (``https://mangafire.to``) is a PHP/jQuery server-rendered HTML + AJAX
site whose download + keyword-search AJAX endpoints are gated by a per-request
``vrf`` token that is **captured from the warm browser, never reverse-engineered**
(issue #192, live-verified 2026-06-09 + cross-checked against the Keiyoushi/Mihon
``src/all/mangafire`` extension). It adds **zero networking glue** — every outbound
call is ``ctx.get_json`` / ``ctx.get_bytes`` / ``solver.fetch_via_browser*``
(SRC-01/SRC-02).

Each of the four ``Source`` hooks copies a DIFFERENT existing analog:

* **chapter list** — ``GET /ajax/manga/{slugId}/chapter/{lang}`` (JSON-with-HTML-in-
  ``result``, NO vrf) → ``ctx.get_json`` + lxml-in-``to_thread`` (D-06).
* **recent** — ``GET /filter?sort=recently_updated`` (HTML, NO vrf) → ``ctx.get_bytes``
  + lxml + title→chapter fan-out + DIRECT newest-chapter mint (D-07).
* **fetch_manifest** — navigate the read page; the reader's own JS mints the per-
  request ``vrf`` and auto-fires ``/ajax/read/chapter/{itemId}?vrf=…`` →
  ``solver.fetch_via_browser`` + a ``performance``-entry capture extract; each image
  URL is SSRF-allowlisted; the ``#scr_{offset}`` scramble offset rides as a URL
  fragment (D-08/D-09/D-10).
* **fetch_image** — ``ctx.get_bytes`` of the fragment-stripped URL + a geometric
  piece-shuffle descramble when ``offset>0`` (port of Keiyoushi ``ImageInterceptor.kt``
  in Pillow, offloaded via ``to_thread``) (D-11/D-12).
* **search** — type the keyword via the ``fetch_via_browser_typed`` real-keyboard
  primitive (Plan 12-01), capture the search ``vrf`` from ``performance``, then
  ``ctx.get_bytes(/filter?keyword=…&vrf=…)``, parse cards, fan out, GAP-2 mint-after-
  slice (D-13).

This module (Task 1) holds ONLY the module-level constants + pure functions; the
concrete ``MangaFireSource`` class lands in Task 2.

SSRF (D-10): the page-image CDN host VARIES per content (``o48.mfcdn1.xyz``, …) and
is NEVER pinned. ``_is_allowed_image_url`` enforces only the stable invariants
(``https`` + public-host shape + ``/mf/`` namespace + image extension + no traversal),
stripping any URL fragment BEFORE the path is matched so a malicious ``#`` fragment
cannot smuggle a path past the regex.
"""

from __future__ import annotations

import asyncio
import io
import json
import posixpath
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urldefrag, urlencode, urljoin, urlparse

import lxml.html
from cachetools import LRUCache
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

# ── SSRF allowlist (host-agnostic; copy the mangaball/weebcentral shape, D-10) ──
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
# host. (The metadata-link detail GET in fetch_external_links targets the MAIN site host
# via base_url and does NOT route through this image allowlist, so pinning to the CDN
# family does not affect it.)
_MANGAFIRE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.mfcdn\d+\.xyz$", re.IGNORECASE)
_MANGAFIRE_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".localhost")

# ── adaptive CDN zone-retry (debug mangafire-stale-manifest-reresolve-budget) ───
# MangaFire serves a chapter's pages from ONE of a small rotating set of CDN zones,
# each host shaped ``{prefix}.mfcdn{N}.xyz`` (prefix ∈ nw8/l1n/k99/m3z/o48; N ∈ the
# set below). Cloudflare's WAF on SOME zones hard-blocks (403) the gateway's egress
# IP while OTHERS allow it; the block is egress-IP-dependent, so we do NOT pin a
# single "good" zone — on a per-page 403 we retry the IDENTICAL signed path with the
# host's ``mfcdnN`` rewritten to each OTHER known zone until one returns bytes (the
# live cross-test proved the exact signed path returns 200 on an un-blocked zone),
# then remember the winning zone for the rest of the job. Extend this tuple if
# MangaFire adds a zone — no other code change needed.
_MANGAFIRE_CDN_ZONES: tuple[int, ...] = (1, 2, 3)
# Matches the ``mfcdnN`` label inside a MangaFire image host (``o48.mfcdn1.xyz``) so
# the zone digit can be rewritten while leaving the subdomain prefix + ``.xyz`` TLD
# (and the whole path/signature) byte-for-byte intact.
_MANGAFIRE_ZONE_RE = re.compile(r"\.mfcdn(\d+)\.", re.IGNORECASE)
# Per-job sidecar attr stashed on the SourceContext holding the CDN zone that last
# answered 200 for THIS job, so subsequent pages try it FIRST and never re-probe the
# blocked zone every page. The context is per-job (engine._build_context), so this
# never cross-contaminates concurrent jobs — unlike source-instance state (the source
# object is a shared registry singleton).
_PREFERRED_ZONE_ATTR = "_mangafire_preferred_cdn_zone"

# ── geometric descramble constants (D-12, Keiyoushi ImageInterceptor.kt) ────────
PIECE_SIZE = 200
MIN_SPLIT_COUNT = 5

# ── per-query vrf LRU cache size (D-13). The cache instance itself lives on the
# MangaFireSource instance (Task 2) so it never leaks across instances/tests; this
# is the size knob (Claude's discretion per the plan artifacts list). ─────────────
_VRF_CACHE_MAXSIZE = 20

# ── control-flow constants (mirror atsumaru/weebcentral) ───────────────────────
# search() deep-enumerates at most this many title candidates (GAP-1 lock).
_DEFAULT_TITLE_CANDIDATES = 5
# Bounds the per-candidate chapter-list fan-out (search + recent). Both HTML/JSON
# paths use the unlimited get_bytes/get_json byte/json primitives, so this semaphore
# (not the rate limiter) is the real concurrency bound — probe-tune in Plan 03.
_CHAPTERS_FANOUT_CONCURRENCY = 6
# recent() bounds the per-title fan-out so a poll never balloons into dozens of GETs.
_RECENT_TITLE_CAP = 20
# Default chapter language when the request carries no language filter.
_DEFAULT_LANGUAGE = "en"
# The keyword <input> the typed primitive drives (D-13).
_SEARCH_INPUT_SELECTOR = ".search-inner input[name=keyword]"

# ── fetch_manifest contention retry (debug mangafire-manifest-contention) ──────
# Under concurrent browser navs the in-page manifest capture intermittently yields
# an EMPTY/malformed list (reader AJAX never fires in the poll window, or the in-page
# re-fetch throws). It self-heals on retry (live: isolated 8/8 OK). fetch_manifest
# therefore re-navigates & re-extracts up to this many EXTRA times on an empty/
# malformed capture, with the backoff below, before raising. An integrity mismatch on
# a NON-empty capture is a real data problem and is NOT retried.
_MANIFEST_EMPTY_RETRIES = 2
# Backoff (seconds) before each extra attempt; index 0 = before the 1st retry, etc.
_MANIFEST_RETRY_BACKOFF: tuple[float, ...] = (1.0, 2.0)

# ── browser `extract` bodies (bare `return`; the framework wraps `async () => {…}`,
# mirroring comix `_CHAPTER_PAGES_EXTRACT_JS`). ─────────────────────────────────

# fetch_manifest (D-08): poll the browser's own `performance` resource entries for
# the reader's auto-fired `/ajax/read/chapter/` AJAX, re-`fetch()` it in-page with
# the XHR header, and return [url, offset] per image (index 0 = URL, index 2 =
# offset; offset==0 ⇒ not scrambled). ≤60×500ms.
#
# Contention hardening (debug mangafire-manifest-contention, 2026-06-19): under
# concurrent reader navs the in-page re-`fetch()` of the AJAX URL can transiently
# THROW or come back empty (observed images=-2). The re-fetch is therefore wrapped
# in its own small bounded retry, and on a thrown/empty re-fetch we CONTINUE the
# outer poll loop (keep waiting / retry next tick) instead of aborting the whole
# extract — the ~30s overall budget (≤60×500ms) is preserved. Contract unchanged:
# still returns [[url, offset], …] or [].
_IMAGE_LIST_EXTRACT_JS = """
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 60; i++) {
    const hit = performance
      .getEntriesByType('resource')
      .map((e) => e.name)
      .find((u) => u.includes('/ajax/read/chapter/'));
    if (hit) {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const r = await fetch(hit, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
          });
          const j = await r.json();
          const imgs = (j && j.result && j.result.images) || [];
          if (imgs.length) return imgs.map((it) => [it[0], it[2] || 0]);
        } catch (e) {
          // transient in-page re-fetch failure under contention — keep trying.
        }
        await sleep(200);
      }
      // re-fetch threw or stayed empty: fall through and CONTINUE polling.
    }
    await sleep(500);
  }
  return [];
"""

# search (D-13): after the keyword is TYPED (real keyboard), the site's debounced
# handler fires `/ajax/manga/search?…&vrf=…`; find it in `performance` entries and
# return the captured vrf. The SAME vrf works on `/filter` (Keiyoushi proves it).
_SEARCH_VRF_EXTRACT_JS = """
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 60; i++) {
    const hit = performance
      .getEntriesByType('resource')
      .map((e) => e.name)
      .find((u) => u.includes('/ajax/manga/search') && u.includes('vrf='));
    if (hit) {
      const m = hit.match(/[?&]vrf=([^&]+)/);
      if (m) return decodeURIComponent(m[1]);
    }
    await sleep(500);
  }
  return null;
"""


def _ceil_div(a: int, b: int) -> int:
    """Integer ceiling division ``ceil(a/b)`` (D-12 ``ceilDiv``)."""
    return (a + b - 1) // b


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` is a well-formed MangaFire ``/mf/`` page image (SSRF, D-10).

    Belt-and-suspenders defence on every browser-captured manifest URL before the
    framework fetches it (T-12-03/T-12-04). The CDN host VARIES per content so it is
    NEVER pinned — the trust comes from ``https`` + a public-host shape + the ``/mf/``
    path namespace + an image extension + no traversal. Any URL fragment (the
    ``#scr_{offset}`` scramble marker, or a hostile ``#`` smuggle) is stripped BEFORE
    the path is parsed, so a fragment can never sneak a bad path past the regex. We
    reject internal/metadata host namespaces and validate the
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
    entire path + query + signature stay byte-for-byte identical (the live cross-test
    proved the exact signed path returns 200 on an un-blocked zone). A URL with no
    ``mfcdn`` label is returned unchanged. Operates on the netloc only so a path segment
    that happened to contain ``mfcdnN`` could never be rewritten.
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


def _descramble_image(content: bytes, offset: int) -> bytes:
    """Un-shuffle a MangaFire scrambled page image (D-12; PKG-02).

    Port of Keiyoushi ``ImageInterceptor.kt``. For ``offset<=0`` the page is NOT
    scrambled → the bytes pass through byte-for-byte. For ``offset>0`` the image is a
    ``PIECE_SIZE``-px piece grid whose interior pieces are cyclically shifted; the
    last row/column stay in place. We decode with Pillow, re-assemble the grid, and
    re-encode in the SAME format — NEVER recompressing beyond that re-encode (PKG-02).
    A non-image / truncated / hostile body degrades to a passthrough (the packaging
    ``is_valid_image`` guard rejects it downstream — T-12-07), never raising.
    """
    if offset <= 0:
        return content
    try:
        with Image.open(io.BytesIO(content)) as src_img:
            fmt = src_img.format or "PNG"
            # Capture the source JPEG quantization tables BEFORE the context closes
            # — the reassembled image is built from scratch and carries none, so
            # ``quality="keep"`` can't preserve fidelity (PKG-02, issue #218). Re-
            # encoding with the SOURCE qtables keeps the page visually lossless.
            qtables = getattr(src_img, "quantization", None)
            img = src_img.convert(src_img.mode)
        width, height = img.size
        piece_w = min(PIECE_SIZE, _ceil_div(width, MIN_SPLIT_COUNT))
        piece_h = min(PIECE_SIZE, _ceil_div(height, MIN_SPLIT_COUNT))
        x_max = _ceil_div(width, piece_w) - 1
        y_max = _ceil_div(height, piece_h) - 1
        dst = Image.new(img.mode, (width, height))
        for x in range(x_max + 1):
            for y in range(y_max + 1):
                x_dst = piece_w * x
                y_dst = piece_h * y
                w = min(piece_w, width - x_dst)
                h = min(piece_h, height - y_dst)
                # Last row/col stay in place; interior pieces shift by `offset`.
                x_src = piece_w * (x if x == x_max else (x_max - x + offset) % x_max)
                y_src = piece_h * (y if y == y_max else (y_max - y + offset) % y_max)
                region = img.crop((x_src, y_src, x_src + w, y_src + h))
                dst.paste(region, (x_dst, y_dst))
        buf = io.BytesIO()
        # Never recompress at Pillow's defaults (JPEG q75 / WebP ~q80) — that
        # degrades every offset>0 page (PKG-02, issue #218). JPEG re-encodes with
        # the captured source qtables (+ no chroma subsampling) for a visually
        # lossless result; WebP has no qtables so the best we can do is q95/method6.
        # PNG (and any other format) stays lossless under a plain re-encode.
        save_kwargs: dict[str, Any] = {}
        if fmt == "JPEG":
            if qtables:
                save_kwargs = {"qtables": qtables, "subsampling": 0}
            else:
                save_kwargs = {"quality": 95, "subsampling": 0}
        elif fmt == "WEBP":
            save_kwargs = {"quality": 95, "method": 6}
        dst.save(buf, format=fmt, **save_kwargs)
        return buf.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return content


def _parse_chapter_list(html: str) -> list[dict[str, Any]]:
    """Parse the chapter-list ``result`` HTML → ordered chapter rows (D-06).

    One ``<li data-number="…">`` per chapter (newest-first document order). Each row
    carries the read ``a@href`` (the resolve unit), the ``data-number`` chapter
    number, and the 2nd ``<span>`` date (``MMM dd, yyyy``). A row with no read href is
    skipped; a malformed fragment returns ``[]`` (never raises).
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for li in doc.xpath("//li[@data-number]"):
        hrefs = li.xpath(".//a/@href")
        if not hrefs:
            continue
        spans = [s.strip() for s in li.xpath(".//a//span/text()") if s.strip()]
        out.append(
            {
                "href": str(hrefs[0]).strip(),
                "number": str(li.get("data-number") or "").strip(),
                "date": spans[1] if len(spans) >= 2 else None,
            }
        )
    return out


def _parse_sync_data(html: bytes | str) -> dict[str, Any]:
    """Parse the detail page's ``<script id="syncData">`` JSON → raw tracker dict.

    The detail page embeds a single ``<script id="syncData">{…}</script>`` whose body
    is a JSON object carrying ``anilist_id`` + ``mal_id`` (plus internal keys the
    normalizer drops). Per RESEARCH Pitfall 6 the WHOLE script body is JSON-parsed —
    NOT regexed per-id — so a shape change surfaces as a parse miss, not silent
    mis-extraction.

    On ANY miss (no script, empty body, malformed JSON, non-object) this RAISES — the
    framework best-effort wrapper (``resolve_external_links``) owns the swallow-to-
    ``None`` so the chapter releases still return (R6). lxml + ``json.loads`` are
    blocking, so this runs under ``asyncio.to_thread`` (ruff ASYNC, no loop blocking).
    """
    doc = lxml.html.fromstring(html)
    scripts = doc.xpath("//script[@id='syncData']")
    if not scripts:
        raise ValueError("no syncData script on detail page")
    data = json.loads(scripts[0].text_content())
    if not isinstance(data, dict):
        raise ValueError("syncData is not a JSON object")
    return data


def _parse_cards(html: bytes | str) -> list[dict[str, Any]]:
    """Parse ``.original.card-lg .unit .inner`` cards → ``[{href,title,thumbnail}]``.

    Shared by both ``recent`` (D-07) and ``search`` (D-13) — both feeds render the
    identical card markup. Each ``.inner`` exposes a ``.info > a`` whose ``href`` is
    ``/manga/{slug}.{id}`` and whose text is the title, plus a cover ``<img src>``.
    A card with no ``/manga/`` info link is skipped; a malformed fragment returns
    ``[]`` (never raises). Deduped by href in document (relevance) order.
    """
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    inners = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' original ')"
        " and contains(concat(' ', normalize-space(@class), ' '), ' card-lg ')]"
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' inner ')]"
    )
    for inner in inners:
        info_links = inner.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' info ')]"
            "//a[contains(@href, '/manga/')]"
        )
        if not info_links:
            continue
        anchor = info_links[0]
        href = (anchor.get("href") or "").strip()
        title = (anchor.text_content() or "").strip()
        if not href or not title or href in seen:
            continue
        seen.add(href)
        imgs = inner.xpath(".//img/@src")
        out.append(
            {
                "href": href,
                "title": title,
                "thumbnail": str(imgs[0]).strip() if imgs else None,
            }
        )
    return out


def _manga_token_and_slug_id(href: str) -> tuple[str, str]:
    """Split a ``/manga/{slug}.{id}`` href → ``(manga_token, slug_id)`` (D-06).

    ``manga_token`` is the full ``{slug}.{id}`` (the stable per-manga identifier used
    in guids/ids); ``slug_id`` is the trailing token after the last ``.`` (the
    chapter-list endpoint key). A href with no ``.`` yields an empty ``slug_id`` so the
    caller skips it.
    """
    token = href.rstrip("/").rsplit("/", 1)[-1]
    slug_id = token.rsplit(".", 1)[-1] if "." in token else ""
    return token, slug_id


def _iso_from_mangafire_date(raw: Any) -> str | None:
    """Normalize a MangaFire ``MMM dd, yyyy`` date → RFC3339 ``date-time`` (REL-01).

    The chapter-list rows render the date as ``May 21, 2026``. Returns the aware-UTC
    ISO string, or ``None`` for a missing/unparseable value so the caller can fall
    back to ``now(UTC)`` (the contract's ``publishDate`` is required RFC3339).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.strptime(raw.strip(), "%b %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed.isoformat()


class MangaFireSource(Source):
    """MangaFire (mangafire.to) — cloudflare browser-DOM × title→chapter fan-out.

    A composite of four existing analogs (one per hook): atsumaru's fan-out/mint
    control flow, comix's ``fetch_via_browser`` + ``_solver_from_ctx`` browser-DOM
    manifest, weebcentral/mangaball's lxml-via-``to_thread`` parsing + host-agnostic
    SSRF allowlist, plus the geometric image descramble (D-12). Zero networking glue —
    every outbound call is ``ctx.get_json`` / ``ctx.get_bytes`` / ``solver.fetch_via_
    browser*`` (SRC-01/SRC-02). See the module docstring for the per-hook map.
    """

    key = "mangafire"
    name = "MangaFire"
    base_url = "https://mangafire.to"
    # Title-search only — MangaFire exposes no external metadata-id namespace (SRCH-07).
    id_types: list[str] = []
    # Recon-observed language set (the /filter language[] options). Refine in Plan 03.
    languages = ["en", "fr", "es", "es-la", "pt-br", "ja"]
    # Probe-measured (2026-06-10, scripts/probe_rate_limits.py, residential proxies,
    # fresh-proxy-per-category, 46/50 healthy) — D-14, replacing the Plan 02
    # placeholders. Two sweeps found a REAL per-endpoint difference (unlike atsumaru's
    # uniform floor):
    #   * text/AJAX host (mangafire.to — /filter + /ajax/manga/{slug}/chapter): a real
    #     HTTP-429 CEILING. Fully clean at 120/min (0/120 throttled); 429 onset at
    #     300/min (103/300 blocked), worsening at 600 (370) and 960 (718). Parallelism
    #     stayed clean to concurrency 32 at 120/min. 60 is the harness's conservative
    #     ~50%-safety-fraction of the 120/min clean ceiling — a MEASURED ceiling, not a
    #     floor (contrast atsumaru's 480 FLOOR). It governs the LIMITED get_json
    #     chapter-list path (the high-volume fan-out path) and leaves host headroom for
    #     the few UNLIMITED get_bytes /filter calls that share the same 429 ceiling.
    #   * image CDN (separate host): NO limit — clean to 960/min at concurrency 8 (a
    #     FLOOR). The get_bytes image path is exempt from this limiter (limited=False);
    #     its throughput is bounded by max_concurrent_jobs, not rate_limit_per_minute.
    #   * browser manifest (fetch_via_browser): UNMEASURABLE by the httpx probe (a
    #     browser-nav path — skipped, "manifest not captured"); the Plan 04 live nightly
    #     confirms it end-to-end.
    rate_limit_per_minute = 60
    # D-30 per-source override — bounds concurrent download JOBS only (recent/search/
    # filter HTML use the separate per-source _CHAPTERS_FANOUT_CONCURRENCY semaphore,
    # not this knob). Each job's binding cost is its one browser reader-nav manifest
    # extract (comix-class). Serialized to 1 (was 3) by the
    # mangafire-manifest-contention investigation (2026-06-19): concurrent mangafire
    # reader navs starve each other's in-page `/ajax/read/chapter/` capture,
    # intermittently yielding an empty manifest → "malformed mangafire chapter
    # manifest" (live repro: 8/8 isolated OK, 10/10 FAIL at concurrency 5). 1 removes
    # mangafire-vs-mangafire manifest contention WITHOUT touching the shared solver
    # `cloudflare_fetch_concurrency`. Tradeoff: mangafire download throughput is one
    # chapter at a time; search/recent are unaffected, and the bounded retry + hardened
    # extract above cover residual cross-source contention.
    max_concurrent_jobs = 1
    # D-05: interior pages answer cold over httpx, but declare cloudflare anyway so the
    # framework keeps a warm browser + clearance for the manifest path and degrades
    # gracefully on a datacenter host that trips a managed challenge.
    antibot = "cloudflare"
    cloudflare_challenge_url = "https://mangafire.to/"
    # D-04: the image descramble is geometric (done in fetch_image), NOT the response-
    # byte decrypt seam; no session bootstrap.
    decrypt_scheme = None
    session_prep = None
    supports_search = True
    supports_recent = True
    # Opt OUT of the engine's 403→stale-baseUrl re-resolve recovery (debug
    # ``mangafire-stale-manifest-reresolve-budget``). A MangaFire image 403 is a
    # Cloudflare WAF deny of the gateway's egress IP on a particular CDN zone
    # (``mfcdnN``), NOT a stale/expired baseUrl: re-resolving re-navigates the reader
    # and lands the SAME pages on the SAME blocked zone, so the engine's re-resolve is
    # provably useless and only burns 2 wasted browser navs before failing with the
    # misleading "stale manifest re-resolve budget exceeded". This source instead
    # self-heals INSIDE ``fetch_image`` by retrying the identical signed path across
    # the OTHER CDN zones; a 403 only escapes ``fetch_image`` when ALL zones are
    # blocked, where re-resolve cannot help anyway — so failing fast is correct.
    reresolve_manifest_on_403 = False

    def __init__(self) -> None:
        super().__init__()
        # Per-query vrf cache (D-13): a repeat search on the same keyword reuses the
        # captured vrf and skips the typed browser nav. Instance-scoped so it never
        # leaks across instances/tests.
        self._vrf_cache: LRUCache[str, str] = LRUCache(maxsize=_VRF_CACHE_MAXSIZE)

    # ─────────────────────────────── search ──────────────────────────────────

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        """Keyword search → per-chapter Releases (SRCH-01..07, D-13).

        Three-step live flow: (1) type the keyword via the ``fetch_via_browser_typed``
        real-keyboard primitive (Plan 12-01) and capture the search ``vrf`` from
        ``performance`` (cached per query, LRU); (2) ``ctx.get_bytes`` the vrf'd
        ``/filter`` HTML and prune the title-only cards to the candidate cap;
        (3) deep-enumerate each candidate's chapter list under a bounded semaphore,
        filter by :meth:`chapter_matches`, slice to ``req.limit``, and ONLY THEN mint
        (GAP-2 mint-after-slice — a long title would otherwise evict the returned
        releases' own handles). Zero networking glue (SRC-01/02).
        """
        query = req.query or ""
        if not query:
            return []
        lang = self._filter_language(req.languages)

        async def _resolve_fn() -> list[dict[str, Any]]:
            vrf = self._vrf_cache.get(query)
            if vrf is None:
                solver = self._solver_from_ctx(ctx, need_typed=True)
                captured = await solver.fetch_via_browser_typed(
                    f"{self.base_url}/home",
                    type_selector=_SEARCH_INPUT_SELECTOR,
                    type_text=query,
                    extract=_SEARCH_VRF_EXTRACT_JS,
                    wait_for=None,
                    timeout=60.0,
                )
                if not isinstance(captured, str) or not captured:
                    raise SourceError(
                        "source_unavailable", "mangafire search vrf capture failed"
                    )
                vrf = captured
                self._vrf_cache[query] = vrf
            params = urlencode(
                {"keyword": query, "language[]": lang, "page": 1, "vrf": vrf}
            )
            html = await ctx.get_bytes(f"{self.base_url}/filter?{params}")
            cards = await asyncio.to_thread(_parse_cards, html)
            return prune_candidates(
                cards,
                query,
                keys=lambda d: [d.get("title")],
                cap=_DEFAULT_TITLE_CANDIDATES,
            )

        # Layer 1 (D-01): cache the title→pruned-candidate resolution so a repeat
        # chapter search on the same (query, languages) skips the typed nav + /filter.
        candidates: list[dict[str, Any]] = await ctx.cached_resolve(
            ctx.cached_resolve_key(_normalize(query), req.languages or []),
            _resolve_fn,
        )
        ctx.candidates_enumerated = len(candidates)
        per_candidate_limit = req.limit or 50
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _fetch_candidate(
            manga_token: str, slug_id: str, manga_title: str
        ) -> list[Release]:
            # Layer 2 (CACHE-02/03): cache the UNFILTERED per-candidate chapter list.
            # The semaphore + the GET live INSIDE ``_enum_fn`` so a cache HIT acquires
            # no fan-out slot.
            async def _enum_fn() -> Enumeration:
                async with sem:
                    rows = await self._chapter_list(slug_id, lang, ctx)
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

            enum = await ctx.cached_enumerate(
                # ``_chapter_list`` fetches the ``/{lang}``-scoped chapter feed, so the
                # Layer-2 key MUST be namespaced by ``lang`` — keying on ``slug_id``
                # alone lets one language's warmed rows leak into a later search for
                # the same manga in another language (mismatched releases/handles).
                ctx.cached_enumerate_key(slug_id, [lang]),
                _enum_fn,
            )
            releases = self._chapters_to_releases(
                enum.items,
                manga_token,
                manga_title,
                lang,
                per_candidate_limit,
                ctx,
                req,
            )

            # Phase-13 (R4/R6/D-02): resolve the series' tracker links ONCE via the
            # framework wrapper (resolve-once cache + 5s timeout + swallow-all, D-03),
            # keyed on ``manga_token`` (the stable ``{slug}.{id}`` id that also builds
            # the detail URL), then stamp the single resolved object onto every release
            # of the series (identical-object, R4). The detail URL is built from the
            # gateway-internal ``manga_token`` (this source's OWN card href), never
            # client input (SSRF-safe, T-13-01).
            async def _parse_links(t: str = manga_token) -> ExternalLinks | None:
                return await self.fetch_external_links(t, ctx)

            links = await ctx.resolve_external_links(manga_token, _parse_links)
            for rel in releases:
                rel.external_links = links
            return releases

        tasks: list[Coroutine[Any, Any, list[Release]]] = []
        for card in candidates:
            href = card.get("href")
            if not href:
                continue
            manga_token, slug_id = _manga_token_and_slug_id(str(href))
            if not slug_id:
                continue
            manga_title = str(card.get("title") or "Unknown")
            tasks.append(_fetch_candidate(manga_token, slug_id, manga_title))

        # gather (not TaskGroup): re-raise the FIRST child SourceError UNCHANGED so
        # fanout.py classifies it as the source's own failure (mirror atsumaru).
        results = await asyncio.gather(*tasks)
        releases: list[Release] = []
        for chunk in results:
            releases.extend(chunk)
        return releases

    async def fetch_external_links(
        self, series_id: str, ctx: SourceContext
    ) -> ExternalLinks | None:
        """One best-effort detail GET → ``{anilist, myAnimeList}`` (D-02/R6).

        Issues a SINGLE ``GET /manga/{series_id}`` (the detail HTML page) through the
        framework transport + the source's anti-bot session, parses the
        ``<script id="syncData">`` JSON off-loop (``asyncio.to_thread`` — lxml +
        ``json.loads`` are blocking), and routes it through the shared normalizer,
        which keeps ONLY ``anilist_id``/``mal_id`` and drops MangaFire's internal keys
        (``manga_id``/``page``/…).

        ``series_id`` is the gateway-internal ``manga_token`` (this source's OWN search
        card href), so the URL is never a Mangarr-supplied value (SSRF-safe, T-13-01).
        NO try/except/timeout here — the framework wrapper owns resolve-once + the 5s
        timeout + swallow-all-to-``None`` (D-03); a raising/slow GET or an
        absent/malformed ``syncData`` leaves the chapter releases intact (R6).
        """
        html = await ctx.get_bytes(f"{self.base_url}/manga/{series_id}")
        sync_data = await asyncio.to_thread(_parse_sync_data, html)
        return normalize(sync_data, "mangafire")

    def _chapters_to_releases(
        self,
        chapters: list[Any],
        manga_token: str,
        manga_title: str,
        lang: str,
        limit: int,
        ctx: SourceContext,
        req: SearchRequest,
    ) -> list[Release]:
        """Walk one candidate's chapter list → per-chapter Releases (GAP-2).

        Filtered by :meth:`chapter_matches`, kept NEWEST-FIRST (the chapter-list
        document order IS newest-first), sliced to ``limit``, THEN minted — handle
        count per candidate is bounded by ``limit`` so the returned releases' handles
        always survive the store cap.
        """
        rows: list[dict[str, Any]] = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            if not chapter.get("href"):
                continue  # no resolve unit
            number = self._parse_decimal(chapter.get("number"))
            if not self.chapter_matches(req, number):
                continue
            rows.append(chapter)
        releases: list[Release] = []
        for chapter in rows[:limit]:  # mint AFTER slice (newest-first preserved)
            rel = self._to_release(manga_token, manga_title, lang, chapter, ctx)
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
        """Newest-updated /filter cards → DIRECT newest-chapter releases (RCNT-01/02).

        GETs ``/filter?sort=recently_updated&language[]={lang}&page=1`` (HTML, NO vrf),
        parses the title-only cards, fans out the per-title chapter list under a bounded
        semaphore, and mints the NEWEST chapter DIRECT (the read href is always present,
        no ``:DEFERRED``). The route applies the authoritative newest-first sort +
        ``since`` cut; the source-side ``since`` is left to the route (IN-01). Zero
        networking glue (``get_bytes`` + ``get_json``).
        """
        lang = self._filter_language(languages)
        params = urlencode({"sort": "recently_updated", "language[]": lang, "page": 1})
        html = await ctx.get_bytes(f"{self.base_url}/filter?{params}")
        cards = await asyncio.to_thread(_parse_cards, html)
        bound = min(_RECENT_TITLE_CAP, limit or _RECENT_TITLE_CAP)
        sem = asyncio.Semaphore(_CHAPTERS_FANOUT_CONCURRENCY)

        async def _newest_release(
            manga_token: str, slug_id: str, manga_title: str
        ) -> Release | None:
            async with sem:
                rows = await self._chapter_list(slug_id, lang, ctx)
            newest = next(
                (r for r in rows if isinstance(r, dict) and r.get("href")), None
            )
            if newest is None:
                return None
            return self._to_release(manga_token, manga_title, lang, newest, ctx)

        tasks: list[Coroutine[Any, Any, Release | None]] = []
        for card in cards[:bound]:
            href = card.get("href")
            if not href:
                continue
            manga_token, slug_id = _manga_token_and_slug_id(str(href))
            if not slug_id:
                continue
            manga_title = str(card.get("title") or "Unknown")
            tasks.append(_newest_release(manga_token, slug_id, manga_title))

        results = await asyncio.gather(*tasks)
        return [rel for rel in results if rel is not None]

    # ─────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a read href → ordered SSRF-allowlisted page URLs (PKG-01/R6, D-08).

        ``chapter_id`` is the read href (``/read/{slug}.{id}/{lang}/chapter-{number}``,
        D-09) minted by search/recent. Navigates it in the warm browser via
        ``solver.fetch_via_browser(read_url, extract=_IMAGE_LIST_EXTRACT_JS)`` — the
        reader's own JS mints the per-request vrf and auto-fires the image-list AJAX,
        which the extract captures from ``performance`` and returns as ``[[url, offset],
        …]``. Each captured URL is SSRF-allowlisted (fragment stripped first; a
        non-allowlisted URL raises ``source_unavailable`` pre-fetch — SEC-01), and the
        scramble offset rides as a ``#scr_{offset}`` fragment when ``offset>0`` (D-10).
        The extracted count is guarded against ``ctx.expected_pages`` when known.

        Bounded retry (debug mangafire-manifest-contention, 2026-06-19): an empty/
        malformed capture under browser contention self-heals, so on that result we
        re-navigate & re-extract up to ``_MANIFEST_EMPTY_RETRIES`` extra times with
        ``_MANIFEST_RETRY_BACKOFF`` before raising "malformed mangafire chapter
        manifest". The SSRF allowlist, offset parsing, and ``expected_pages`` integrity
        check run on the FINAL successful capture only — an integrity mismatch is a real
        data problem and is NOT retried.
        """
        if not chapter_id.startswith("/read/"):
            raise SourceError(
                "source_unavailable",
                f"malformed mangafire chapter id {chapter_id!r} (want a /read/ href)",
            )
        solver = self._solver_from_ctx(ctx)
        read_url = urljoin(self.base_url + "/", chapter_id.lstrip("/"))
        captured = await self._capture_image_list(solver, read_url)
        urls: list[str] = []
        for entry in captured:
            if not isinstance(entry, (list, tuple)) or not entry:
                raise SourceError(
                    "source_unavailable", "malformed mangafire image entry"
                )
            raw_url = entry[0]
            if not isinstance(raw_url, str) or not raw_url:
                raise SourceError(
                    "source_unavailable", "mangafire image entry missing url"
                )
            clean, _frag = urldefrag(raw_url)
            if not _is_allowed_image_url(clean):
                # Never fetch a non-allowlisted (off-host / off-shape) URL (SEC-01).
                raise SourceError(
                    "source_unavailable",
                    f"image URL failed the SSRF allowlist: {clean!r}",
                )
            offset = self._parse_int(entry[1]) if len(entry) >= 2 else 0
            urls.append(f"{clean}#scr_{offset}" if offset and offset > 0 else clean)
        if ctx.expected_pages is not None and len(urls) != ctx.expected_pages:
            raise SourceError(
                "source_unavailable",
                f"manifest integrity: extracted {len(urls)} images, "
                f"chapter declares {ctx.expected_pages} pages",
            )
        return urls

    async def _capture_image_list(self, solver: Any, read_url: str) -> list[Any]:
        """Navigate the read page + run the capture extract, retrying empty captures.

        Returns the raw ``[[url, offset], …]`` list from ``_IMAGE_LIST_EXTRACT_JS``.
        Under browser contention the capture intermittently comes back empty/malformed
        (the reader AJAX never fired, or the in-page re-fetch threw — debug
        ``mangafire-manifest-contention``); that self-heals, so on an empty/malformed
        capture (or a ``fetch_via_browser`` raise) we re-navigate up to
        ``_MANIFEST_EMPTY_RETRIES`` extra times with ``_MANIFEST_RETRY_BACKOFF`` before
        raising. A non-empty capture is returned as-is — its SSRF/offset/integrity
        validation (incl. the ``expected_pages`` mismatch, a real data problem that is
        NOT retried) happens in the caller.
        """
        last_exc: Exception | None = None
        total_attempts = _MANIFEST_EMPTY_RETRIES + 1
        for attempt in range(total_attempts):
            if attempt > 0:
                backoff_idx = min(attempt - 1, len(_MANIFEST_RETRY_BACKOFF) - 1)
                await asyncio.sleep(_MANIFEST_RETRY_BACKOFF[backoff_idx])
            try:
                captured = await solver.fetch_via_browser(
                    read_url,
                    extract=_IMAGE_LIST_EXTRACT_JS,
                    wait_for=None,
                    timeout=60.0,
                )
            except Exception as exc:  # noqa: BLE001 — retried then surfaced typed
                last_exc = exc
                continue
            if isinstance(captured, list) and captured:
                return captured
            # empty/malformed capture — retry (contention self-heals); no exc to chain.
            last_exc = None
        if last_exc is not None:
            raise SourceError(
                "source_unavailable", f"browser manifest fetch failed: {last_exc}"
            ) from last_exc
        raise SourceError("source_unavailable", "malformed mangafire chapter manifest")

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        """Fetch one page image's bytes + geometric descramble (PKG-02, D-11/D-12).

        Strips the ``#scr_{offset}`` fragment, fetches the CLEAN URL via
        ``ctx.get_bytes`` (NEVER the browser — CLAUDE.md) with adaptive CDN zone-retry
        (see :meth:`_get_bytes_zone_retry` / debug
        ``mangafire-stale-manifest-reresolve-budget``), and when ``offset>0``
        descrambles in Pillow offloaded via ``asyncio.to_thread`` (ruff ASYNC). An
        ``offset==0`` page is returned unchanged. The descramble always runs on the
        bytes from WHICHEVER zone answered, so a zone switch never bypasses the offset
        un-shuffle.
        """
        clean, frag = urldefrag(url)
        offset = 0
        if frag.startswith("scr_"):
            offset = self._parse_int(frag[4:]) or 0
        content = await self._get_bytes_zone_retry(clean, ctx)
        if offset > 0:
            return await asyncio.to_thread(_descramble_image, content, offset)
        return content

    async def _get_bytes_zone_retry(self, clean_url: str, ctx: SourceContext) -> bytes:
        """``ctx.get_bytes`` the clean URL, retrying across CDN zones on a 403.

        MangaFire's per-page 403 is a Cloudflare WAF deny of the gateway's egress IP on
        a particular ``mfcdnN`` CDN zone, not a stale/expired signed URL (debug
        ``mangafire-stale-manifest-reresolve-budget``). The IDENTICAL signed path
        returns 200 on an un-blocked zone, so on a 403 we re-fetch the same path with
        the host's zone label rewritten to each OTHER known zone
        (:data:`_MANGAFIRE_CDN_ZONES`) until one answers; the winning zone is remembered
        on the per-job context (:data:`_PREFERRED_ZONE_ATTR`) so the rest of this job
        tries it FIRST and never re-probes the blocked zone every page.

        Every rewritten URL is re-validated through :func:`_is_allowed_image_url` (SSRF,
        SEC-01) before it is fetched — a rewrite that somehow failed the allowlist is
        skipped, never fetched blindly. A non-403 ``SourceError`` (a genuine page loss)
        propagates unchanged. When EVERY zone 403s the request raises a clear terminal
        ``source_unavailable`` naming the blocked host — NOT the old stale-manifest
        wording (the engine also opts mangafire out of its re-resolve recovery).
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

    # ─────────────────────────── helpers ──────────────────────────────────────

    async def _chapter_list(
        self, slug_id: str, lang: str, ctx: SourceContext
    ) -> list[dict[str, Any]]:
        """GET the per-manga chapter list → ordered chapter rows (D-06).

        ``GET /ajax/manga/{slugId}/chapter/{lang}`` returns ``{"result":"<ul>…</ul>"}``
        (HTML-in-JSON, NO vrf); the ``result`` HTML is lxml-parsed off the event loop
        via ``asyncio.to_thread``. Consumed by BOTH search and recent.
        """
        body = await ctx.get_json(
            f"{self.base_url}/ajax/manga/{slug_id}/chapter/{lang}"
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, str):
            return []
        return await asyncio.to_thread(_parse_chapter_list, result)

    @staticmethod
    def _solver_from_ctx(ctx: SourceContext, *, need_typed: bool = False) -> Any:
        """Pull the AntiBotSolver out of ``ctx`` for the browser-fetch paths (D-41).

        The framework wires the solver into ``SourceContext`` for any ``cloudflare*``
        source. ``fetch_manifest`` needs the one-shot ``fetch_via_browser`` primitive;
        ``search`` ALSO needs the off-Protocol ``fetch_via_browser_typed`` real-keyboard
        primitive (``need_typed=True``). Raises ``SourceError`` when the solver is
        missing OR lacks a required primitive (a wiring bug, not a runtime condition).
        """
        solver = getattr(ctx, "_solver", None)
        if solver is None or not hasattr(solver, "fetch_via_browser"):
            raise SourceError(
                "source_unavailable",
                "mangafire browser-fetch requires a solver with fetch_via_browser",
            )
        if need_typed and not hasattr(solver, "fetch_via_browser_typed"):
            raise SourceError(
                "source_unavailable",
                "mangafire search requires a solver with fetch_via_browser_typed",
            )
        return solver

    @classmethod
    def _filter_language(cls, languages: list[str] | None) -> str:
        """The single language threaded into the /filter + chapter-list URLs.

        MangaFire is multi-language and the feeds are language-scoped, so a search/
        recent request picks the first requested language (or the English default).
        """
        if languages:
            return languages[0]
        return _DEFAULT_LANGUAGE

    # ─────────────────────────── Release normalization ───────────────────────────

    def _to_release(
        self,
        manga_token: str,
        manga_title: str,
        lang: str,
        chapter: dict[str, Any],
        ctx: SourceContext,
    ) -> Release | None:
        """Mint one Release from a single parsed chapter row (D-08/D-09)."""
        href = chapter.get("href")
        if not href:
            return None
        href = str(href)
        chapter_number = self._parse_decimal(chapter.get("number"))
        publish_date = (
            _iso_from_mangafire_date(chapter.get("date"))
            or datetime.now(UTC).isoformat()
        )
        ch_str = (
            format(chapter_number.normalize(), "f")
            if chapter_number is not None
            else "?"
        )
        slug_id = manga_token.rsplit(".", 1)[-1] if "." in manga_token else manga_token
        title = self._build_title(manga_title, ch_str, lang)
        # The read href is the true per-chapter resolve unit — fold its unique tail
        # into the guid so chapters with a blank data-number (ch_str=="?") don't
        # collide and get silently deduped away by Mangarr (issue #219).
        href_slug = href.rstrip("/").rsplit("/", 1)[-1]
        guid = f"mangafire:{manga_token}:ch-{ch_str}:{lang}:{href_slug}"

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=href,  # the read href is the resolve unit (D-09)
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
            ids={"mangafireMangaId": manga_token, "mangafireSlugId": slug_id},
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
