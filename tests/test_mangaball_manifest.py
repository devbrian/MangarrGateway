"""Unit tests for MangaBall ``fetch_manifest`` HTML extract + SSRF (Task 3).

The search-path manifest tail: GET ``/chapter-detail/{translation_id}/`` (HTML),
extract the absolute ``img[data-src]`` URLs in document order, SSRF-allowlist each
(scheme https + the ``/storage/.../{id}-{NNN}.jpg`` path shape — the CDN host is
read from the DOM, NEVER reconstructed, RECON §4), and guard the extracted count
against the chapter's ``pages``.

* A served HTML page with N ``img[data-src]`` tags → N absolute URLs in order,
  with hosts that DIFFER from ``base_url`` (proves no host reconstruction).
* A non-allowlisted extracted URL (wrong scheme / wrong path shape) → SourceError,
  never fetched.
* A pages≠count mismatch → SourceError (integrity guard).

No network: a fake ``SourceContext`` serves the chapter-detail HTML via get_bytes.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangaball import MangaBallSource, _is_allowed_image_url


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


def _img(host: str, translation_id: str, n: int) -> str:
    return (
        f'<img class="page" data-src='
        f'"https://{host}/storage/68515540702284f8341784c8/0/1184.1/grp/vi/'
        f'{translation_id}-{n:03d}.jpg" alt="page {n}">'
    )


def _detail_html(host: str, translation_id: str, pages: int) -> bytes:
    body = "".join(_img(host, translation_id, i + 1) for i in range(pages))
    return f"<html><body><div id='reader'>{body}</div></body></html>".encode()


# ───────────────────────────── _is_allowed_image_url ────────────────────────


def test_is_allowed_image_url_accepts_varying_cdn_hosts() -> None:
    """The CDN host varies per content — the allowlist keys on path + https,
    not a single host literal (RECON §4)."""
    base = "/storage/t/0/23/g/vi/6a1e164ac01e2cf095f75b1a-001.jpg"
    assert _is_allowed_image_url(f"https://chikorita.red-and-blue.net{base}")
    assert _is_allowed_image_url(f"https://bulbasaur.poke-black-and-white.net{base}")


def test_is_allowed_image_url_rejects_non_https() -> None:
    url = (
        "http://chikorita.red-and-blue.net/storage/t/0/23/g/vi/"
        "6a1e164ac01e2cf095f75b1a-001.jpg"
    )
    assert not _is_allowed_image_url(url)


def test_is_allowed_image_url_rejects_off_shape_path() -> None:
    # Wrong path prefix (not /storage/...).
    assert not _is_allowed_image_url("https://evil.example.net/etc/passwd")
    # Right prefix, wrong filename shape (no zero-padded index).
    assert not _is_allowed_image_url(
        "https://cdn.example.net/storage/t/0/23/g/vi/cover.jpg"
    )


def test_is_allowed_image_url_rejects_empty_host() -> None:
    assert not _is_allowed_image_url("https:///storage/t/0/23/g/vi/x-001.jpg")


def test_is_allowed_image_url_rejects_path_traversal() -> None:
    """CR-01: a raw ``/storage/..`` path matches the regex but httpx fetches the
    NORMALIZED path off-shape — the guard must reject the traversal vector."""
    # Raw path matches _MANGABALL_IMG_PATH_RE; httpx would fetch /etc/passwd-001.jpg.
    assert not _is_allowed_image_url(
        "https://cdn.example.net/storage/../../../etc/passwd-001.jpg"
    )
    # A single-segment traversal that still ends in the allowed shape after the
    # ``..`` is also rejected (normalized path escapes /storage/).
    assert not _is_allowed_image_url(
        "https://cdn.example.net/storage/a/b/../../../../x-001.jpg"
    )


def test_is_allowed_image_url_rejects_internal_metadata_hosts() -> None:
    """CR-01: the broad host regex matches ``metadata.google.internal`` and the
    like — internal/metadata namespaces must be rejected (cloud-metadata SSRF)."""
    base = "/storage/t/0/23/g/vi/6a1e164ac01e2cf095f75b1a-001.jpg"
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
    # Document order preserved (001, 002, 003).
    assert urls[0].endswith(f"{tx_id}-001.jpg")
    assert urls[1].endswith(f"{tx_id}-002.jpg")
    assert urls[2].endswith(f"{tx_id}-003.jpg")
    # The hosts come from the DOM and differ from base_url (no reconstruction).
    for url in urls:
        assert host in url
        assert "mangaball.net" not in url
    # The chapter-detail GET used the bare translation id.
    assert len(ctx.get_calls) == 1
    assert ctx.get_calls[0] == f"https://mangaball.net/chapter-detail/{tx_id}/"


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_non_allowlisted_url() -> None:
    """An extracted off-shape URL raises SourceError (no blind fetch, SSRF)."""
    tx_id = "6a1e164ac01e2cf095f75b1a"
    # A poisoned DOM: a data-src pointing at an off-shape internal host/path.
    html = (
        b"<html><body>"
        b'<img data-src="https://internal.metadata.server/latest/meta-data/">'
        b"</body></html>"
    )
    ctx = _ctx(html)
    source = MangaBallSource()
    with pytest.raises(SourceError) as excinfo:
        await source.fetch_manifest(tx_id, ctx)
    assert excinfo.value.code == "source_unavailable"


@pytest.mark.asyncio
async def test_fetch_manifest_pages_count_mismatch_raises() -> None:
    """A pages≠extracted-count mismatch raises (integrity guard)."""
    tx_id = "6a1e164ac01e2cf095f75b1a"
    ctx = _ctx(_detail_html("chikorita.red-and-blue.net", tx_id, 3))
    source = MangaBallSource()
    # The deferred path passes the promised `pages` into the tail; here we
    # exercise it directly with a deliberately-wrong count.
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
    url = "https://chikorita.red-and-blue.net/storage/t/0/23/g/vi/x-001.jpg"
    data = await source.fetch_image(url, ctx)  # type: ignore[arg-type]
    assert data == b"JPEGDATA"
    assert ctx.fetched == [url]
