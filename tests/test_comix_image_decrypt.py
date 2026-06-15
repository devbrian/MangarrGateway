"""Offline regression for Comix's static page-image decode (spike-012-verified).

No network, no browser: a tiny fake ``SourceContext`` stand-in serves the page
bytes + the ``x-enc-seed``/``x-enc-len`` response headers via
``get_bytes_plain_with_headers`` (the only ctx method ``fetch_image`` calls).

The cipher is the spike-012-verified static 32-bit LCG keystream XORed over the
first ``x-enc-len`` bytes of every 4th page image. The embedded vector is the
spike's captured (ciphertext, seed, plaintext) triple — its plaintext is a
deterministic WebP header (RIFF … WEBP). If Comix ever rotates the LCG constants,
``test_decode_enc_prefix_matches_offline_vector`` fails first (the canary).
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from PIL import Image

from manga_gateway.framework.errors import SourceError
from manga_gateway.sources.comix import (
    ComixSource,
    _scramble_permutation,
    _unscramble_image,
)

# Spike-012 offline regression vector (page 04 — x-enc-seed header + raw bytes;
# plaintext is the deterministic WebP header: RIFF + filesize-8 LE + WEBP + …).
_VECTOR_SEED = 1415945509
_VECTOR_CIPHER = bytes.fromhex("22cc645f74d999ede717e03564f6eac4764f4f91")
_VECTOR_PLAIN = bytes.fromhex("52494646124a15005745425056503820064a1500")

# Spike-017 atomic algo-2 vector (page 12 — x-enc-algo: 2; xorshift32(seed|1) low-byte
# keystream). Captured live 2026-06-14; plaintext is the simple-VP8 WebP header.
_ALGO2_SEED = 135981641
_ALGO2_CIPHER = bytes.fromhex("b2d0c027ac6bf230d718db59965d2373a874b38c")
_ALGO2_PLAIN = bytes.fromhex("524946463c7b02005745425056503820307b0200")


class _FakeCtx:
    """``SourceContext`` stand-in serving fixed bytes + headers to ``fetch_image``."""

    def __init__(self, data: bytes, headers: dict[str, str]) -> None:
        self._data = data
        self._headers = httpx.Headers(headers)

    async def get_bytes_plain_with_headers(
        self, url: str
    ) -> tuple[bytes, httpx.Headers]:
        return self._data, self._headers


def test_decode_enc_prefix_matches_offline_vector() -> None:
    out = ComixSource._decode_enc_prefix(_VECTOR_CIPHER, _VECTOR_SEED, 4096)
    assert out[: len(_VECTOR_PLAIN)] == _VECTOR_PLAIN
    # Canary: the decoded prefix is a decodable WebP header.
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WEBP"


def test_decode_enc_prefix_seed_zero_passthrough() -> None:
    assert ComixSource._decode_enc_prefix(b"\x00\x01\x02", 0, 4096) == b"\x00\x01\x02"


@pytest.mark.asyncio
async def test_fetch_image_decodes_when_seed_nonzero() -> None:
    ctx: Any = _FakeCtx(
        _VECTOR_CIPHER, {"x-enc-seed": "1415945509", "x-enc-len": "4096"}
    )
    out = await ComixSource().fetch_image("https://cdn.example.store/si/T/04.webp", ctx)
    assert out[: len(_VECTOR_PLAIN)] == _VECTOR_PLAIN
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WEBP"


@pytest.mark.asyncio
async def test_fetch_image_passthrough_seed_zero_and_missing() -> None:
    payload = b"\x52\x49\x46\x46\x10\x00\x00\x00WEBPVP8 "  # plaintext WebP-shaped page

    src = ComixSource()

    # seed == 0 → untouched
    ctx_zero: Any = _FakeCtx(payload, {"x-enc-seed": "0"})
    out_zero = await src.fetch_image("https://cdn.example.store/si/T/01.webp", ctx_zero)
    assert out_zero == payload

    # header absent → untouched
    ctx_missing: Any = _FakeCtx(payload, {})
    out_missing = await src.fetch_image(
        "https://cdn.example.store/si/T/02.webp", ctx_missing
    )
    assert out_missing == payload

    # non-numeric header → fails safe to passthrough
    ctx_garbage: Any = _FakeCtx(payload, {"x-enc-seed": "garbage"})
    out_garbage = await src.fetch_image(
        "https://cdn.example.store/si/T/03.webp", ctx_garbage
    )
    assert out_garbage == payload

    # signed/negative seed → fails safe to passthrough (must NOT decode-corrupt a
    # plaintext page; `int("-1") & 0xFFFFFFFF` would otherwise be a live nonzero seed)
    ctx_neg_seed: Any = _FakeCtx(payload, {"x-enc-seed": "-1", "x-enc-len": "4096"})
    out_neg_seed = await src.fetch_image(
        "https://cdn.example.store/si/T/04.webp", ctx_neg_seed
    )
    assert out_neg_seed == payload


@pytest.mark.asyncio
async def test_fetch_image_negative_enc_len_falls_back_to_default() -> None:
    """A hostile negative ``x-enc-len`` must NOT leave the page as ciphertext.

    ``range(min(-1, …))`` is empty, so a naive parse would skip the XOR loop and
    return undecodable bytes (a `page N invalid`). The decode must fall back to the
    default length and still recover the WebP.
    """
    ctx: Any = _FakeCtx(_VECTOR_CIPHER, {"x-enc-seed": "1415945509", "x-enc-len": "-1"})
    out = await ComixSource().fetch_image("https://cdn.example.store/si/T/08.webp", ctx)
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WEBP"


def test_enc_header_int_rejects_signed_and_nondecimal() -> None:
    f = ComixSource._enc_header_int
    assert f(None) == 0
    assert f("") == 0
    assert f("  ") == 0
    assert f("-1") == 0  # signed → malformed
    assert f("+1") == 0  # signed → malformed
    assert f("0x10") == 0  # non-decimal → malformed
    assert f("4096") == 4096
    assert f(" 4096 ") == 4096  # surrounding whitespace tolerated
    assert f("3333521747") == 3333521747  # large unsigned 32-bit seed admitted
    assert f("4294967295") == 0xFFFFFFFF  # max 32-bit seed admitted (boundary)
    assert f("4294967296") == 0  # > 32 bits → malformed (would alias under the mask)
    assert f("99999999999999") == 0  # far over → malformed
    assert f("9" * 5000) == 0  # over-long digit run → 0, must NOT raise ValueError


# ───────────────────────── spike-017: byte-algo-2 (xorshift) ──────────────────────────


def test_decode_enc_prefix_algo2_matches_atomic_vector() -> None:
    """Canary: ``x-enc-algo: 2`` xorshift cipher, bit-exact vs the live page-12 capture.

    If Comix rotates the xorshift constants this fails first (like the algo-1 canary).
    """
    out = ComixSource._decode_enc_prefix(_ALGO2_CIPHER, _ALGO2_SEED, 4096, algo=2)
    assert out[: len(_ALGO2_PLAIN)] == _ALGO2_PLAIN
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WEBP"
    assert out[12:16] == b"VP8 "


def test_decode_enc_prefix_default_algo_is_legacy_lcg() -> None:
    """The 3-arg call path stays algo-1 (byte-identical to PR #170 / spike 012)."""
    assert ComixSource._decode_enc_prefix(
        _VECTOR_CIPHER, _VECTOR_SEED, 4096
    ) == ComixSource._decode_enc_prefix(_VECTOR_CIPHER, _VECTOR_SEED, 4096, algo=1)


def test_decode_enc_prefix_unknown_algo_raises() -> None:
    with pytest.raises(ValueError, match="unknown x-enc-algo"):
        ComixSource._decode_enc_prefix(_ALGO2_CIPHER, _ALGO2_SEED, 4096, algo=4)


@pytest.mark.asyncio
async def test_fetch_image_decodes_algo2_byte_page() -> None:
    ctx: Any = _FakeCtx(
        _ALGO2_CIPHER,
        {"x-enc-seed": str(_ALGO2_SEED), "x-enc-len": "4096", "x-enc-algo": "2"},
    )
    out = await ComixSource().fetch_image("https://cdn.example.store/i4/T/12.webp", ctx)
    assert out[: len(_ALGO2_PLAIN)] == _ALGO2_PLAIN
    assert out[12:16] == b"VP8 "


@pytest.mark.asyncio
async def test_fetch_image_unknown_enc_algo_fails_loud() -> None:
    ctx: Any = _FakeCtx(
        _ALGO2_CIPHER,
        {"x-enc-seed": str(_ALGO2_SEED), "x-enc-len": "4096", "x-enc-algo": "9"},
    )
    with pytest.raises(SourceError):
        await ComixSource().fetch_image("https://cdn.example.store/i4/T/12.webp", ctx)


# ───────────────────────── spike-017: tile scramble ──────────────────────────


def _make_grid_image(cols: int, rows: int, tile: int = 16) -> Image.Image:
    """An RGB image whose every tile is a UNIQUE solid color → exact-recovery oracle."""
    img = Image.new("RGB", (cols * tile, rows * tile))
    for idx in range(cols * rows):
        color = ((idx * 37) % 256, (idx * 91) % 256, (idx * 53 + 7) % 256)
        x, y = (idx % cols) * tile, (idx // cols) * tile
        img.paste(color, (x, y, x + tile, y + tile))
    return img


def _scramble_to_webp(
    img: Image.Image, seed: int, cols: int, rows: int, algo: int
) -> bytes:
    """Forward-scramble: EXACT inverse of ``_unscramble_image`` (scrambled cell i holds
    original tile ``perm[i]``), encoded lossless WebP."""
    perm = _scramble_permutation(seed, cols * rows, algo)
    tw, th = img.width // cols, img.height // rows
    scr = img.copy()
    for s_idx in range(cols * rows):
        src = perm[s_idx]
        sx, sy = (src % cols) * tw, (src // cols) * th
        dx, dy = (s_idx % cols) * tw, (s_idx // cols) * th
        scr.paste(img.crop((sx, sy, sx + tw, sy + th)), (dx, dy))
    buf = io.BytesIO()
    scr.save(buf, format="WEBP", lossless=True, quality=100, method=6)
    return buf.getvalue()


@pytest.mark.parametrize("algo", [1, 2, 3])
def test_unscramble_round_trip_recovers_original(algo: int) -> None:
    cols, rows, seed = 5, 5, 4010719162
    original = _make_grid_image(cols, rows)
    scrambled = _scramble_to_webp(original, seed, cols, rows, algo)
    # a non-trivial permutation actually moved tiles
    assert scrambled != original.tobytes()
    restored_bytes = _unscramble_image(scrambled, seed, cols, rows, algo)
    restored = Image.open(io.BytesIO(restored_bytes)).convert("RGB")
    assert restored.tobytes() == original.tobytes()


def test_unscramble_unknown_algo_raises() -> None:
    img = _make_grid_image(5, 5)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    with pytest.raises(ValueError, match="unknown x-scramble-algo"):
        _unscramble_image(buf.getvalue(), 123, 5, 5, algo=7)


def test_unscramble_non_image_passthrough() -> None:
    junk = b"not a webp at all"
    assert _unscramble_image(junk, 123, 5, 5, algo=3) == junk


@pytest.mark.asyncio
async def test_fetch_image_unscrambles_scramble_page() -> None:
    cols, rows, seed = 5, 5, 4167260734
    original = _make_grid_image(cols, rows)
    scrambled = _scramble_to_webp(original, seed, cols, rows, 3)
    ctx: Any = _FakeCtx(
        scrambled,
        {
            "x-scramble-seed": str(seed),
            "x-scramble-grid": "5x5",
            "x-scramble-algo": "3",
        },
    )
    out = await ComixSource().fetch_image("https://cdn.example.store/i4/T/04.webp", ctx)
    restored = Image.open(io.BytesIO(out)).convert("RGB")
    assert restored.tobytes() == original.tobytes()


@pytest.mark.asyncio
async def test_fetch_image_unknown_scramble_algo_fails_loud() -> None:
    img = _make_grid_image(5, 5)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    ctx: Any = _FakeCtx(
        buf.getvalue(),
        {"x-scramble-seed": "123", "x-scramble-grid": "5x5", "x-scramble-algo": "8"},
    )
    with pytest.raises(SourceError):
        await ComixSource().fetch_image("https://cdn.example.store/i4/T/04.webp", ctx)


def test_scramble_grid_parser() -> None:
    g = ComixSource._scramble_grid
    assert g(None) == (5, 5)
    assert g("") == (5, 5)
    assert g("5x5") == (5, 5)
    assert g("5X5") == (5, 5)
    assert g("3,4") == (3, 4)
    assert g("6") == (6, 6)  # single dimension → square
    assert g("garbage") == (5, 5)
    assert g("999x999") == (64, 64)  # clamped to _SCRAMBLE_GRID_MAX


def test_algo_header_parser() -> None:
    h = ComixSource._algo_header
    assert h(None, 1) == 1  # missing → default
    assert h("", 1) == 1  # blank → default
    assert h("2", 1) == 2  # present → verbatim (dispatch resolves / fails loud)
    assert h("garbage", 1) == -1  # malformed → guaranteed-unknown (fails loud)
