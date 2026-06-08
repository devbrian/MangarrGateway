"""Unit tests for ``AtsumaruSource.fetch_manifest`` — composite id + SSRF allowlist.

``GET /api/read/chapter?mangaId=&chapterId=`` returns
``{readChapter:{pages:[{image:"/static/pages/{scan}/{chapter}/{N}.webp",…}]}}`` —
RELATIVE, same-origin image paths. ``fetch_manifest``:

* splits the COMPOSITE ``{mangaId}:{chapterId}`` chapter id (``read/chapter`` needs
  both query params) and passes BOTH to the GET;
* prepends ``base_url`` to each relative ``image`` path, in array order;
* SSRF-allowlists EVERY url (same-origin ``atsu.moe`` + ``/static/`` + image ext)
  BEFORE return — raising ``SourceError`` on an off-host / off-namespace url;
* raises on empty/missing ``pages`` and on a malformed composite id;
* enforces the ``ctx.expected_pages`` integrity guard when set.

No network: a fake ``SourceContext`` returns the canned read-chapter object via
``get_json`` and records the params for assertions.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.atsumaru import AtsumaruSource

_BASE = "https://atsu.moe"
_READ = f"{_BASE}/api/read/chapter"


class _FakeCtxForManifest:
    """``SourceContext`` stand-in: a canned read-chapter body via ``get_json``."""

    def __init__(
        self, body: dict[str, Any], *, expected_pages: int | None = None
    ) -> None:
        self._body = body
        self.expected_pages = expected_pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.calls.append((url, params))
        return self._body


def _read_body(images: list[str]) -> dict[str, Any]:
    return {
        "readChapter": {
            "id": "gGfRS",
            "title": "Chapter 1184",
            "scanlationMangaId": "cmgz",
            "pages": [
                {"id": f"gGfRS-{i}", "image": img, "number": i}
                for i, img in enumerate(images)
            ],
        }
    }


@pytest.mark.asyncio
async def test_fetch_manifest_splits_composite_and_prefixes_urls_in_order() -> None:
    body = _read_body(
        [
            "/static/pages/cmgz/gGfRS/0.webp",
            "/static/pages/cmgz/gGfRS/1.webp",
            "/static/pages/cmgz/gGfRS/2.webp",
        ]
    )
    ctx = _FakeCtxForManifest(body)
    urls = await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]

    assert urls == [
        f"{_BASE}/static/pages/cmgz/gGfRS/0.webp",
        f"{_BASE}/static/pages/cmgz/gGfRS/1.webp",
        f"{_BASE}/static/pages/cmgz/gGfRS/2.webp",
    ]
    # BOTH ids reach read/chapter — mangaId is Zod-required (value not re-validated).
    assert len(ctx.calls) == 1
    url, params = ctx.calls[0]
    assert url == _READ
    assert params == {"mangaId": "sVC2A", "chapterId": "gGfRS"}


@pytest.mark.parametrize("bad_id", ["gGfRS", ":gGfRS", "sVC2A:", "", ":"])
@pytest.mark.asyncio
async def test_fetch_manifest_malformed_composite_raises(bad_id: str) -> None:
    ctx = _FakeCtxForManifest(_read_body(["/static/pages/c/g/0.webp"]))
    with pytest.raises(SourceError):
        await AtsumaruSource().fetch_manifest(bad_id, ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_empty_pages_raises() -> None:
    ctx = _FakeCtxForManifest({"readChapter": {"pages": []}})
    with pytest.raises(SourceError):
        await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_missing_readchapter_raises() -> None:
    ctx = _FakeCtxForManifest({})
    with pytest.raises(SourceError):
        await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_host_url_raises_ssrf() -> None:
    # A poisoned absolute off-host image must fail the allowlist.
    body = _read_body(
        ["/static/pages/c/g/0.webp", "https://evil.com/static/pages/c/g/1.webp"]
    )
    ctx = _FakeCtxForManifest(body)
    with pytest.raises(SourceError):
        await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_off_namespace_url_raises_ssrf() -> None:
    # A same-origin path OUTSIDE the /static/ namespace must fail the allowlist.
    ctx = _FakeCtxForManifest(_read_body(["/api/secret/0.webp"]))
    with pytest.raises(SourceError):
        await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_traversal_url_raises_ssrf() -> None:
    # A traversal path that would normalize out of /static/ must fail.
    ctx = _FakeCtxForManifest(_read_body(["/static/../api/secret.webp"]))
    with pytest.raises(SourceError):
        await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_pages_integrity_guard() -> None:
    ctx = _FakeCtxForManifest(
        _read_body(["/static/pages/c/g/0.webp", "/static/pages/c/g/1.webp"]),
        expected_pages=3,  # declares 3 but only 2 extracted → integrity failure
    )
    with pytest.raises(SourceError, match="integrity"):
        await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_manifest_pages_integrity_passes_on_match() -> None:
    ctx = _FakeCtxForManifest(
        _read_body(["/static/pages/c/g/0.webp", "/static/pages/c/g/1.webp"]),
        expected_pages=2,
    )
    urls = await AtsumaruSource().fetch_manifest("sVC2A:gGfRS", ctx)  # type: ignore[arg-type]
    assert len(urls) == 2
