"""Unit tests for ``MangadotSource.fetch_manifest`` — namespace routing + url + SSRF.

The manifest endpoint has TWO namespaces (debug mangadot-resolve-404):
``GET /api/uploads/{id}/images`` for ``source=="user"`` chapters and
``GET /api/chapters/{id}/images`` for scraped chapters. Both return an object
``{chapter,manga,images:[{url,w,h}],…}`` where each ``url`` is RELATIVE + same-origin
(``/chapters/manga_43/chapter_83_g17423/001.webp``). The source is packed into the
``chapter_id`` argument as ``{id}|{source}`` (a bare id → scraped, back-compat).
``fetch_manifest``:

* routes to the uploads vs chapters endpoint from the packed source;
* prepends ``base_url`` to each relative url and returns them in array order;
* SSRF-allowlists EVERY url (same-origin ``/chapters/`` + image ext) BEFORE return;
* raises ``SourceError`` on empty ``images`` OR any off-allowlist url.

No network: a fake ``SourceContext`` returns the canned manifest object via
``get_json``.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.mangadot import MangadotSource

_BASE = "https://mangadot.net"


class _FakeCtxForManifest:
    """``SourceContext`` stand-in: returns a canned manifest object via ``get_json``."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.calls: list[str] = []

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append(url)
        return self._body


@pytest.mark.asyncio
async def test_fetch_manifest_prefixes_relative_urls_in_order() -> None:
    body = {
        "chapter": {"id": 388872, "page_count": 3},
        "images": [
            {"url": "/chapters/manga_5296/chapter_26/001.webp", "w": 0, "h": 0},
            {"url": "/chapters/manga_5296/chapter_26/002.webp", "w": 800, "h": 1200},
            {"url": "/chapters/manga_5296/chapter_26/003.jpg", "w": 0, "h": 0},
        ],
    }
    ctx = _FakeCtxForManifest(body)
    urls = await MangadotSource().fetch_manifest("388872", ctx)  # type: ignore[arg-type]

    assert urls == [
        f"{_BASE}/chapters/manga_5296/chapter_26/001.webp",
        f"{_BASE}/chapters/manga_5296/chapter_26/002.webp",
        f"{_BASE}/chapters/manga_5296/chapter_26/003.jpg",
    ]
    # Bare id (no packed source) → the scraped /api/chapters namespace (back-compat).
    assert ctx.calls == [f"{_BASE}/api/chapters/388872/images"]


@pytest.mark.asyncio
async def test_fetch_manifest_user_source_routes_to_uploads() -> None:
    # debug mangadot-resolve-404: a "user"-source chapter resolves on the
    # /api/uploads/{id}/images namespace, NOT /api/chapters (whose overlapping id
    # space would 404 or return a DIFFERENT manga's chapter).
    body = {"images": [{"url": "/chapters/manga_43/chapter_83_g17423/001.webp"}]}
    ctx = _FakeCtxForManifest(body)
    urls = await MangadotSource().fetch_manifest("4458|user", ctx)  # type: ignore[arg-type]

    assert urls == [f"{_BASE}/chapters/manga_43/chapter_83_g17423/001.webp"]
    assert ctx.calls == [f"{_BASE}/api/uploads/4458/images"]


@pytest.mark.asyncio
async def test_fetch_manifest_non_user_source_routes_to_chapters() -> None:
    # A non-"user" packed source (scraped) keeps the /api/chapters namespace.
    body = {"images": [{"url": "/chapters/manga_43/chapter_83/001.webp"}]}
    ctx = _FakeCtxForManifest(body)
    urls = await MangadotSource().fetch_manifest("388872|scraped", ctx)  # type: ignore[arg-type]

    assert urls == [f"{_BASE}/chapters/manga_43/chapter_83/001.webp"]
    assert ctx.calls == [f"{_BASE}/api/chapters/388872/images"]


@pytest.mark.parametrize(
    ("chapter_id", "source", "expected"),
    [
        ("4458", "user", "4458|user"),
        ("388872", "scraped", "388872|scraped"),
        ("388872", None, "388872"),  # falsy source → BARE id (scraped/back-compat)
        ("388872", "", "388872"),  # blank source → BARE id
        ("4458", "  user  ", "4458|user"),  # trimmed
    ],
)
def test_resolve_id_round_trips(chapter_id: str, source: Any, expected: str) -> None:
    # _build_resolve_id ↔ _parse_resolve_id inverse; a bare id parses to source=None
    # so older handles + scraped rows keep hitting /api/chapters (debug
    # mangadot-resolve-404 back-compat guarantee).
    built = MangadotSource._build_resolve_id(chapter_id, source)
    assert built == expected
    raw_id, parsed_source = MangadotSource._parse_resolve_id(built)
    assert raw_id == chapter_id
    assert parsed_source == ((str(source).strip() or None) if source else None)


@pytest.mark.asyncio
async def test_fetch_manifest_empty_images_raises() -> None:
    ctx = _FakeCtxForManifest({"chapter": {}, "images": []})
    with pytest.raises(SourceError):
        await MangadotSource().fetch_manifest("388872", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_missing_images_key_raises() -> None:
    ctx = _FakeCtxForManifest({"chapter": {}})
    with pytest.raises(SourceError):
        await MangadotSource().fetch_manifest("388872", ctx)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_images", ["/chapters/m/c/001.webp", {"0": "x"}, 5])
@pytest.mark.asyncio
async def test_fetch_manifest_non_list_images_raises(bad_images: Any) -> None:
    # WR-01: a non-list ``images`` (string/dict/number) must raise a clear shape
    # error, NOT iterate wrongly into the misleading "no page images found" path.
    ctx = _FakeCtxForManifest({"chapter": {}, "images": bad_images})
    with pytest.raises(SourceError, match="not a list"):
        await MangadotSource().fetch_manifest("388872", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_url_raises_ssrf() -> None:
    # An absolute off-host url (e.g. a poisoned response) must fail the allowlist.
    body = {
        "images": [
            {"url": "/chapters/manga_5296/chapter_26/001.webp"},
            {"url": "https://evil.com/chapters/x/y/002.webp"},
        ]
    }
    ctx = _FakeCtxForManifest(body)
    with pytest.raises(SourceError):
        await MangadotSource().fetch_manifest("388872", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_namespace_url_raises_ssrf() -> None:
    # A relative url outside the /chapters/ namespace must fail the allowlist.
    body = {"images": [{"url": "/covers/manga_5296.webp"}]}
    ctx = _FakeCtxForManifest(body)
    with pytest.raises(SourceError):
        await MangadotSource().fetch_manifest("388872", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_skips_blank_and_non_dict_entries() -> None:
    body = {
        "images": [
            {"url": "/chapters/m/c/001.webp"},
            {"url": "  "},  # blank → skipped
            "not-a-dict",  # non-dict → skipped
            {"url": "/chapters/m/c/002.webp"},
        ]
    }
    ctx = _FakeCtxForManifest(body)
    urls = await MangadotSource().fetch_manifest("388872", ctx)  # type: ignore[arg-type]
    assert urls == [
        f"{_BASE}/chapters/m/c/001.webp",
        f"{_BASE}/chapters/m/c/002.webp",
    ]
