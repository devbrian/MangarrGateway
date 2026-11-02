"""Unit tests for ``KaganeSource.fetch_manifest`` — the 3-call token flow + SSRF.

``fetch_manifest(book_id)`` (live recon 2026-06-09):

1. ``POST https://kagane.moe/api/integrity`` ``{}`` → ``{token}`` (cf_clearance attached
   by the framework when held — on-demand since 261101-plw).
2. ``POST https://kagane.moe/api/v2/books/{book_id}?intent=Read`` with the
   ``x-integrity-token`` header → ``{access_token, cache_url, manifest:{pages:[{page_id,
   ext, page_no}]}}``.
3. EXTRACT each page URL by joining the server-provided ``cache_url`` + the page path +
   the ``access_token`` query, ordered by ``page_no``, SSRF-allowlisted, and guarded
   against ``ctx.expected_pages``. A ``jxl`` page keeps its ``.jxl`` suffix (the CDN
   transcodes to JPEG on the wire; the packager names the entry from the bytes —
   261101-plw).

``fetch_image`` must send ``Origin: https://kagane.moe`` — the CDN 404s without it
(261101-plw).

No network: a fake ``SourceContext`` returns canned integrity + book-token bodies via
``post_json_body`` and records params/headers for assertions.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.kagane import KaganeSource

_INTEGRITY = "https://kagane.moe/api/integrity"
_BOOKS = "https://kagane.moe/api/v2/books"
_CACHE = "https://kstatic.to"


class _FakeCtxForManifest:
    def __init__(
        self,
        *,
        cache_url: str = _CACHE,
        access_token: str = "drmtok",
        pages: list[dict[str, Any]] | None = None,
        expected_pages: int | None = None,
        book_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._cache_url = cache_url
        self._access_token = access_token
        self._pages = (
            pages
            if pages is not None
            else [
                {"page_id": "p2", "ext": "jxl", "page_no": 2},
                {"page_id": "p1", "ext": "jxl", "page_no": 1},
            ]
        )
        self._book_overrides = book_overrides
        self.expected_pages = expected_pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json_body(
        self,
        url: str,
        *,
        body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        bucket: str = "default",
    ) -> dict[str, Any]:
        self.calls.append(
            (
                url,
                {
                    "body": body,
                    "params": params,
                    "headers": headers,
                    "bucket": bucket,
                },
            )
        )
        if url == _INTEGRITY:
            return {"token": "integ-jwt"}
        if url.startswith(f"{_BOOKS}/"):
            if self._book_overrides is not None:
                return self._book_overrides
            return {
                "access_token": self._access_token,
                "cache_url": self._cache_url,
                "manifest": {"pages": self._pages},
            }
        raise AssertionError(f"unexpected post_json_body url: {url}")

    async def get_bytes_plain_with_headers(
        self, url: str, *, extra_headers: dict[str, str] | None = None
    ) -> tuple[bytes, httpx.Headers]:
        self.calls.append((url, {"extra_headers": extra_headers}))
        return b"jpegbytes", httpx.Headers()


@pytest.mark.asyncio
async def test_fetch_manifest_three_calls_ordered_urls() -> None:
    ctx = _FakeCtxForManifest()
    urls = await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]
    # Ordered by page_no (p1 then p2 despite reversed input).
    assert urls == [
        f"{_CACHE}/api/v2/books/page/bookX/p1.jxl?token=drmtok",
        f"{_CACHE}/api/v2/books/page/bookX/p2.jxl?token=drmtok",
    ]
    # Exactly two outbound calls — page URLs are constructed, not fetched —
    # so a future unintended extra HTTP call in the happy path fails this test.
    assert len(ctx.calls) == 2
    # integrity first, then the book-token POST.
    assert ctx.calls[0][0] == _INTEGRITY
    book_url, book_call = ctx.calls[1]
    assert book_url == f"{_BOOKS}/bookX"
    assert book_call["headers"] == {"x-integrity-token": "integ-jwt"}
    assert book_call["params"] == {"intent": "Read"}


@pytest.mark.asyncio
async def test_fetch_manifest_both_token_posts_use_download_bucket() -> None:
    # 260625-rom / SRC-03: both token POSTs (integrity + book-token) must acquire the
    # SEPARATE download limiter so a download flood cannot starve interactive search.
    ctx = _FakeCtxForManifest()
    await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]
    assert len(ctx.calls) == 2
    integ_url, integ_call = ctx.calls[0]
    book_url, book_call = ctx.calls[1]
    assert integ_url == _INTEGRITY
    assert book_url == f"{_BOOKS}/bookX"
    # The proof fetch_manifest tags BOTH POSTs onto the download bucket.
    assert integ_call["bucket"] == "download"
    assert book_call["bucket"] == "download"


@pytest.mark.asyncio
async def test_fetch_manifest_empty_book_id_raises() -> None:
    ctx = _FakeCtxForManifest()
    with pytest.raises(SourceError):
        await KaganeSource().fetch_manifest("", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_no_token_raises() -> None:
    ctx = _FakeCtxForManifest(access_token="")
    with pytest.raises(SourceError):
        await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_no_pages_raises() -> None:
    ctx = _FakeCtxForManifest(
        book_overrides={
            "access_token": "t",
            "cache_url": _CACHE,
            "manifest": {"pages": []},
        }
    )
    with pytest.raises(SourceError):
        await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_cache_url_raises_ssrf() -> None:
    ctx = _FakeCtxForManifest(cache_url="https://evil.com")
    with pytest.raises(SourceError):
        await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_non_https_cache_url_raises_ssrf() -> None:
    ctx = _FakeCtxForManifest(cache_url="http://kstatic.to")
    with pytest.raises(SourceError):
        await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_subdomain_cache_url_allowed() -> None:
    ctx = _FakeCtxForManifest(cache_url="https://cdn.kagane.moe")
    urls = await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]
    assert all(u.startswith("https://cdn.kagane.moe/api/v2/books/page/") for u in urls)


@pytest.mark.asyncio
async def test_fetch_manifest_ext_preserved() -> None:
    """261101-plw: the manifest ``ext`` is kept verbatim (no jxl→jpg substitution)."""
    ctx = _FakeCtxForManifest(pages=[{"page_id": "p1", "ext": "png", "page_no": 1}])
    urls = await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]
    assert urls == [f"{_CACHE}/api/v2/books/page/bookX/p1.png?token=drmtok"]


@pytest.mark.asyncio
async def test_fetch_image_sends_origin_header() -> None:
    """261101-plw: the kstatic.to CDN 404s without ``Origin: https://kagane.moe``."""
    ctx = _FakeCtxForManifest()
    url = f"{_CACHE}/api/v2/books/page/bookX/p1.jpg?token=drmtok"
    data = await KaganeSource().fetch_image(url, ctx)  # type: ignore[arg-type]
    assert data == b"jpegbytes"
    assert ctx.calls == [
        (
            url,
            {
                "extra_headers": {
                    "Origin": "https://kagane.moe",
                    "Referer": "https://kagane.moe/",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_fetch_manifest_page_missing_id_or_ext_raises() -> None:
    ctx = _FakeCtxForManifest(pages=[{"page_no": 1, "ext": "jxl"}])  # no page_id
    with pytest.raises(SourceError):
        await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_mismatch_raises() -> None:
    ctx = _FakeCtxForManifest(expected_pages=3)  # 2 pages extracted, declares 3
    with pytest.raises(SourceError, match="integrity"):
        await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_passes_on_match() -> None:
    ctx = _FakeCtxForManifest(expected_pages=2)
    urls = await KaganeSource().fetch_manifest("bookX", ctx)  # type: ignore[arg-type]
    assert len(urls) == 2
