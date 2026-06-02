"""MangaBall source — the third declarative source + a third prep style (SRC-01).

MangaBall (``https://mangaball.net``) is a **MangaDex-class** source: a clean
JSON-REST backend with no response encryption and plain-CDN ``.jpg`` images
(RECON TL;DR). ~90% of this module is the MangaDex shape (guid/mint,
``_parse_decimal``, the manifest-integrity guard) plus the Comix ``:DEFERRED``
machinery for ``/recent`` (D-10/D-11).

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
``rate_limit_per_minute = 30`` is a conservative start (real ceiling unprobed —
live-tune, RECON Open Q3).

ENDPOINT SHAPES (live-recon-pinned, ``07-RECON-mangaball.md``):

* base: ``https://mangaball.net``
* search: ``POST /api/v1/title/search-advanced/`` (form) →
  ``{code,message,data:[Title…],pagination}``; each Title embeds a
  ``chapters`` list whose ``translations`` are the release granularity.
* recent: ``POST /api/v1/title/search/`` (form,
  ``search_type=getRecentlyUpdatedChapter``) → same Title shape, newest-first.
* chapter listing: ``POST /api/v1/chapter/chapter-listing-by-title-id/`` (form,
  ``title_id``) → the FLAT ``{code,message,ALL_CHAPTERS:[…],…}`` envelope (NOT
  the standard ``data`` envelope — :func:`_items_and_pagination` dispatches both,
  D-09).
* manifest: ``GET /chapter-detail/{translation_id}/`` (HTML) → absolute
  ``<img data-src="…">`` URLs in document order. The CDN host VARIES per content
  (``chikorita.red-and-blue.net``, ``bulbasaur.poke-black-and-white.net``, …) —
  the host is read from the DOM, NEVER reconstructed (RECON §4 / CLAUDE.md SSRF).
* image: plain httpx ``GET`` of each absolute CDN ``.jpg``.

guid (D-08): ``mangaball:{title_id}:ch-{number_float}:{language}:{translation_id}``
— the language + translation id are required because one chapter number maps to N
translations (one per language/group). The search-path
``ResolutionRecord.chapter_id`` is the ``translation_id``; the ``/recent`` path
mints a ``:DEFERRED`` composite resolved at ``fetch_manifest`` time (D-10).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..framework.base import Source
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

# Composite chapter-id separator + DEFERRED sentinel for the /recent late-bind
# (D-10), mirroring comix.py's ``_CID_SEP`` / ``_DEFERRED_SENTINEL``. MangaBall's
# composite is simpler than Comix's 4-field one: ``DEFERRED|{title_id}|{number}|
# {language}`` (translation_id is dropped — it is what fetch_manifest resolves).
_CID_SEP = "|"
_DEFERRED_SENTINEL = "DEFERRED"

# SSRF allowlist for the DOM-extracted page-image URLs (T-07-07/T-07-09,
# CLAUDE.md). The CDN host VARIES per content (RECON §4) so — unlike Comix — it
# cannot be pinned to one literal; we allowlist the ``https`` scheme + the
# observed ``/storage/.../{id}-{NNN}.jpg`` path shape only. A poisoned DOM (or a
# future extractor regression) surfacing an off-shape path is rejected before any
# fetch. ``[a-z0-9.-]`` host guard rejects empty / userinfo-bearing hosts.
_MANGABALL_IMG_PATH_RE = re.compile(
    r"^/storage/[A-Za-z0-9_/.-]+/[A-Za-z0-9]+-\d{3,}\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_MANGABALL_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)


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


def _is_allowed_image_url(url: str) -> bool:
    """True if ``url`` looks like a MangaBall CDN page image (SSRF allowlist).

    Belt-and-suspenders defence on every DOM-extracted manifest URL before the
    framework fetches it (T-07-07/T-07-09). Rejects non-HTTPS schemes, empty /
    malformed hosts, and any path that does not match the observed
    ``/storage/.../{id}-{NNN}.jpg`` shape. The host is NOT pinned to a literal —
    the MangaBall CDN host varies per content (RECON §4) — so the path shape +
    ``https`` carry the guard.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and bool(_MANGABALL_HOST_RE.match(host))
        and bool(_MANGABALL_IMG_PATH_RE.match(parsed.path))
    )


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
    # Conservative start — real ceiling unprobed (RECON Open Q3); live-tune.
    rate_limit_per_minute = 30
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
        """Keyword search → one Release per translation (SRCH-01..07, D-08).

        POSTs ``/api/v1/title/search-advanced/`` with the recon-observed form body
        (``search_input`` + default filters), parses the standard envelope via
        :func:`_items_and_pagination`, and for each Title's chapters resolves each
        ``translation`` into a Release carrying the fully-specific D-08 guid and an
        opaque minted handle. ZERO networking glue — ``ctx.post_json`` owns the
        form encode, CSRF injection, rate limit, retry, and health feed (SRC-02).
        """
        form: dict[str, Any] = {
            "search_input": req.query or "",
            **_SEARCH_DEFAULT_FILTERS,
        }
        body = await ctx.post_json(
            f"{self.base_url}/api/v1/title/search-advanced/", data=form
        )
        titles, _pagination = _items_and_pagination(body)

        releases: list[Release] = []
        for title in titles:
            if not isinstance(title, dict):
                continue
            releases.extend(self._title_to_releases(title, ctx))
        return releases

    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        """Newest-first recent chapters via deferred resolution (RCNT-01/02, D-10).

        Implemented in Task 2.
        """
        raise NotImplementedError  # pragma: no cover - Task 2

    # ───────────────────────── R6 fetch/package hooks (PKG-01/02) ────────────────

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        """Resolve a chapter id → ordered page-image URLs (PKG-01/R6).

        Implemented in Task 3 (HTML ``img[data-src]`` extract + SSRF allowlist).
        """
        raise NotImplementedError  # pragma: no cover - Task 3

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

    def _title_to_releases(
        self, title: dict[str, Any], ctx: SourceContext
    ) -> list[Release]:
        """Resolve one Title's chapters/translations into Releases (D-08).

        Each ``translation`` of each chapter is one grabbable unit → one Release.
        HTML-string Title fields (``alternateName``/``status``/``last_chapter``) are
        stripped via :func:`_strip_html`; only the plain ``name`` is the manga title.
        """
        title_id = title.get("_id")
        if not title_id:
            return []
        manga_title = _strip_html(title.get("name")) or "Unknown"

        releases: list[Release] = []
        for chapter in title.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            number = self._parse_decimal(chapter.get("number_float"))
            for translation in chapter.get("translations") or []:
                if not isinstance(translation, dict):
                    continue
                rel = self._to_release(
                    str(title_id), manga_title, number, translation, ctx
                )
                if rel is not None:
                    releases.append(rel)
        return releases

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
        publish_date = str(translation.get("date") or "")
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
