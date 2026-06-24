"""Unit tests for ``MangaFireSource.fetch_manifest`` — browserless httpx, SSRF, offset.

The manifest is derived in PURE PYTHON over httpx (issue #314, no browser): two signed
reader AJAX calls, each signed in-process with ``mangafire_vrf.compute_vrf`` over the
``@``-joined path identifiers (the SAME pipeline PR #313 cracked for search):

  1. ``GET /ajax/read/{slug}/chapter/{lang}?vrf=compute_vrf("{slug}@chapter@{lang}")``
     → reader chapter list HTML whose ``<a data-id data-number>`` rows map the read
     href's ``chapter-{number}`` to its reader ``itemId``;
  2. ``GET /ajax/read/chapter/{itemId}?vrf=compute_vrf("chapter@{itemId}")``
     → ``{"result":{"images":[[url, _, offset], …]}}`` (server-minted page URLs).

``fetch_manifest`` then:

* rejects a malformed (non-``/read/``) chapter id;
* SSRF-allowlists EVERY URL (fragment stripped first) — raising on the first rejection,
  never a blind fetch (SEC-01);
* carries the scramble offset (entry index 2) as a ``#scr_{offset}`` fragment when
  ``offset>0``;
* enforces the ``ctx.expected_pages`` integrity guard when set.

No network/browser: a fake ``SourceContext`` answers ``get_json`` for the two endpoints
from canned bodies and records the signed vrf each call carried.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.mangafire import (
    _MANIFEST_EMPTY_RETRIES,
    _PREFERRED_ZONE_ATTR,
    MangaFireSource,
    _rewrite_zone,
    _zone_of,
)
from manga_gateway.sources.mangafire_vrf import compute_vrf

_READ_HREF = "/read/blue-lockk.kw9j9/en/chapter-346.2"
_SLUG_ID = "kw9j9"
_LANG = "en"
_NUMBER = "346.2"
_ITEM_ID = "7389265"
_CDN = "https://o48.mfcdn1.xyz/mf/abcdef0123/h"


def _reader_list_html(rows: dict[str, str]) -> str:
    """Build a reader chapter-list ``result.html`` fragment from number→itemId rows."""
    base = f"/read/blue-lockk.{_SLUG_ID}/{_LANG}"
    items = "".join(
        f'<li class="item"><a data-id="{item_id}" data-number="{number}" '
        f'href="{base}/chapter-{number}">Ch {number}</a></li>'
        for number, item_id in rows.items()
    )
    return f'<ul class="scroll-sm">{items}</ul>'


class _FakeCtx:
    """Answers ``get_json`` for the two reader endpoints from canned bodies (#314).

    ``list_body`` is returned for the reader chapter-list endpoint; ``image_bodies`` is
    a list consumed in order for the image-list endpoint (the last item is reused once
    exhausted, so a steady-state can be pinned). Each item is a body dict to RETURN or
    an ``Exception`` to RAISE. Records the ``vrf`` query param each call carried so the
    signing can be asserted.
    """

    def __init__(
        self,
        *,
        list_body: Any,
        image_bodies: list[Any],
        expected_pages: int | None = None,
    ) -> None:
        self._list_body = list_body
        self._image_bodies = image_bodies
        self.expected_pages = expected_pages
        self.list_calls: list[dict[str, Any]] = []
        self.image_calls: list[dict[str, Any]] = []

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        if "/ajax/read/chapter/" in url:
            self.image_calls.append({"url": url, **params})
            idx = min(len(self.image_calls) - 1, len(self._image_bodies) - 1)
            body = self._image_bodies[idx]
            if isinstance(body, Exception):
                raise body
            return body
        if "/ajax/read/" in url and "/chapter/" in url:
            self.list_calls.append({"url": url, **params})
            if isinstance(self._list_body, Exception):
                raise self._list_body
            return self._list_body
        raise AssertionError(f"unexpected get_json url {url!r}")


def _ctx(
    *,
    images: list[Any] | None = None,
    image_bodies: list[Any] | None = None,
    rows: dict[str, str] | None = None,
    list_body: Any = None,
    expected_pages: int | None = None,
) -> _FakeCtx:
    if list_body is None:
        list_body = {"result": {"html": _reader_list_html(rows or {_NUMBER: _ITEM_ID})}}
    if image_bodies is None:
        image_bodies = [{"result": {"images": images if images is not None else []}}]
    return _FakeCtx(
        list_body=list_body, image_bodies=image_bodies, expected_pages=expected_pages
    )


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the manifest empty-list retry backoff a no-op so tests never sleep."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("manga_gateway.sources.mangafire.asyncio.sleep", _instant)


@pytest.mark.asyncio
async def test_fetch_manifest_signs_both_calls_and_resolves_item_id() -> None:
    """Happy path: the reader list resolves the itemId; both AJAX calls carry the vrf
    signed over the ``@``-joined identifiers, and the image URLs come back with their
    scramble offset (entry index 2) carried as a ``#scr_`` fragment."""
    images = [
        [f"{_CDN}/0.jpg", 2, 0],
        [f"{_CDN}/1.jpg", 1, 3],
        [f"{_CDN}/2.webp", 2, 0],
    ]
    ctx = _ctx(images=images)
    urls = await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]

    assert urls == [f"{_CDN}/0.jpg", f"{_CDN}/1.jpg#scr_3", f"{_CDN}/2.webp"]
    # Reader list: signed over "{slug}@chapter@{lang}" and pointed at the slug endpoint.
    assert len(ctx.list_calls) == 1
    assert ctx.list_calls[0]["url"].endswith(f"/ajax/read/{_SLUG_ID}/chapter/{_LANG}")
    assert ctx.list_calls[0]["vrf"] == compute_vrf(f"{_SLUG_ID}@chapter@{_LANG}")
    # Image list: the resolved itemId is in the path, signed over "chapter@{itemId}".
    assert len(ctx.image_calls) == 1
    assert ctx.image_calls[0]["url"].endswith(f"/ajax/read/chapter/{_ITEM_ID}")
    assert ctx.image_calls[0]["vrf"] == compute_vrf(f"chapter@{_ITEM_ID}")


@pytest.mark.parametrize(
    "bad_id",
    ["", "blue-lockk.kw9j9", "/manga/blue-lockk.kw9j9", "/read/x", "/read/x/en/foo-1"],
)
@pytest.mark.asyncio
async def test_fetch_manifest_malformed_chapter_id_raises(bad_id: str) -> None:
    ctx = _ctx(images=[[f"{_CDN}/0.jpg", 2, 0]])
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(bad_id, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_unknown_chapter_number_raises() -> None:
    """The reader list has no row matching the href's chapter number → no itemId."""
    ctx = _ctx(rows={"999": "111"}, images=[[f"{_CDN}/0.jpg", 2, 0]])
    with pytest.raises(SourceError, match="item-id"):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]
    # Never reached the image endpoint without an itemId.
    assert ctx.image_calls == []


@pytest.mark.asyncio
async def test_fetch_manifest_unreadable_reader_list_raises() -> None:
    ctx = _ctx(list_body={"result": None}, images=[[f"{_CDN}/0.jpg", 2, 0]])
    with pytest.raises(SourceError, match="unreadable"):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_url_raises_ssrf() -> None:
    images = [[f"{_CDN}/0.jpg", 2, 0], ["https://evil.internal/mf/x/1.jpg", 2, 0]]
    ctx = _ctx(images=images)
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_namespace_url_raises_ssrf() -> None:
    ctx = _ctx(images=[["https://o48.mfcdn1.xyz/other/0.jpg", 2, 0]])
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_empty_image_list_raises(_no_sleep: None) -> None:
    ctx = _ctx(images=[])
    with pytest.raises(SourceError, match="empty mangafire image list"):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]
    # initial attempt + the bounded extra retries were all spent.
    assert len(ctx.image_calls) == _MANIFEST_EMPTY_RETRIES + 1


@pytest.mark.parametrize(
    "bad_body",
    [
        {"result": None},  # non-dict result
        {"result": {}},  # missing images key
        {"result": {"images": "nope"}},  # non-list images
        {"not": "a result envelope"},  # missing result
    ],
)
@pytest.mark.asyncio
async def test_fetch_manifest_malformed_image_list_fails_fast(
    _no_sleep: None, bad_body: Any
) -> None:
    """A malformed image-list envelope fails fast (no retry budget burned) with a
    distinct 'malformed' error — NOT the misleading 'empty image list' (CodeRabbit
    #319). Only an actual empty list is retryable."""
    ctx = _ctx(image_bodies=[bad_body])
    with pytest.raises(SourceError, match="malformed mangafire image list"):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]
    # Failed on the FIRST attempt — the empty-list retry budget was not spent.
    assert len(ctx.image_calls) == 1


@pytest.mark.asyncio
async def test_fetch_manifest_retries_empty_then_succeeds(_no_sleep: None) -> None:
    """An empty image list on the first try is re-fetched; the retry's list succeeds."""
    valid = {"result": {"images": [[f"{_CDN}/0.jpg", 2, 0], [f"{_CDN}/1.jpg", 1, 3]]}}
    ctx = _ctx(image_bodies=[{"result": {"images": []}}, valid])
    urls = await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]
    assert urls == [f"{_CDN}/0.jpg", f"{_CDN}/1.jpg#scr_3"]
    assert len(ctx.image_calls) == 2  # one retry was spent


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_mismatch_raises() -> None:
    images = [[f"{_CDN}/0.jpg", 2, 0], [f"{_CDN}/1.jpg", 2, 0]]
    ctx = _ctx(images=images, expected_pages=3)
    with pytest.raises(SourceError, match="integrity"):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]
    # A genuine integrity mismatch is a real data problem — NOT retried.
    assert len(ctx.image_calls) == 1


@pytest.mark.asyncio
async def test_fetch_manifest_integrity_guard_passes_on_match() -> None:
    images = [[f"{_CDN}/0.jpg", 2, 0], [f"{_CDN}/1.jpg", 1, 2]]
    ctx = _ctx(images=images, expected_pages=2)
    urls = await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]
    assert urls == [f"{_CDN}/0.jpg", f"{_CDN}/1.jpg#scr_2"]


@pytest.mark.asyncio
async def test_fetch_manifest_malformed_image_entry_raises() -> None:
    ctx = _ctx(images=[[f"{_CDN}/0.jpg", 2, 0], []])
    with pytest.raises(SourceError):
        await MangaFireSource().fetch_manifest(_READ_HREF, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_image_descrambles_only_when_offset_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fetch_image`` strips the #scr_ fragment, get_bytes the clean URL, and only
    descrambles (offload) when offset>0 — never fetching through the browser."""
    captured: list[str] = []
    descramble_calls: list[tuple[bytes, int]] = []

    def _fake_descramble(content: bytes, offset: int) -> bytes:
        descramble_calls.append((content, offset))
        return b"descrambled"

    # Stub the real Pillow descramble so the offset>0 branch returns a
    # DISTINGUISHABLE value — otherwise both branches return ``b"rawbytes"`` and
    # the test would pass whether or not descrambling actually ran.
    monkeypatch.setattr(
        "manga_gateway.sources.mangafire._descramble_image", _fake_descramble
    )

    class _ImgCtx:
        async def get_bytes(self, url: str) -> bytes:
            captured.append(url)
            return b"rawbytes"

    src = MangaFireSource()
    ctx = _ImgCtx()
    # offset==0 → passthrough (clean URL fetched, bytes unchanged, NO descramble).
    out0 = await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert out0 == b"rawbytes"
    assert captured[-1] == f"{_CDN}/0.jpg"
    assert descramble_calls == []
    # offset>0 → clean URL fetched (fragment stripped) AND descramble invoked with
    # the parsed offset on the fetched bytes.
    out2 = await src.fetch_image(f"{_CDN}/1.jpg#scr_2", ctx)  # type: ignore[arg-type]
    assert captured[-1] == f"{_CDN}/1.jpg"
    assert out2 == b"descrambled"
    assert descramble_calls == [(b"rawbytes", 2)]


# ───────────────── adaptive CDN zone-retry (fetch_image, #281 debug) ──────────────
# A MangaFire page 403 is a Cloudflare WAF deny of the gateway egress IP on a CDN zone
# (``mfcdnN``), not a stale signed URL. ``fetch_image`` retries the IDENTICAL signed
# path across the other known zones, remembers the winning zone per-job, and raises a
# CLEAR terminal error only when EVERY zone 403s.

_ZONE403 = SourceError("source_unavailable", "upstream 403", status=403)


class _ZoneCtx:
    """A per-job ctx whose ``get_bytes`` 403s some zones and 200s others.

    ``ok_zones`` lists the ``mfcdnN`` zone numbers that return bytes; every other zone
    raises a 403 ``SourceError`` — mirroring the live WAF split (mfcdn1/2 → 403,
    mfcdn3 → 200). Records the order of fetched URLs so the test can assert the
    remembered-zone-first behavior. Plain class (no __slots__) so the source can stash
    its per-job preferred-zone sidecar attribute on it, exactly like the real context.
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
async def test_fetch_image_retries_other_zone_on_403() -> None:
    """The original (blocked) zone 403s; the same signed path 200s on another zone."""
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones={3})  # mfcdn1 (the URL's zone) blocked, mfcdn3 ok
    out = await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert out == b"zone-bytes"
    # The blocked mfcdn1 was tried first (the URL's own zone), then a rewrite to an
    # un-blocked zone returned bytes — same path, only the host zone label changed.
    assert ctx.calls[0] == f"{_CDN}/0.jpg"
    assert _zone_of(ctx.calls[-1]) == 3
    assert "/mf/abcdef0123/h/0.jpg" in ctx.calls[-1]
    # The winning zone is remembered on the per-job context.
    assert getattr(ctx, _PREFERRED_ZONE_ATTR) == 3


@pytest.mark.asyncio
async def test_fetch_image_remembers_winning_zone_for_subsequent_pages() -> None:
    """After one page finds the good zone, the next page tries that zone FIRST and
    never re-probes the blocked original zone again (no wasted 403 per page)."""
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones={3})
    await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    ctx.calls.clear()
    await src.fetch_image(f"{_CDN}/1.jpg", ctx)  # type: ignore[arg-type]
    # The second page's FIRST attempt is the remembered good zone — no mfcdn1 probe.
    assert _zone_of(ctx.calls[0]) == 3
    assert all(_zone_of(u) != 1 for u in ctx.calls)


@pytest.mark.asyncio
async def test_fetch_image_descrambles_after_zone_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offset>0 page still descrambles when the bytes came from a switched zone."""
    descramble_calls: list[tuple[bytes, int]] = []

    def _fake_descramble(content: bytes, offset: int) -> bytes:
        descramble_calls.append((content, offset))
        return b"descrambled"

    monkeypatch.setattr(
        "manga_gateway.sources.mangafire._descramble_image", _fake_descramble
    )
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones={3})
    out = await src.fetch_image(f"{_CDN}/1.jpg#scr_4", ctx)  # type: ignore[arg-type]
    # Bytes came from mfcdn3, but the offset un-shuffle still ran on those bytes.
    assert out == b"descrambled"
    assert descramble_calls == [(b"zone-bytes", 4)]
    assert _zone_of(ctx.calls[-1]) == 3


@pytest.mark.asyncio
async def test_fetch_image_all_zones_blocked_raises_clear_terminal_error() -> None:
    """When EVERY known zone 403s, raise a CLEAR terminal error naming the host — NOT
    the old stale-manifest wording — so the engine fails fast and correctly."""
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones=set())  # all zones blocked
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
    # Exactly one fetch — no zone rewrites were attempted on a non-403.
    assert len(ctx.calls) == 1


@pytest.mark.asyncio
async def test_fetch_image_skips_zone_candidate_failing_ssrf_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-01 defensive: a rewritten zone URL that somehow fails the SSRF allowlist is
    skipped (never fetched). Here the allowlist rejects mfcdn2 (the rewrite target),
    forcing the retry to skip it and fall through to mfcdn3."""

    def _allow(url: str) -> bool:
        # Reject only the mfcdn2 rewrite; everything else passes.
        return _zone_of(url) != 2

    monkeypatch.setattr("manga_gateway.sources.mangafire._is_allowed_image_url", _allow)
    src = MangaFireSource()
    ctx = _ZoneCtx(ok_zones={2, 3})  # mfcdn2 WOULD answer, but the allowlist blocks it
    out = await src.fetch_image(f"{_CDN}/0.jpg", ctx)  # type: ignore[arg-type]
    assert out == b"zone-bytes"
    # mfcdn2 was never fetched (rejected pre-fetch); the bytes came from mfcdn3.
    assert all(_zone_of(u) != 2 for u in ctx.calls)
    assert _zone_of(ctx.calls[-1]) == 3


def test_zone_of_and_rewrite_zone_round_trip() -> None:
    """Pure helpers: parse the zone, rewrite it leaving prefix/path/signature intact."""
    url = f"{_CDN}/0.jpg"
    assert _zone_of(url) == 1
    rewritten = _rewrite_zone(url, 3)
    assert rewritten == "https://o48.mfcdn3.xyz/mf/abcdef0123/h/0.jpg"
    assert _zone_of(rewritten) == 3
    # A non-zoned host is returned unchanged and reports no zone.
    other = "https://cdn.example.com/mf/x/0.jpg"
    assert _zone_of(other) is None
    assert _rewrite_zone(other, 3) == other
