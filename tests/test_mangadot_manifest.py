"""Unit tests for ``MangadotSource.fetch_manifest`` — relative-url prefix + SSRF.

``GET /api/chapters/{chapter_id}/images`` returns an object
``{chapter,images:[{url,w,h}],…}`` where each ``url`` is RELATIVE + same-origin
(``/chapters/manga_5296/chapter_26/001.webp``). ``fetch_manifest``:

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
    assert ctx.calls == [f"{_BASE}/api/chapters/388872/images"]


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
