"""Unit tests for ``MangaFireSource.fetch_manifest`` / ``fetch_image`` — JSON API.

The manifest is a single plain httpx GET (260706-hgu, no browser, no vrf):
``GET /api/chapters/{chapterId}`` → ``{"data":{"pages":[{"url","width","height"}, …]}}``
— server-minted ``mfcdnN.xyz`` CDN URLs with NO scramble offset. ``fetch_manifest``:

* SSRF-allowlists EVERY page url (fragment stripped first) — raising on the first
  rejection, never a blind fetch (SEC-01);
* returns the urls CLEAN (no fragment — the new API carries no offset);
* enforces the ``ctx.expected_pages`` integrity guard when set;
* raises on a missing/empty ``data.pages``.

``fetch_image`` fetches the clean url through the adaptive CDN zone-retry (the
``mfcdnN`` WAF-block self-heal) — no descramble anymore.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.mangafire import (
    _PREFERRED_ZONE_ATTR,
    MangaFireSource,
    _rewrite_zone,
    _zone_of,
)

_CHAPTER_ID = "4736538"
_CDN = "https://o48.mfcdn1.xyz/mf/abcdef0123/h"


class _FakeCtx:
    """Answers ``get_json`` for ``/api/chapters/{id}`` from a canned body."""

    def __init__(self, *, body: Any, expected_pages: int | None = None) -> None:
        self._body = body
        self.expected_pages = expected_pages
        self.calls: list[str] = []

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append(url)
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _pages_body(pages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"id": _CHAPTER_ID, "number": 700, "pages": pages}}


def _page(url: str) -> dict[str, Any]:
    return {"url": url, "width": 1066, "height": 1600}


@pytest.mark.asyncio
async def test_fetch_manifest_returns_clean_allowlisted_urls() -> None:
    body = _pages_body([_page(f"{_CDN}/0.jpg"), _page(f"{_CDN}/1.webp")])
    ctx = _FakeCtx(body=body)
    urls = await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]
    assert urls == [f"{_CDN}/0.jpg", f"{_CDN}/1.webp"]
    # No URL fragment survives from the new API (there is no scramble offset).
    assert all("#" not in u for u in urls)
    # The manifest is a single GET at /api/chapters/{id}.
    assert len(ctx.calls) == 1
    assert ctx.calls[0].endswith(f"/api/chapters/{_CHAPTER_ID}")


@pytest.mark.asyncio
async def test_fetch_manifest_strips_fragment_before_allowlist() -> None:
    body = _pages_body([_page(f"{_CDN}/0.jpg#anything")])
    ctx = _FakeCtx(body=body)
    urls = await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]
    assert urls == [f"{_CDN}/0.jpg"]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_url_raises_ssrf() -> None:
    body = _pages_body(
        [_page(f"{_CDN}/0.jpg"), _page("https://evil.internal/mf/x/1.jpg")]
    )
    ctx = _FakeCtx(body=body)
    with pytest.raises(SourceError, match="SSRF allowlist"):
        await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_namespace_url_raises_ssrf() -> None:
    ctx = _FakeCtx(body=_pages_body([_page("https://o48.mfcdn1.xyz/other/0.jpg")]))
    with pytest.raises(SourceError, match="SSRF allowlist"):
        await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_page_missing_url_raises() -> None:
    ctx = _FakeCtx(body=_pages_body([{"width": 1, "height": 2}]))
    with pytest.raises(SourceError, match="missing url"):
        await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_body",
    [
        {"data": None},
        {"data": {}},
        {"data": {"pages": []}},
        {"data": {"pages": "nope"}},
        {"not": "a data envelope"},
    ],
)
@pytest.mark.asyncio
async def test_fetch_manifest_missing_pages_raises(bad_body: Any) -> None:
    ctx = _FakeCtx(body=bad_body)
    with pytest.raises(SourceError, match="no pages"):
        await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_mismatch_raises() -> None:
    body = _pages_body([_page(f"{_CDN}/0.jpg"), _page(f"{_CDN}/1.jpg")])
    ctx = _FakeCtx(body=body, expected_pages=3)
    with pytest.raises(SourceError, match="integrity"):
        await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_passes_on_match() -> None:
    body = _pages_body([_page(f"{_CDN}/0.jpg"), _page(f"{_CDN}/1.jpg")])
    ctx = _FakeCtx(body=body, expected_pages=2)
    urls = await MangaFireSource().fetch_manifest(_CHAPTER_ID, ctx)  # type: ignore[arg-type]
    assert urls == [f"{_CDN}/0.jpg", f"{_CDN}/1.jpg"]


# ───────────────── adaptive CDN zone-retry (fetch_image, #281 debug) ──────────────
# A MangaFire page 403 is a Cloudflare WAF deny of the gateway egress IP on a CDN zone
# (``mfcdnN``), not a stale signed URL. ``fetch_image`` retries the IDENTICAL page path
# across the other known zones, remembers the winning zone per-job, and raises a CLEAR
# terminal error only when EVERY zone 403s.

_ZONE403 = SourceError("source_unavailable", "upstream 403", status=403)


class _ZoneCtx:
    """A per-job ctx whose ``get_bytes`` 403s some zones and 200s others.

    ``ok_zones`` lists the ``mfcdnN`` zone numbers that return bytes; every other zone
    raises a 403 ``SourceError`` — mirroring the live WAF split (mfcdn1/2 → 403,
    mfcdn3 → 200). Records the order of fetched URLs. Plain class (no __slots__) so the
    source can stash its per-job preferred-zone sidecar attribute on it.
    """

    def __init__(self, ok_zones: set[int]) -> None:
        self._ok = ok_zones
        self.calls: list[str] = []

    async def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        zone = _zone_of(url)
        if zone in self._ok:
            return b"zone-bytes"
        raise _ZONE403


@pytest.mark.asyncio
async def test_fetch_image_returns_clean_bytes_no_descramble() -> None:
    """``fetch_image`` strips any fragment, get_bytes the clean URL, returns bytes as-is
    (the new API carries no scramble offset)."""
    captured: list[str] = []

    class _ImgCtx:
        async def get_bytes(self, url: str) -> bytes:
            captured.append(url)
            return b"rawbytes"

    src = MangaFireSource()
    ctx = _ImgCtx()
    out = await src.fetch_image(f"{_CDN}/0.jpg#anything", ctx)  # type: ignore[arg-type]
    assert out == b"rawbytes"
    # Fragment stripped before fetch.
    assert captured[-1] == f"{_CDN}/0.jpg"


@pytest.mark.asyncio
async def test_fetch_image_retries_other_zone_on_403() -> None:
    """The original (blocked) zone 403s; the same path 200s on another zone."""
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones={3})  # mfcdn1 (the URL's zone) blocked, mfcdn3 ok
    out = await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert out == b"zone-bytes"
    assert ctx.calls[0] == f"{_CDN}/0.jpg"
    assert _zone_of(ctx.calls[-1]) == 3
    assert "/mf/abcdef0123/h/0.jpg" in ctx.calls[-1]
    assert getattr(ctx, _PREFERRED_ZONE_ATTR) == 3


@pytest.mark.asyncio
async def test_fetch_image_remembers_winning_zone_for_subsequent_pages() -> None:
    """After one page finds the good zone, the next page tries that zone FIRST and
    never re-probes the blocked original zone again."""
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones={3})
    await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    ctx.calls.clear()
    await src.fetch_image(f"{_CDN}/1.jpg", ctx)  # type: ignore[arg-type]
    assert _zone_of(ctx.calls[0]) == 3
    assert all(_zone_of(u) != 1 for u in ctx.calls)


@pytest.mark.asyncio
async def test_fetch_image_all_zones_blocked_raises_clear_terminal_error() -> None:
    """When EVERY known zone 403s, raise a CLEAR terminal error naming the host."""
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones=set())
    with pytest.raises(SourceError) as exc:
        await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert exc.value.status == 403
    msg = str(exc.value)
    assert "all mangafire CDN zones blocked" in msg
    assert "o48.mfcdn1.xyz" in msg
    assert "stale manifest" not in msg


@pytest.mark.asyncio
async def test_fetch_image_non_403_error_propagates_without_zone_retry() -> None:
    """A non-403 page failure (a genuine loss) propagates as-is — NOT zone-retried."""

    class _Ctx:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_bytes(self, url: str) -> bytes:
            self.calls.append(url)
            raise SourceError("source_unavailable", "upstream 500", status=500)

    src = MangaFireSource()
    ctx = _Ctx()
    with pytest.raises(SourceError) as exc:
        await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert exc.value.status == 500
    assert len(ctx.calls) == 1


@pytest.mark.asyncio
async def test_fetch_image_skips_zone_candidate_failing_ssrf_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-01 defensive: a rewritten zone URL that somehow fails the SSRF allowlist is
    skipped (never fetched)."""

    def _allow(url: str) -> bool:
        return _zone_of(url) != 2

    monkeypatch.setattr("manga_gateway.sources.mangafire._is_allowed_image_url", _allow)
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones={2, 3})  # mfcdn2 WOULD answer, but the allowlist blocks it
    out = await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert out == b"zone-bytes"
    assert all(_zone_of(u) != 2 for u in ctx.calls)
    assert _zone_of(ctx.calls[-1]) == 3


def test_zone_of_and_rewrite_zone_round_trip() -> None:
    """Pure helpers: parse the zone, rewrite it leaving prefix/path intact."""
    url = f"{_CDN}/0.jpg"
    assert _zone_of(url) == 1
    rewritten = _rewrite_zone(url, 3)
    assert rewritten == "https://o48.mfcdn3.xyz/mf/abcdef0123/h/0.jpg"
    assert _zone_of(rewritten) == 3
    other = "https://cdn.example.com/mf/x/0.jpg"
    assert _zone_of(other) is None
    assert _rewrite_zone(other, 3) == other
