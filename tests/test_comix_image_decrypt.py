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

from typing import Any

import httpx
import pytest

from manga_gateway.sources.comix import ComixSource

# Spike-012 offline regression vector (page 04 — x-enc-seed header + raw bytes;
# plaintext is the deterministic WebP header: RIFF + filesize-8 LE + WEBP + …).
_VECTOR_SEED = 1415945509
_VECTOR_CIPHER = bytes.fromhex("22cc645f74d999ede717e03564f6eac4764f4f91")
_VECTOR_PLAIN = bytes.fromhex("52494646124a15005745425056503820064a1500")


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
