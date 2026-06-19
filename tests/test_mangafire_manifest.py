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
from manga_gateway.sources.mangafire import (
    _IMAGE_LIST_EXTRACT_JS,
    _PREFERRED_ZONE_ATTR,
    MangaFireSource,
    _rewrite_zone,
    _zone_of,
)

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
