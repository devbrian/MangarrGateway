"""Unit tests for MangaBall ``fetch_manifest`` + ``fetch_image`` (v2 backend).

261003-mangaball-api-v2: the manifest is ``GET /api/v1/chapter-detail?chapter_id=<id>``
→ ``{"status","code","data":{"chapter":{…,"pages":[absolute URLs in order]},…}}``.
Each page URL is SSRF-allowlisted (https + public host + ``/storage/`` + image
extension — the CDN host is read from the response, NEVER reconstructed, RECON §4),
and the count is guarded against the chapter's expected pages when known.

* N ``pages`` → N URLs in order, with hosts that DIFFER from ``base_url``.
* A non-allowlisted entry → SourceError, never fetched.
* A pages≠count mismatch → SourceError (integrity guard).
* Empty pages / missing ``data`` / missing ``chapter`` → SourceError.
* ``fetch_image`` sends ``Referer: https://mangaball.com/`` (the CDN hotlink-blocks
  a bare GET).

No network: a fake ``SourceContext`` serves the chapter-detail JSON via get_json.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangaball import MangaBallSource, _is_allowed_image_url

_CHAPTER_DETAIL = "https://mangaball.com/api/v1/chapter-detail"

# The two exact live v2 CDN URL shapes (verified 2026-10-03).
_LIVE_POKE_URL = (
    "https://bulbasaur.poke-black-and-white.net/storage/"
    "689f5dbee93bccbdc8ffd5c7/0/48/harimanga/en/001.jpg"
)
_LIVE_RED_BLUE_URL = (
    "https://chikorita.red-and-blue.net/storage/6aaa08555e43ba036cfe18eb/0/11/"
    "atsu/en/6ac10263052042d762e1ee0c-001.avif"
)


class _FakeCtxForManifest:
    """``SourceContext`` stand-in: serves the chapter-detail JSON via get_json.

    ``expected_pages`` mirrors the real ``SourceContext.expected_pages`` integrity
    hint the engine forwards on the download path (#83/IN-03); ``None`` (the default)
    is the v2 case (no page count anywhere) where the guard is a no-op.
    """

    def __init__(
        self, detail: dict[str, Any], expected_pages: int | None = None
    ) -> None:
        self.handle_store = HandleStore()
        self._detail = detail
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.expected_pages = expected_pages

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.get_calls.append((url, params))
        return self._detail


def _ctx(detail: dict[str, Any], expected_pages: int | None = None) -> Any:
    return _FakeCtxForManifest(detail, expected_pages)


def _page_url(host: str, n: int, lang: str = "en") -> str:
    """A real-shape page URL: /storage/{titleId}/{vol}/{chap}/{group}/{lang}/NNN.jpg."""
    return f"https://{host}/storage/68515cf3702284f834179a32/0/58/harimanga/{lang}/{n:03d}.jpg"


def _detail(urls: list[str]) -> dict[str, Any]:
    """The v2 chapter-detail envelope carrying ``urls`` as ``data.chapter.pages``."""
    return {
        "status": "success",
        "code": 200,
        "data": {
            "chapter": {"id": "6a1e164ac01e2cf095f75b1a", "pages": urls},
            "title": {"id": "68515cf3702284f834179a32"},
            "group": {"id": "g1"},
        },
    }


def _detail_n(host: str, n: int) -> dict[str, Any]:
    return _detail([_page_url(host, i + 1) for i in range(n)])


# ───────────────────────────── _is_allowed_image_url ────────────────────────


def test_is_allowed_image_url_accepts_varying_cdn_hosts() -> None:
    """The CDN host varies per content — the allowlist keys on path + https,
    not a single host literal (RECON §4)."""
    base = "/storage/68515cf3702284f834179a32/0/58/694f5f8e/en/01.jpg"
    assert _is_allowed_image_url(f"https://chikorita.red-and-blue.net{base}")
    assert _is_allowed_image_url(f"https://bulbasaur.poke-black-and-white.net{base}")
    assert _is_allowed_image_url(f"https://jigglypuff.poke-black-and-white.net{base}")


def test_is_allowed_image_url_accepts_varying_filename_shapes() -> None:
    """GAP-3 round 2 (live W-04): the page FILENAME varies by upload source and is
    NOT pinned — only the ``/storage/`` namespace + image extension + https + public
    host are enforced. All of these are real live shapes that MUST be accepted."""
    host = "https://chikorita.red-and-blue.net"
    # Zero-padded page index (daomeoden group dir).
    assert _is_allowed_image_url(f"{host}/storage/t/0/1184.1/daomeoden/vi/01.jpg")
    # translation-id-prefixed index.
    assert _is_allowed_image_url(
        f"{host}/storage/t/0/1184.1/daomeoden/vi/6a1e164ac01e2cf095f75b1a-001.jpg"
    )
    # Opaque token filename (comick/mangadex mirrors).
    assert _is_allowed_image_url(
        "https://bulbasaur.poke-black-and-white.net/storage/t/0/1184/mangadex/pt-br/HRK0MmP.png"
    )
    # .webp page.
    assert _is_allowed_image_url(
        "https://jigglypuff.poke-black-and-white.net/storage/t/0/7/comick/en/6a1c-001.webp"
    )
    # .avif page — the CDN's live format as of 260721 (debug avif-image-ssrf-allowlist).
    assert _is_allowed_image_url(
        "https://ampharos.red-and-blue.net/storage/68521c665cd6b2cdfbcfd79e/0/182/atsu/en/6a5f4e4b235a123b6c66f2f7-001.avif"
    )


def test_is_allowed_image_url_rejects_non_https() -> None:
    url = "http://chikorita.red-and-blue.net/storage/t/0/58/tx/en/01.jpg"
    assert not _is_allowed_image_url(url)


def test_is_allowed_image_url_rejects_off_shape_path() -> None:
    # Wrong path prefix (not /storage/...).
    assert not _is_allowed_image_url("https://evil.example.net/etc/passwd")
    # The site logo lives under /public/, not /storage/ — rejected by the namespace.
    assert not _is_allowed_image_url("https://mangaball.com/public/frontend/logo.svg")
    # Covers live under /covers/, not /storage/ — rejected by the namespace.
    assert not _is_allowed_image_url(
        "https://bulbasaur.poke-black-and-white.net/covers/t/cover_1.webp"
    )
    # Under /storage/ but a non-image extension — rejected by the extension guard
    # (an HTML/JSON exfil target must never be fetched as a "page image").
    assert not _is_allowed_image_url("https://cdn.example.net/storage/t/chapter.html")
    assert not _is_allowed_image_url("https://cdn.example.net/storage/t/data.json")
    assert not _is_allowed_image_url("https://mangaball.com/storage/x/logo.svg")


def test_is_allowed_image_url_rejects_empty_host() -> None:
    assert not _is_allowed_image_url("https:///storage/t/0/58/tx/en/01.jpg")


def test_is_allowed_image_url_rejects_path_traversal() -> None:
    """CR-01: a raw ``/storage/..`` path matches the regex but httpx fetches the
    NORMALIZED path off-shape — the guard must reject the traversal vector."""
    # Raw path contains ``..``; httpx would fetch /etc/01.jpg outside /storage/.
    assert not _is_allowed_image_url(
        "https://cdn.example.net/storage/../../../etc/01.jpg"
    )
    # A traversal that still ends in the allowed shape after the ``..`` is also
    # rejected (normalized path escapes /storage/).
    assert not _is_allowed_image_url(
        "https://cdn.example.net/storage/a/b/../../../../x/01.jpg"
    )


def test_is_allowed_image_url_rejects_internal_metadata_hosts() -> None:
    """CR-01: the broad host regex matches ``metadata.google.internal`` and the
    like — internal/metadata namespaces must be rejected (cloud-metadata SSRF)."""
    base = "/storage/t/0/58/tx/en/01.jpg"
    assert not _is_allowed_image_url(f"https://metadata.google.internal{base}")
    assert not _is_allowed_image_url(f"https://internal.corp.local{base}")
    assert not _is_allowed_image_url(f"https://foo.localhost{base}")


def test_is_allowed_image_url_accepts_live_v2_cdn_shapes() -> None:
    """Both exact live v2 page URLs (jpg on poke-black-and-white, avif on
    red-and-blue) pass the allowlist (261003-mangaball-api-v2)."""
    assert _is_allowed_image_url(_LIVE_POKE_URL)
    assert _is_allowed_image_url(_LIVE_RED_BLUE_URL)


# ───────────────────────────── fetch_manifest ───────────────────────────────


@pytest.mark.asyncio
async def test_fetch_manifest_returns_pages_in_order() -> None:
    row_id = "6a1e164ac01e2cf095f75b1a"
    host = "chikorita.red-and-blue.net"
    ctx = _ctx(_detail_n(host, 3))
    urls = await MangaBallSource().fetch_manifest(row_id, ctx)

    assert len(urls) == 3
    assert urls[0].endswith("/en/001.jpg")
    assert urls[1].endswith("/en/002.jpg")
    assert urls[2].endswith("/en/003.jpg")
    # Hosts come from the response and differ from base_url (no reconstruction).
    for url in urls:
        assert host in url
        assert "mangaball.com" not in url
    # Exactly one chapter-detail GET with the row id as a query param.
    assert ctx.get_calls == [(_CHAPTER_DETAIL, {"chapter_id": row_id})]


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_non_allowlisted_url() -> None:
    """An off-shape pages entry raises SourceError (no blind fetch, SSRF)."""
    host = "chikorita.red-and-blue.net"
    poisoned = [
        _page_url(host, 1),
        "https://internal.metadata.server/latest/meta-data/",
        _page_url(host, 2),
    ]
    ctx = _ctx(_detail(poisoned))
    with pytest.raises(SourceError) as excinfo:
        await MangaBallSource().fetch_manifest("a" * 24, ctx)
    assert excinfo.value.code == "source_unavailable"
    # The offending URL is named in the error (observability).
    assert "metadata.server" in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_manifest_pages_count_mismatch_raises() -> None:
    """A pages≠count mismatch raises (integrity guard)."""
    ctx = _ctx(_detail_n("chikorita.red-and-blue.net", 3))
    with pytest.raises(SourceError) as excinfo:
        await MangaBallSource()._manifest_for_translation("a" * 24, 5, ctx)
    assert excinfo.value.code == "source_unavailable"


@pytest.mark.asyncio
async def test_fetch_manifest_engages_pages_guard_via_expected_pages() -> None:
    """#83/IN-03: ``fetch_manifest`` engages the guard from ``ctx.expected_pages``."""
    ctx = _ctx(_detail_n("chikorita.red-and-blue.net", 3), expected_pages=5)
    with pytest.raises(SourceError) as excinfo:
        await MangaBallSource().fetch_manifest("a" * 24, ctx)
    assert excinfo.value.code == "source_unavailable"
    assert "integrity" in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_manifest_expected_pages_match_passes() -> None:
    ctx = _ctx(_detail_n("chikorita.red-and-blue.net", 3), expected_pages=3)
    urls = await MangaBallSource().fetch_manifest("a" * 24, ctx)
    assert len(urls) == 3


@pytest.mark.asyncio
async def test_fetch_manifest_expected_pages_none_skips_guard() -> None:
    """``expected_pages=None`` (the v2 default) degrades the guard to a no-op."""
    ctx = _ctx(_detail_n("chikorita.red-and-blue.net", 3))
    urls = await MangaBallSource().fetch_manifest("a" * 24, ctx)
    assert len(urls) == 3


@pytest.mark.asyncio
async def test_fetch_manifest_empty_or_missing_pages_raises() -> None:
    """Empty pages / missing ``data`` / missing ``chapter`` → source_unavailable."""
    for detail in (
        _detail([]),
        {"status": "success", "code": 200},
        {"status": "success", "code": 200, "data": {"title": {}}},
    ):
        with pytest.raises(SourceError) as excinfo:
            await MangaBallSource().fetch_manifest("a" * 24, _ctx(detail))
        assert excinfo.value.code == "source_unavailable"


# ───────────────────────────── fetch_image ──────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_image_sends_mangaball_referer() -> None:
    """The CDN hotlink-blocks a bare GET (403) — the Referer must ride along."""

    class _ImgCtx:
        def __init__(self) -> None:
            self.fetched: list[tuple[str, dict[str, str] | None]] = []

        async def get_bytes_plain_with_headers(
            self, url: str, *, extra_headers: dict[str, str] | None = None
        ) -> tuple[bytes, httpx.Headers]:
            self.fetched.append((url, extra_headers))
            return b"JPEGDATA", httpx.Headers()

    ctx = _ImgCtx()
    data = await MangaBallSource().fetch_image(_LIVE_RED_BLUE_URL, ctx)  # type: ignore[arg-type]
    assert data == b"JPEGDATA"
    assert ctx.fetched == [(_LIVE_RED_BLUE_URL, {"Referer": "https://mangaball.com/"})]
