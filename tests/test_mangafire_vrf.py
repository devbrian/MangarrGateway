"""Unit tests for the MangaFire ``vrf`` signer (261101-r4c) — no network, no real chunk.

``STUB_CHUNK`` stands in for the site's obfuscator-VM polyfill chunk: it publishes a
transparent ``getProtectionToken`` that echoes ``navigator.appCodeName`` (proving the
shim loaded) plus its args, and ends with the same trailing ``export{...};`` the real
chunk carries (proving the strip). ``STUB_HTML`` / ``stub_bootstrap_bytes`` are shared
with the other mangafire test fakes so they can answer the homepage + chunk fetches.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.mangafire import MangaFireSource
from manga_gateway.sources.mangafire_vrf import VrfSigner, find_chunk_url

_CHUNK_URL = "https://s.mfcdn.nl/build/mf/assets/polyfill-tlfroa-Bq-bHCd3.js"
STUB_HTML = (
    "<html><head>"
    f'<link rel="modulepreload" as="script" href="{_CHUNK_URL}" />'
    "</head><body></body></html>"
)
STUB_CHUNK = (
    "globalThis.getProtectionToken = (p, q) => "
    "navigator.appCodeName + '|' + p + '|' + JSON.stringify(q);\n"
    "export{a as b};"
)


def stub_bootstrap_bytes(url: str) -> bytes:
    """Homepage → ``STUB_HTML``; any polyfill chunk URL → ``STUB_CHUNK``."""
    if url == "https://mangafire.to/":
        return STUB_HTML.encode()
    if "/polyfill-" in url:
        return STUB_CHUNK.encode()
    raise AssertionError(f"unexpected get_bytes {url}")


def test_find_chunk_url_real_link_tag() -> None:
    assert find_chunk_url(STUB_HTML) == _CHUNK_URL


def test_find_chunk_url_absent() -> None:
    assert find_chunk_url("<html><script src='/app.js'></script></html>") is None


def test_find_chunk_url_foreign_host_rejected() -> None:
    html = STUB_HTML.replace("s.mfcdn.nl", "evil.example")
    assert find_chunk_url(html) is None


def test_stub_signer_strips_export_and_loads_shim() -> None:
    token = VrfSigner(STUB_CHUNK).sign("/titles", {"keyword": "x", "limit": 3})
    assert token == 'Mozilla|/titles|{"keyword":"x","limit":3}'


@pytest.mark.parametrize(
    "chunk",
    ["throw new Error('boom');", "var notTheSigner = 1;"],
)
def test_bad_chunk_raises_source_unavailable(chunk: str) -> None:
    with pytest.raises(SourceError) as ei:
        VrfSigner(chunk)
    assert ei.value.code == "source_unavailable"


class _SignCtx:
    """Records chunk fetches + every ``get_json``; replays a scripted outcome list
    (the last outcome repeats)."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.chunk_fetches = 0
        self.get_json_calls: list[tuple[str, dict[str, Any]]] = []

    async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
        if "/polyfill-" in url:
            self.chunk_fetches += 1
        return stub_bootstrap_bytes(url)

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        self.get_json_calls.append((url, params))
        out = self._outcomes[min(len(self.get_json_calls), len(self._outcomes)) - 1]
        if isinstance(out, Exception):
            raise out
        return dict(out)


def _403() -> SourceError:
    return SourceError("source_unavailable", "upstream 403", status=403)


@pytest.mark.asyncio
async def test_403_rebuilds_once_and_retries() -> None:
    ctx = _SignCtx([_403(), {"items": []}])
    src = MangaFireSource()
    body = await src._signed_get_json("/titles", {"keyword": "x"}, ctx)  # type: ignore[arg-type]
    assert body == {"items": []}
    assert ctx.chunk_fetches == 2
    assert len(ctx.get_json_calls) == 2
    for url, params in ctx.get_json_calls:
        assert url == "https://mangafire.to/api/titles"
        assert params["keyword"] == "x"
        assert params["vrf"]


@pytest.mark.asyncio
async def test_always_403_propagates_after_one_rebuild() -> None:
    ctx = _SignCtx([_403()])
    src = MangaFireSource()
    with pytest.raises(SourceError) as ei:
        await src._signed_get_json("/titles", {"keyword": "x"}, ctx)  # type: ignore[arg-type]
    assert ei.value.status == 403
    assert len(ctx.get_json_calls) == 2
    assert ctx.chunk_fetches == 2


@pytest.mark.asyncio
async def test_non_403_propagates_without_rebuild() -> None:
    ctx = _SignCtx([SourceError("source_unavailable", "upstream 500", status=500)])
    src = MangaFireSource()
    with pytest.raises(SourceError) as ei:
        await src._signed_get_json("/titles", {"keyword": "x"}, ctx)  # type: ignore[arg-type]
    assert ei.value.status == 500
    assert len(ctx.get_json_calls) == 1
    assert ctx.chunk_fetches == 1
