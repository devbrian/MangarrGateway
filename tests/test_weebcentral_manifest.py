"""Unit tests for ``WeebCentralSource.fetch_manifest`` — reading_style + SSRF.

``GET /chapters/{id}/images?…&reading_style=long_strip`` returns server-rendered
``<img src="https://{cdn}/manga/{Series}/{NNNN}-{PPP}.png">`` (live recon 2026-06-08
— absolute, ordered, ALL pages in one call). ``fetch_manifest``:

* sends ``reading_style=long_strip`` (REQUIRED — the endpoint 307s to 0 imgs without
  it) and no ``HX-Request`` header;
* extracts the ordered ``<img src>`` URLs VERBATIM (never reconstructed);
* SSRF-allowlists EVERY url (host-UNPINNED: ``https`` + public host + ``/manga/`` +
  image ext, no traversal / internal-host) BEFORE return — raising on the first
  rejection;
* raises on 0 imgs and enforces the ``ctx.expected_pages`` integrity guard when set.

No network: a fake ``SourceContext`` returns the canned images HTML via ``get_bytes``
and records the requested URL (with its query string) for assertions.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.weebcentral import WeebCentralSource

_BASE = "https://weebcentral.com"
_CHAPTER = "01KSTMTWZE2WHXAP7E18YEH80N"


def _images_html(urls: list[str]) -> bytes:
    imgs = "".join(f'<img src="{u}" alt="page">' for u in urls)
    return f"<div>{imgs}</div>".encode()


class _FakeCtxForManifest:
    """``SourceContext`` stand-in: a canned images body via ``get_bytes``."""

    def __init__(self, html: bytes, *, expected_pages: int | None = None) -> None:
        self._html = html
        self.expected_pages = expected_pages
        self.calls: list[str] = []

    async def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        return self._html


def _cdn(host: str, series: str, n: int) -> str:
    return f"https://{host}/manga/{series}/1184-{n:03d}.png"


@pytest.mark.asyncio
async def test_fetch_manifest_sends_reading_style_and_extracts_in_order() -> None:
    urls = [_cdn("scans-hot.planeptune.us", "One-Piece", n) for n in (1, 2, 3)]
    ctx = _FakeCtxForManifest(_images_html(urls))
    out = await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]

    assert out == urls  # verbatim, in document order
    assert len(ctx.calls) == 1
    parsed = urlparse(ctx.calls[0])
    assert parsed.path == f"/chapters/{_CHAPTER}/images"
    qs = parse_qs(parsed.query)
    assert qs["reading_style"] == ["long_strip"]  # the #1 trap — REQUIRED
    assert qs["is_prev"] == ["False"]
    assert qs["current_page"] == ["1"]


@pytest.mark.asyncio
async def test_fetch_manifest_accepts_rotated_cdn_host() -> None:
    """Host-UNPINNED allowlist: a DIFFERENT public CDN host still passes."""
    urls = [
        _cdn("scans.lastation.us", "Some-Manhwa", 1),
        _cdn("official.lowee.us", "Some-Manhwa", 2),
    ]
    ctx = _FakeCtxForManifest(_images_html(urls))
    out = await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]
    assert out == urls


@pytest.mark.asyncio
async def test_fetch_manifest_empty_raises() -> None:
    ctx = _FakeCtxForManifest(b"<div></div>")
    with pytest.raises(SourceError, match="reading_style"):
        await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_internal_raises_ssrf() -> None:
    urls = [
        _cdn("scans-hot.planeptune.us", "One-Piece", 1),
        "https://metadata.google.internal/manga/x/1184-002.png",
    ]
    ctx = _FakeCtxForManifest(_images_html(urls))
    with pytest.raises(SourceError, match="SSRF"):
        await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_non_https_raises_ssrf() -> None:
    ctx = _FakeCtxForManifest(
        _images_html(["http://scans-hot.planeptune.us/manga/x/1184-001.png"])
    )
    with pytest.raises(SourceError, match="SSRF"):
        await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_namespace_raises_ssrf() -> None:
    # A path OUTSIDE the /manga/ namespace must fail the allowlist.
    ctx = _FakeCtxForManifest(
        _images_html(["https://scans-hot.planeptune.us/secret/1184-001.png"])
    )
    with pytest.raises(SourceError, match="SSRF"):
        await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_traversal_raises_ssrf() -> None:
    ctx = _FakeCtxForManifest(
        _images_html(["https://scans-hot.planeptune.us/manga/../secret/x.png"])
    )
    with pytest.raises(SourceError, match="SSRF"):
        await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_non_image_ext_raises_ssrf() -> None:
    ctx = _FakeCtxForManifest(
        _images_html(["https://scans-hot.planeptune.us/manga/x/payload.svg"])
    )
    with pytest.raises(SourceError, match="SSRF"):
        await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_pages_integrity_guard() -> None:
    urls = [_cdn("scans-hot.planeptune.us", "One-Piece", n) for n in (1, 2)]
    ctx = _FakeCtxForManifest(_images_html(urls), expected_pages=3)
    with pytest.raises(SourceError, match="integrity"):
        await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_pages_integrity_passes_on_match() -> None:
    urls = [_cdn("scans-hot.planeptune.us", "One-Piece", n) for n in (1, 2)]
    ctx = _FakeCtxForManifest(_images_html(urls), expected_pages=2)
    out = await WeebCentralSource().fetch_manifest(_CHAPTER, ctx)  # type: ignore[arg-type]
    assert len(out) == 2
