"""Unit tests for ``ProjectSukiSource.fetch_manifest`` — read page + /callpage walk.

The manifest flow (live recon 2026-06-20):

1. ``GET /read/{bookId}/{chapterId}/1`` (HTML) → page 1's ``<img>`` in the
   ``.strip-reader`` div (absolute ``/images/gallery/{bookId}/{hash}/001?{ts}``).
2. ``POST /callpage`` (JSON body ``{bookid, chapterid, first}``) → ``{chapter_id, src,
   super}``; ``src`` is an HTML fragment of ``<img>`` tags for the remaining pages.
   LOOP while ``super`` is truthy, passing the returned ``chapter_id`` back.
3. Concatenate [page-1 url] + [all /callpage urls] in order, SSRF-allowlist EACH,
   guard the count against ``ctx.expected_pages``.

The composite ``{bookId}:{chapterId}`` chapter id is split first (malformed → raise).

No network: a fake ``SourceContext`` serves the read HTML via ``get_bytes`` and the
``/callpage`` JSON via ``post_json_body`` (a scripted queue of responses).
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.projectsuki import ProjectSukiSource

_BASE = "https://projectsuki.com"
_HASH = "a1b2c3d4ef"


def _page_url(book_id: str, n: int) -> str:
    """A real-shape page URL: extension-less ``00{n}`` filename + a ``?{ts}`` query."""
    return f"{_BASE}/images/gallery/{book_id}/{_HASH}/{n:03d}?171890{n:04d}"


def _read_html(book_id: str) -> str:
    return (
        "<html><body>"
        '<a class="navbar-brand"><img src="/static/logo.svg"></a>'
        f'<div class="strip-reader">'
        f'<img class="img-fluid center-block" src="{_page_url(book_id, 1)}" alt="p1">'
        "</div></body></html>"
    )


def _callpage_src(book_id: str, pages: list[int]) -> str:
    """An HTML fragment of ``<img>`` tags for the given page numbers."""
    return "".join(f'<img src="{_page_url(book_id, n)}">' for n in pages)


class _FakeCtxForManifest:
    """``SourceContext`` stand-in: read HTML via get_bytes + /callpage via post.

    ``callpage_responses`` is a list consumed in order — one JSON dict per POST.
    """

    def __init__(
        self,
        read_html: str,
        callpage_responses: list[dict[str, Any]],
        *,
        expected_pages: int | None = None,
    ) -> None:
        self._read_html = read_html
        self._callpage = list(callpage_responses)
        self.expected_pages = expected_pages
        self.get_calls: list[str] = []
        self.post_calls: list[dict[str, Any]] = []

    async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
        self.get_calls.append(url)
        return self._read_html.encode()

    async def post_json_body(self, url: str, *, body: dict[str, Any]) -> dict[str, Any]:
        self.post_calls.append(body)
        if not self._callpage:
            raise AssertionError("unexpected extra /callpage POST")
        return self._callpage.pop(0)


@pytest.mark.asyncio
async def test_fetch_manifest_single_callpage_iteration() -> None:
    """A 4-page chapter: read page (001) + one /callpage with 002–004 (super falsy)."""
    ctx = _FakeCtxForManifest(
        _read_html("180716"),
        [{"chapter_id": "ch-1", "src": _callpage_src("180716", [2, 3, 4])}],
    )
    urls = await ProjectSukiSource().fetch_manifest("180716:ch-1", ctx)  # type: ignore[arg-type]
    assert urls == [_page_url("180716", n) for n in (1, 2, 3, 4)]
    # One read GET, exactly one /callpage POST (super was falsy → no loop).
    assert ctx.get_calls == [f"{_BASE}/read/180716/ch-1/1"]
    assert len(ctx.post_calls) == 1
    assert ctx.post_calls[0] == {
        "bookid": "180716",
        "chapterid": "ch-1",
        "first": True,
    }


@pytest.mark.asyncio
async def test_fetch_manifest_multi_iteration_super_loop() -> None:
    """``super:true`` walks /callpage again with the returned chapter_id (loop)."""
    ctx = _FakeCtxForManifest(
        _read_html("9"),
        [
            {"chapter_id": "next-1", "src": _callpage_src("9", [2, 3]), "super": True},
            {"chapter_id": "next-2", "src": _callpage_src("9", [4, 5]), "super": False},
        ],
    )
    urls = await ProjectSukiSource().fetch_manifest("9:ch-7", ctx)  # type: ignore[arg-type]
    assert urls == [_page_url("9", n) for n in (1, 2, 3, 4, 5)]
    # Two POSTs: first carries the real chapterid, the second the returned chapter_id.
    assert len(ctx.post_calls) == 2
    assert ctx.post_calls[0] == {"bookid": "9", "chapterid": "ch-7", "first": True}
    assert ctx.post_calls[1] == {"bookid": "9", "chapterid": "next-1", "first": False}


@pytest.mark.parametrize("bad_id", ["ch-1", ":ch-1", "180716:", "", ":"])
@pytest.mark.asyncio
async def test_fetch_manifest_malformed_composite_raises(bad_id: str) -> None:
    ctx = _FakeCtxForManifest(_read_html("1"), [{"src": _callpage_src("1", [2])}])
    with pytest.raises(SourceError):
        await ProjectSukiSource().fetch_manifest(bad_id, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_url_raises_ssrf() -> None:
    poisoned = (
        f'<img src="{_page_url("1", 2)}">'
        '<img src="https://evil.com/images/gallery/1/a1b2c3/3">'
    )
    ctx = _FakeCtxForManifest(_read_html("1"), [{"chapter_id": "c", "src": poisoned}])
    with pytest.raises(SourceError) as excinfo:
        await ProjectSukiSource().fetch_manifest("1:ch-1", ctx)  # type: ignore[arg-type]
    assert excinfo.value.code == "source_unavailable"
    assert "evil.com" in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_manifest_accepts_extension_less_wide_padding() -> None:
    """Page 10 is ``0010`` (no extension) — the SSRF allowlist must accept it."""
    src = (
        f'<img src="{_BASE}/images/gallery/1/{_HASH}/0010">'
        f'<img src="{_BASE}/images/gallery/1/{_HASH}/0011">'
    )
    ctx = _FakeCtxForManifest(_read_html("1"), [{"chapter_id": "c", "src": src}])
    urls = await ProjectSukiSource().fetch_manifest("1:ch-1", ctx)  # type: ignore[arg-type]
    # page 1 from the read page + 0010 + 0011 from /callpage.
    assert urls[1].endswith("/0010")
    assert urls[2].endswith("/0011")


@pytest.mark.asyncio
async def test_fetch_manifest_no_pages_raises() -> None:
    ctx = _FakeCtxForManifest(
        "<html><body>no reader</body></html>",
        [{"chapter_id": "c", "src": ""}],
    )
    with pytest.raises(SourceError):
        await ProjectSukiSource().fetch_manifest("1:ch-1", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_mismatch_raises() -> None:
    ctx = _FakeCtxForManifest(
        _read_html("1"),
        [{"chapter_id": "c", "src": _callpage_src("1", [2])}],  # 2 pages total
        expected_pages=3,
    )
    with pytest.raises(SourceError, match="integrity"):
        await ProjectSukiSource().fetch_manifest("1:ch-1", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_match_passes() -> None:
    ctx = _FakeCtxForManifest(
        _read_html("1"),
        [{"chapter_id": "c", "src": _callpage_src("1", [2, 3])}],  # 3 pages total
        expected_pages=3,
    )
    urls = await ProjectSukiSource().fetch_manifest("1:ch-1", ctx)  # type: ignore[arg-type]
    assert len(urls) == 3


@pytest.mark.asyncio
async def test_fetch_image_delegates_to_get_bytes() -> None:
    class _ImgCtx:
        def __init__(self) -> None:
            self.fetched: list[str] = []

        async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
            self.fetched.append(url)
            return b"JPEGDATA"

    ctx = _ImgCtx()
    url = _page_url("1", 1)
    data = await ProjectSukiSource().fetch_image(url, ctx)  # type: ignore[arg-type]
    assert data == b"JPEGDATA"
    assert ctx.fetched == [url]
