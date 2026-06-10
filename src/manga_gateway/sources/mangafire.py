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

import io
import posixpath
import re
from typing import Any
from urllib.parse import urldefrag, urlparse

import lxml.html
from PIL import Image, UnidentifiedImageError

# ── SSRF allowlist (host-agnostic; copy the mangaball/weebcentral shape, D-10) ──
# The page-image host is NEVER pinned (it varies per content), so the meaningful
# invariants carry the trust: https + public-host shape + /mf/ namespace + image
# extension + no traversal. The internal/metadata suffixes are rejected explicitly
# (the broad host regex would otherwise accept ``metadata.google.internal``).
_MANGAFIRE_IMG_PATH_RE = re.compile(
    r"^/mf/[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_MANGAFIRE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)
_MANGAFIRE_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".localhost")

# ── geometric descramble constants (D-12, Keiyoushi ImageInterceptor.kt) ────────
PIECE_SIZE = 200
MIN_SPLIT_COUNT = 5

# ── per-query vrf LRU cache size (D-13). The cache instance itself lives on the
# MangaFireSource instance (Task 2) so it never leaks across instances/tests; this
# is the size knob (Claude's discretion per the plan artifacts list). ─────────────
_VRF_CACHE_MAXSIZE = 20

# ── browser `extract` bodies (bare `return`; the framework wraps `async () => {…}`,
# mirroring comix `_CHAPTER_PAGES_EXTRACT_JS`). ─────────────────────────────────

# fetch_manifest (D-08): poll the browser's own `performance` resource entries for
# the reader's auto-fired `/ajax/read/chapter/` AJAX, re-`fetch()` it in-page with
# the XHR header, and return [url, offset] per image (index 0 = URL, index 2 =
# offset; offset==0 ⇒ not scrambled). ≤60×500ms.
_IMAGE_LIST_EXTRACT_JS = """
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 60; i++) {
    const hit = performance
      .getEntriesByType('resource')
      .map((e) => e.name)
      .find((u) => u.includes('/ajax/read/chapter/'));
    if (hit) {
      const r = await fetch(hit, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      const j = await r.json();
      const imgs = (j && j.result && j.result.images) || [];
      if (imgs.length) return imgs.map((it) => [it[0], it[2] || 0]);
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
        dst.save(buf, format=fmt)
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
