"""Framework decrypt-registry tests (04-01 seam, async since 04-04).

Covers the scheme-dispatch decrypt seam:

* ``decrypt(None, body, {})`` is a verbatim identity pass-through (MangaDex /
  non-encrypted sources — D-39); the dispatcher is async, but the None path is a
  zero-cost passthrough.
* ``register_scheme("x")(fn)`` registers ``fn`` (sync OR async) and
  ``decrypt("x", body, cfg)`` dispatches to it (awaiting if a coroutine).
* ``decrypt(<unknown>, ...)`` raises a clear ``KeyError`` — never a silent
  pass-through that would leak ciphertext toward CBZ packaging.

The Comix browser-evaluated cipher (``comix-v1``, D-45) was removed in issue
#46 — no live source registers a scheme today, so these tests exercise the
seam shape via synthetic ``echo`` / ``aecho`` schemes that future sources will
follow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from manga_gateway.framework import decrypt as decrypt_mod
from manga_gateway.framework.decrypt import decrypt, register_scheme


@pytest.fixture(autouse=True)
def _isolate_registry() -> AsyncIterator[None]:
    """Snapshot/restore the module registry so locally-registered test schemes
    never leak across tests."""
    saved = dict(decrypt_mod._SCHEMES)
    try:
        yield
    finally:
        decrypt_mod._SCHEMES.clear()
        decrypt_mod._SCHEMES.update(saved)


# ─────────────────────────── core dispatch (async) ───────────────────────────


@pytest.mark.asyncio
async def test_none_scheme_is_verbatim_identity_passthrough() -> None:
    body = b"\x00\x01plain-bytes\xff"
    # None scheme returns the SAME object unchanged (MangaDex pass-through, D-39).
    assert await decrypt(None, body, {}) is body


@pytest.mark.asyncio
async def test_registered_sync_scheme_dispatches() -> None:
    @register_scheme("echo")
    def _echo(body: bytes, config: dict[str, object]) -> bytes:
        return body + b"-decrypted"

    assert await decrypt("echo", b"cipher", {}) == b"cipher-decrypted"


@pytest.mark.asyncio
async def test_registered_async_scheme_dispatches() -> None:
    @register_scheme("aecho")
    async def _aecho(body: bytes, config: dict[str, object]) -> bytes:
        return body + b"-async"

    assert await decrypt("aecho", b"cipher", {}) == b"cipher-async"


@pytest.mark.asyncio
async def test_registered_scheme_receives_config() -> None:
    seen: dict[str, object] = {}

    @register_scheme("withcfg")
    def _withcfg(body: bytes, config: dict[str, object]) -> bytes:
        seen.update(config)
        return body

    await decrypt("withcfg", b"x", {"key": "abc"})
    assert seen == {"key": "abc"}


@pytest.mark.asyncio
async def test_unknown_scheme_raises_not_silent_passthrough() -> None:
    # A wrong/garbage scheme must fail loudly, never leak ciphertext downstream.
    with pytest.raises(KeyError):
        await decrypt("nope", b"cipher", {})


# ─────────── plaintext-fixture regression: do NOT route plaintext through decrypt


_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "comix"
_CHAPTER_INDEXES = _FIXTURE_DIR / "manga_mr3m0_chapter_indexes.json"
_SEARCH_FIXTURE = _FIXTURE_DIR / "search_the_forgotten_field.json"


@pytest.mark.asyncio
async def test_plaintext_chapter_indexes_not_routed_through_decrypt() -> None:
    """The chapter-indexes endpoint is PLAINTEXT (live recon). Proves that running
    ``decrypt(None, ...)`` on the committed plaintext returns the SAME bytes — i.e.
    when the source uses ``get_json_plain`` (scheme=None at that callsite), the
    plaintext flows through unchanged. Doubles as a regression that the
    ``manga_mr3m0_chapter_indexes.json`` shape stays parseable."""
    import json as _json

    plain = _CHAPTER_INDEXES.read_bytes()
    assert plain
    # Identity pass-through (scheme=None) — proves we don't accidentally decrypt.
    assert await decrypt(None, plain, {}) is plain
    data = _json.loads(plain)
    assert data["status"] == "ok"
    items = data["result"]["items"]
    assert items, "chapter-indexes fixture should have entries"
    first = items[0]
    assert "number" in first
    assert "groups" in first and isinstance(first["groups"], list)


def test_search_fixture_shape_pins_hid_mapping() -> None:
    """Pins the real search-API result shape (D-46): ``result.items[].hid`` is the
    canonical 5-char series slug ComixSource maps to ``source_id`` (NOT the numeric
    ``id``). Regression against future shape drift."""
    import json as _json

    data = _json.loads(_SEARCH_FIXTURE.read_bytes())
    assert data["status"] == "ok"
    items = data["result"]["items"]
    assert items, "search fixture should have items"
    first = items[0]
    # The exact item from the live recon (The Forgotten Field, hid=mr3m0).
    assert first["hid"] == "mr3m0"
    assert first["title"] == "The Forgotten Field"
    # numeric id is distinct from hid — the source must NOT confuse the two (D-46).
    assert first["id"] != first["hid"]
    # Fields ComixSource reads off result.items[].
    assert "latestChapter" in first
    assert "url" in first and first["url"].startswith("/title/")
    assert "poster" in first and "large" in first["poster"]
