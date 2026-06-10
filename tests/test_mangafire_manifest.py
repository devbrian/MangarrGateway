"""Unit tests for ``MangaFireSource.fetch_manifest`` — browser capture + SSRF + offset.

The read page is navigated in the warm browser via ``solver.fetch_via_browser`` with
``_IMAGE_LIST_EXTRACT_JS``; the extract returns ``[[url, offset], …]`` captured from
the reader's auto-fired vrf'd AJAX (D-08). ``fetch_manifest``:

* rejects a malformed (non-``/read/``) chapter id;
* pulls the solver via ``_solver_from_ctx`` (raises ``source_unavailable`` if absent);
* SSRF-allowlists EVERY captured URL (fragment stripped first) — raising on the first
  rejection, never a blind fetch (SEC-01);
* carries the scramble offset as a ``#scr_{offset}`` fragment when ``offset>0``;
* enforces the ``ctx.expected_pages`` integrity guard when set.

No network/browser: a fake solver returns a canned capture and a fake ``SourceContext``
exposes ``_solver`` + ``expected_pages``.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.mangafire import _IMAGE_LIST_EXTRACT_JS, MangaFireSource

_READ_HREF = "/read/blue-lockk.kw9j9/en/chapter-346.2"
_READ_URL = "https://mangafire.to/read/blue-lockk.kw9j9/en/chapter-346.2"
_CDN = "https://o48.mfcdn1.xyz/mf/abcdef0123/h"


class _FakeSolver:
    def __init__(self, capture: Any) -> None:
        self._capture = capture
        self.calls: list[dict[str, Any]] = []

    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — op-budget kwarg mirror
    ) -> Any:
        self.calls.append(
            {"url": url, "extract": extract, "wait_for": wait_for, "timeout": timeout}
        )
        return self._capture


class _FakeCtx:
    def __init__(self, solver: Any, *, expected_pages: int | None = None) -> None:
        self._solver = solver
        self.expected_pages = expected_pages


@pytest.mark.asyncio
async def test_fetch_manifest_captures_ssrf_allowlisted_urls_with_offset() -> None:
    capture = [
        [f"{_CDN}/0.jpg", 0],
        [f"{_CDN}/1.jpg", 3],
        [f"{_CDN}/2.webp", 0],
    ]
    solver = _FakeSolver(capture)
    ctx = _FakeCtx(solver)
    urls = await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]

    # offset==0 passes through; offset>0 carries the #scr_{offset} fragment.
    assert urls == [f"{_CDN}/0.jpg", f"{_CDN}/1.jpg#scr_3", f"{_CDN}/2.webp"]
    # The read URL + the manifest extract JS reached the browser primitive.
    assert len(solver.calls) == 1
    assert solver.calls[0]["url"] == _READ_URL
    assert solver.calls[0]["extract"] == _IMAGE_LIST_EXTRACT_JS


@pytest.mark.parametrize(
    "bad_id", ["", "blue-lockk.kw9j9", "/manga/blue-lockk.kw9j9", "/ajax/read/x"]
)
@pytest.mark.asyncio
async def test_fetch_manifest_malformed_chapter_id_raises(bad_id: str) -> None:
    ctx = _FakeCtx(_FakeSolver([[f"{_CDN}/0.jpg", 0]]))
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(bad_id, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_missing_solver_raises() -> None:
    ctx = _FakeCtx(None)
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_url_raises_ssrf() -> None:
    capture = [[f"{_CDN}/0.jpg", 0], ["https://evil.internal/mf/x/1.jpg", 0]]
    ctx = _FakeCtx(_FakeSolver(capture))
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_namespace_url_raises_ssrf() -> None:
    capture = [["https://o48.mfcdn1.xyz/other/0.jpg", 0]]
    ctx = _FakeCtx(_FakeSolver(capture))
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_empty_capture_raises() -> None:
    ctx = _FakeCtx(_FakeSolver([]))
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_mismatch_raises() -> None:
    capture = [[f"{_CDN}/0.jpg", 0], [f"{_CDN}/1.jpg", 0]]
    ctx = _FakeCtx(_FakeSolver(capture), expected_pages=3)
    with pytest.raises(SourceError, match="integrity"):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_passes_on_match() -> None:
    capture = [[f"{_CDN}/0.jpg", 0], [f"{_CDN}/1.jpg", 2]]
    ctx = _FakeCtx(_FakeSolver(capture), expected_pages=2)
    urls = await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]
    assert urls == [f"{_CDN}/0.jpg", f"{_CDN}/1.jpg#scr_2"]


@pytest.mark.asyncio
async def test_fetch_image_descrambles_only_when_offset_positive() -> None:
    """``fetch_image`` strips the #scr_ fragment, get_bytes the clean URL, and only
    descrambles (offload) when offset>0 — never fetching through the browser."""
    captured: list[str] = []

    class _ImgCtx:
        async def get_bytes(self, url: str) -> bytes:
            captured.append(url)
            return b"rawbytes"

    src = MangaFireSource()
    ctx = _ImgCtx()
    # offset==0 → passthrough (clean URL fetched, bytes unchanged).
    out0 = await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert out0 == b"rawbytes"
    assert captured[-1] == f"{_CDN}/0.jpg"
    # offset>0 → clean URL fetched (fragment stripped); descramble degrades to
    # passthrough on the non-image stub but proves the fragment was parsed off.
    out2 = await src.fetch_image(f"{_CDN}/1.jpg#scr_2", ctx)  # type: ignore[arg-type]
    assert captured[-1] == f"{_CDN}/1.jpg"
    assert out2 == b"rawbytes"
