"""Unit tests for MangaBall ``fetch_manifest`` page extract + SSRF (Task 3 / GAP-3).

The manifest tail: GET ``/chapter-detail/{translation_id}/`` (HTML), extract the
ordered page URLs from the client-side ``const chapterImages = JSON.parse(`[…]`)``
array (GAP-3, live W-04 — the page images are NOT ``<img>`` tags; the only ``<img>``
on the page are the site logo + a group icon, which MUST be ignored), SSRF-allowlist
each (scheme https + the real ``/storage/.../{lang}/{NN}.jpg`` path shape — the CDN
host is read from the array, NEVER reconstructed, RECON §4), and guard the extracted
count against the chapter's ``pages``.

* A served HTML page whose ``chapterImages`` array has N URLs → N URLs in array
  order, with hosts that DIFFER from ``base_url`` (proves no host reconstruction)
  and with the logo/group-icon ``<img>`` chrome ignored.
* A non-allowlisted array entry (wrong scheme / wrong path shape / internal host) →
  SourceError, never fetched.
* A pages≠count mismatch → SourceError (integrity guard).
* No ``chapterImages`` marker → SourceError.

No network: a fake ``SourceContext`` serves the chapter-detail HTML via get_bytes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangaball import (
    MangaBallSource,
    _extract_chapter_image_urls,
    _is_allowed_image_url,
)


class _FakeCtxForManifest:
    """``SourceContext`` stand-in: serves chapter-detail HTML via get_bytes."""

    def __init__(self, detail_html: bytes) -> None:
        self.handle_store = HandleStore()
        self._detail_html = detail_html
        self.get_calls: list[str] = []

    async def get_bytes(self, url: str) -> bytes:
        self.get_calls.append(url)
        return self._detail_html


def _ctx(detail_html: bytes) -> Any:
    return _FakeCtxForManifest(detail_html)


def _page_url(host: str, tx_id: str, n: int, lang: str = "en") -> str:
    """A real-shape page URL: /storage/{titleId}/{vol}/{chap}/{tx}/{lang}/{NN}.jpg."""
    return (
        f"https://{host}/storage/68515cf3702284f834179a32/0/58/"
        f"{tx_id}/{lang}/{n:02d}.jpg"
    )


# The site chrome that lives on every chapter-detail page and MUST be ignored by the
# extractor (these are the ONLY <img> tags the live page renders — GAP-3).
_CHROME_IMGS = (
    '<a class="navbar-brand"><img src="/public/frontend/images/logo.svg"></a>'
    '<span class="chapter-badge"><img class="chapter-badge-icon" '
    'src="https://mangaball.net/storage/groups/icons/default.png"></span>'
)


def _detail_html(host: str, tx_id: str, pages: int) -> bytes:
    """Build chapter-detail HTML: chrome <img> tags + the chapterImages JSON array."""
    urls = [_page_url(host, tx_id, i + 1) for i in range(pages)]
    arr = json.dumps(urls)
    return (
        "<html><head></head><body>"
        f"{_CHROME_IMGS}"
        "<script>\n"
        "    const titleId = `68515cf3702284f834179a32`;\n"
        f"    const chapterImages = JSON.parse(`{arr}`);\n"
        "</script></body></html>"
    ).encode()


def _detail_html_with_urls(urls: list[str]) -> bytes:
    """Build chapter-detail HTML whose chapterImages array is exactly ``urls``."""
    return (
        "<html><body>"
        f"{_CHROME_IMGS}"
        f"<script>const chapterImages = JSON.parse(`{json.dumps(urls)}`);</script>"
        "</body></html>"
    ).encode()


# ─────────────────────────── _extract_chapter_image_urls ─────────────────────


def test_extract_ignores_logo_and_group_icon_chrome() -> None:
    """GAP-3: the logo + group-icon <img> chrome is NOT mistaken for page images;
    only the chapterImages JSON array is returned, in array order."""
    tx_id = "694f5f8e9b6877256628f2d0"
    host = "bulbasaur.poke-black-and-white.net"
    urls = _extract_chapter_image_urls(_detail_html(host, tx_id, 3))
    assert len(urls) == 3
    assert urls == [_page_url(host, tx_id, i + 1) for i in range(3)]
    # The chrome images never appear.
    assert all("logo.svg" not in u for u in urls)
    assert all("groups/icons" not in u for u in urls)


def test_extract_returns_empty_when_no_marker() -> None:
    html = b"<html><body><p>no reader here</p></body></html>"
    assert _extract_chapter_image_urls(html) == []


def test_extract_returns_empty_on_malformed_json() -> None:
    html = b"<script>const chapterImages = JSON.parse(`[not, valid json`);</script>"
    assert _extract_chapter_image_urls(html) == []


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


def test_is_allowed_image_url_rejects_non_https() -> None:
    url = "http://chikorita.red-and-blue.net/storage/t/0/58/tx/en/01.jpg"
    assert not _is_allowed_image_url(url)


def test_is_allowed_image_url_rejects_off_shape_path() -> None:
    # Wrong path prefix (not /storage/...).
    assert not _is_allowed_image_url("https://evil.example.net/etc/passwd")
    # The site logo lives under /public/, not /storage/ — rejected by the namespace.
    assert not _is_allowed_image_url("https://mangaball.net/public/frontend/logo.svg")
    # Covers live under /covers/, not /storage/ — rejected by the namespace.
    assert not _is_allowed_image_url(
        "https://bulbasaur.poke-black-and-white.net/covers/t/cover_1.webp"
    )
    # Under /storage/ but a non-image extension — rejected by the extension guard
    # (an HTML/JSON exfil target must never be fetched as a "page image").
    assert not _is_allowed_image_url("https://cdn.example.net/storage/t/chapter.html")
    assert not _is_allowed_image_url("https://cdn.example.net/storage/t/data.json")
    assert not _is_allowed_image_url("https://mangaball.net/storage/x/logo.svg")


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


# ───────────────────────────── fetch_manifest (search path) ─────────────────


@pytest.mark.asyncio
async def test_fetch_manifest_extracts_absolute_urls_in_order() -> None:
    tx_id = "6a1e164ac01e2cf095f75b1a"
    host = "chikorita.red-and-blue.net"
    ctx = _ctx(_detail_html(host, tx_id, 3))
    source = MangaBallSource()
    urls = await source.fetch_manifest(tx_id, ctx)

    assert len(urls) == 3
    # Array order preserved (01, 02, 03).
    assert urls[0].endswith("/en/01.jpg")
    assert urls[1].endswith("/en/02.jpg")
    assert urls[2].endswith("/en/03.jpg")
    # The hosts come from the array and differ from base_url (no reconstruction).
    for url in urls:
        assert host in url
        assert "mangaball.net" not in url
    # The chapter-detail GET used the bare translation id.
    assert len(ctx.get_calls) == 1
    assert ctx.get_calls[0] == f"https://mangaball.net/chapter-detail/{tx_id}/"


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_non_allowlisted_url() -> None:
    """An off-shape array entry raises SourceError (no blind fetch, SSRF)."""
    tx_id = "6a1e164ac01e2cf095f75b1a"
    host = "chikorita.red-and-blue.net"
    # A poisoned array: two good page URLs + one internal-host entry.
    poisoned = [
        _page_url(host, tx_id, 1),
        "https://internal.metadata.server/latest/meta-data/",
        _page_url(host, tx_id, 2),
    ]
    ctx = _ctx(_detail_html_with_urls(poisoned))
    source = MangaBallSource()
    with pytest.raises(SourceError) as excinfo:
        await source.fetch_manifest(tx_id, ctx)
    assert excinfo.value.code == "source_unavailable"
    # The offending URL is named in the error (observability, GAP-3).
    assert "metadata.server" in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_manifest_pages_count_mismatch_raises() -> None:
    """A pages≠extracted-count mismatch raises (integrity guard)."""
    tx_id = "6a1e164ac01e2cf095f75b1a"
    ctx = _ctx(_detail_html("chikorita.red-and-blue.net", tx_id, 3))
    source = MangaBallSource()
    with pytest.raises(SourceError) as excinfo:
        await source._manifest_for_translation(tx_id, 5, ctx)
    assert excinfo.value.code == "source_unavailable"


@pytest.mark.asyncio
async def test_fetch_manifest_empty_page_list_raises() -> None:
    tx_id = "6a1e164ac01e2cf095f75b1a"
    ctx = _ctx(b"<html><body><p>no images here</p></body></html>")
    source = MangaBallSource()
    with pytest.raises(SourceError) as excinfo:
        await source.fetch_manifest(tx_id, ctx)
    assert excinfo.value.code == "source_unavailable"


@pytest.mark.asyncio
async def test_fetch_image_delegates_to_get_bytes() -> None:
    class _ImgCtx:
        def __init__(self) -> None:
            self.fetched: list[str] = []

        async def get_bytes(self, url: str) -> bytes:
            self.fetched.append(url)
            return b"JPEGDATA"

    ctx = _ImgCtx()
    source = MangaBallSource()
    url = "https://chikorita.red-and-blue.net/storage/t/0/58/tx/en/01.jpg"
    data = await source.fetch_image(url, ctx)  # type: ignore[arg-type]
    assert data == b"JPEGDATA"
    assert ctx.fetched == [url]
