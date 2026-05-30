"""Framework decrypt-registry tests (04-01 seam, async since 04-04 D-45).

Covers the scheme-dispatch decrypt seam — the placeholder stdlib XOR ``comix-v1``
from 04-03 has been REPLACED by a browser-evaluated coroutine that delegates to
the warm Patchright solver (D-45 — the live cipher is a jsdefender VM stream cipher
not statically reversible in v1). The dispatch shape:

* ``decrypt(None, body, {})`` is a verbatim identity pass-through (MangaDex /
  non-encrypted sources — D-39); the dispatcher is async, but the None path is a
  zero-cost passthrough.
* ``register_scheme("x")(fn)`` registers ``fn`` (sync OR async) and
  ``decrypt("x", body, cfg)`` dispatches to it (awaiting if a coroutine).
* ``decrypt(<unknown>, ...)`` raises a clear ``KeyError`` — never a silent
  pass-through that would leak ciphertext toward CBZ packaging.
* ``comix-v1`` requires ``decrypt_config["solver"]`` (D-45); a missing solver raises
  :class:`DecryptError`.

The D-44 real-ciphertext fixture (``tests/fixtures/comix/chapter_9001596.bin``) is
exercised against a FakeSolver — proving the seam wiring; the live smoke commits
the ``.plain`` companion that will pin the byte-exact decrypted JSON.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from manga_gateway.framework import decrypt as decrypt_mod
from manga_gateway.framework.decrypt import DecryptError, decrypt, register_scheme


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


# ─────────────────────── comix-v1 (D-45 browser-evaluated) ───────────────────


_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "comix"
_REAL_CIPHERTEXT = _FIXTURE_DIR / "chapter_9001596.bin"


class _FakeSolver:
    """Captures the ciphertext handed to ``decrypt`` and returns canned plaintext."""

    def __init__(self, plaintext: bytes) -> None:
        self.plaintext = plaintext
        self.received: list[bytes] = []

    async def decrypt(self, ciphertext: bytes) -> bytes:
        self.received.append(ciphertext)
        return self.plaintext


def test_comix_v1_scheme_is_registered() -> None:
    assert "comix-v1" in decrypt_mod._SCHEMES


@pytest.mark.asyncio
async def test_comix_v1_requires_solver_in_config() -> None:
    """D-45: comix-v1 delegates to ``solver.decrypt`` — a missing solver is a
    framework wiring bug, not silently ignored."""
    with pytest.raises(DecryptError):
        await decrypt("comix-v1", b"cipher", {})


@pytest.mark.asyncio
async def test_comix_v1_delegates_to_solver_decrypt() -> None:
    fake = _FakeSolver(plaintext=b'{"pages":["a","b"]}')
    out = await decrypt("comix-v1", b"ciphertext", {"solver": fake})
    assert out == b'{"pages":["a","b"]}'
    assert fake.received == [b"ciphertext"]


@pytest.mark.asyncio
async def test_comix_v1_decrypt_real_ciphertext() -> None:
    """D-44: feed the committed real ciphertext envelope through the comix-v1 seam
    via a FakeSolver. Proves the seam wiring is correct; the live smoke commits a
    ``.plain`` companion in a follow-up that pins the byte-exact decrypted JSON.

    The stub plaintext mirrors the 12 image URLs observed in the rendered DOM
    after decryption (CONTEXT.md live_recon) so this test also documents the
    expected plaintext shape the live smoke will verify against.
    """
    ciphertext = _REAL_CIPHERTEXT.read_bytes()
    assert ciphertext  # fixture committed
    observed_urls = [
        f"https://jloo.wowpic3.store/si/bEqPbYfoPT0GmlnlAkqfoBJE4oENYsQ/{n:02d}.webp"
        for n in (1, 2, 4, 5, 7, 8, 10, 11, 13, 14, 16, 17)
    ]
    import json as _json

    stub_plain = _json.dumps({"pages": observed_urls}).encode("utf-8")
    fake = _FakeSolver(plaintext=stub_plain)
    out = await decrypt("comix-v1", ciphertext, {"solver": fake})
    assert out == stub_plain
    # The solver received the ciphertext envelope verbatim — no pre-processing.
    assert fake.received == [ciphertext]


# ─────────── plaintext-fixture regression: do NOT route plaintext through decrypt


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
