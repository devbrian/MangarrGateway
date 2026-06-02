"""Mangadot source — the 4th declarative source + the bare-array endpoint proof.

Mangadot (``https://mangadot.net``) is a **clean-JSON** ``antibot="cloudflare"``
source (plaintext JSON, NOT ``+encrypted``): plain ``ctx.get_json`` /
``ctx.get_json_array`` / ``ctx.get_bytes`` calls auto-inject the captured
``cf_clearance`` + matching UA (D-40) and reconcile a challenge 403 (D-35). The
parsing analog is **mangaball/mangadex** (clean JSON, no browser-DOM read); the
Cloudflare class-attr analog is **comix** (``cloudflare_challenge_url`` +
``needs_solver_warm`` in the live profile).

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
* manifest: ``GET /api/chapters/{chapter_id}/images`` →
  ``{chapter,images:[{url,w,h}],prev_chapter_id,next_chapter_id,…}``. ``images[].url``
  is **relative, same-origin** (``/chapters/manga_5296/chapter_26/001.webp``);
  prepend ``base_url``. No separate CDN. ``w/h`` often ``0`` → ignored.
* image: ``GET https://mangadot.net{images[].url}`` (webp/jpg/png) via
  ``ctx.get_bytes`` (one-line delegate).

guid (D-08): ``mangadot:{manga_id}:ch-{number}:{language}:{chapter_id}`` — the
language + chapter id are required because one chapter number maps to N
language/group rows. ``ResolutionRecord.chapter_id`` is the bare chapter ``id``
(DIRECT, no ``:DEFERRED`` — Mangadot exposes the stable id, unlike Comix's recent
feed).

recent(): a verbatim no-op (``return []``, ``supports_recent=False``). Mangadot
exposes no clean chapter-level JSON feed (CONTEXT.md / RESEARCH.md — live-probed
2026-06-02). The core value (search → handle → download → CBZ, R1/R6) is fully
delivered without recent(). Tracked for revisit as a GitHub issue (mirrors comix #31).

⚠️ Chapter-id provenance oddity (RESEARCH.md, CONFIRMED LIVE): some junk/aggregated
manga entries (e.g. manga 101) list chapter ids that resolve to OTHER manga. The
live-smoke ``default_query`` is chosen so its ``chapters/list`` ids resolve back to
the SAME manga with a matching ``page_count`` (see ``tests/live/profiles/mangadot.py``).
"""

from __future__ import annotations

import posixpath
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..framework.base import Source
from ..framework.errors import SourceError
from ..handles.store import ResolutionRecord
from ..models.search import Release

if TYPE_CHECKING:
    from ..framework.context import SourceContext
    from ..models.search import SearchRequest

# search() deep-enumerates this many manga candidates (mangaball GAP-1 lock —
# search is TITLE-ONLY, so each candidate is a full chapters/list fan-out). Like
# mangaball, ``req.interactive`` does NOT widen the candidate count.
_DEFAULT_MANGA_CANDIDATES = 5

# Floor for empty/malformed timestamps so they sort oldest and never crash.
_TS_FLOOR = datetime.min.replace(tzinfo=UTC)

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
    host_ok = host == "mangadot.net" or host.endswith(".mangadot.net")
    if not host_ok:
        return False
    # Reject traversal outright; validate the NORMALIZED path httpx will fetch.
    if ".." in parsed.path.split("/"):
        return False
    norm_path = posixpath.normpath(parsed.path)
    return parsed.scheme == "https" and bool(_MANGADOT_IMG_PATH_RE.match(norm_path))


class MangadotSource(Source):
    """Mangadot (mangadot.net) — clean-JSON ``cloudflare`` source (bare-array list).

    A mangadex/mangaball-class clean-JSON source whose only new requirement is the
    bare top-level JSON array chapter-list endpoint, consumed via the additive
    ``ctx.get_json_array`` (full ``get_json`` parity). Cloudflare clearance is
    framework-owned (D-40/D-35) — this source declares only the class-attrs and
    adds zero networking glue (see module docstring).
    """

    key = "mangadot"
    name = "Mangadot"
    base_url = "https://mangadot.net"
    # Title-search only — no external metadata-id namespace.
    id_types: list[str] = []
    # English-only at v1 (widen if the live loop shows other langs present).
    languages = ["en"]
    # Conservative start — origin 504s under load (CONTEXT.md). Live-tune.
    rate_limit_per_minute = 10
    # Plaintext JSON behind Cloudflare (NOT +encrypted). The framework injects
    # clearance (D-40) + reconciles a challenge 403 (D-35) for any cloudflare* source.
    antibot = "cloudflare"
    decrypt_scheme = None
    # The URL the framework solver navigates to so Cloudflare issues a cf_clearance
    # cookie. Per-domain CF (#90) auto-wires the challenge_urls map from this attr.
    cloudflare_challenge_url = "https://mangadot.net/"
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
        array, consumed by ``ctx.get_json_array``). One Release is minted per
        chapter row — and per language/group row when one ``chapter_number`` has
        multiple rows (D-08). Releases are language-filtered by ``req.languages``,
        ordered NEWEST-FIRST by parsed ``date_added``, sliced to ``req.limit``, and
        ONLY THEN minted (mangaball GAP-2 mint-after-slice — handle-eviction
        lesson). ZERO networking glue — both calls ride ``ctx`` (SRC-01/02).
        """
        envelope = await ctx.get_json(
            f"{self.base_url}/api/search",
            search=req.query or "",
            sortBy="relevance",
            page=1,
        )
        manga_list = envelope.get("manga_list")
        candidates = [m for m in (manga_list or []) if isinstance(m, dict)][
            :_DEFAULT_MANGA_CANDIDATES
        ]
        wanted_langs = set(req.languages) if req.languages else None
        limit = req.limit or 50

        releases: list[Release] = []
        for manga in candidates:
            manga_id = manga.get("id")
            if manga_id is None:
                continue
            manga_id = str(manga_id)
            manga_title = str(manga.get("title") or "Unknown")
            rows = await ctx.get_json_array(
                f"{self.base_url}/api/manga/{manga_id}/chapters/list"
            )
            releases.extend(
                self._chapters_to_releases(
                    rows, manga_id, manga_title, wanted_langs, limit, ctx
                )
            )
        return releases

    def _chapters_to_releases(
        self,
        rows: list[Any],
        manga_id: str,
        manga_title: str,
        wanted_langs: set[str] | None,
        limit: int,
        ctx: SourceContext,
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
            sortable.append((self._parse_ts(row.get("date_added")), row))
        sortable.sort(key=lambda item: item[0], reverse=True)  # newest-first

        releases: list[Release] = []
        for _ts, row in sortable[:limit]:  # mint AFTER slice (GAP-2)
            rel = self._to_release(manga_id, manga_title, row, ctx)
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

        GETs ``/api/chapters/{chapter_id}/images`` (a JSON object) via
        ``ctx.get_json``, reads the ordered ``images`` array (array order = page
        order), prepends ``base_url`` to each RELATIVE ``/chapters/...`` url, and
        SSRF-allowlists every resulting URL (:func:`_is_allowed_image_url`) BEFORE
        return. The image URLs are EXTRACTED from the response, NEVER reconstructed
        (T-07 / CLAUDE.md SSRF). Empty ``images`` OR any URL failing the allowlist
        raises ``SourceError("source_unavailable", …)`` — no blind fetch
        (T-07-07/09). The manifest is consumed only by the gateway's own engine,
        never returned to a caller (R6).
        """
        body = await ctx.get_json(f"{self.base_url}/api/chapters/{chapter_id}/images")
        images = body.get("images")
        urls: list[str] = []
        for entry in images or []:
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

        language = str(row.get("language") or "en")
        chapter_number = self._parse_decimal(row.get("chapter_number"))
        page_count = self._parse_int(row.get("page_count"))
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

        handle = ctx.handle_store.mint(
            ResolutionRecord(
                source_key=self.key,
                chapter_id=chapter_id,  # the bare /api/chapters/{id}/images unit
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
