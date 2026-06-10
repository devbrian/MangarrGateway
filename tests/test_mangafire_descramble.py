"""Unit tests for ``mangafire._descramble_image`` — geometric piece-shuffle (D-12).

Port of Keiyoushi ``ImageInterceptor.kt``: for ``offset==0`` the bytes pass through
byte-for-byte (the page is not scrambled); for ``offset>0`` the 200px-piece grid is
geometrically un-shuffled with the last row/column left in place. The round-trip is
proved by building a SCRAMBLED image with the INVERSE of the descramble mapping and
asserting ``_descramble_image`` reconstructs the original pixels exactly (PNG re-encode
is lossless, so a full-image pixel compare is valid). No recompression beyond the
descramble re-encode.
"""

from __future__ import annotations

import io

from PIL import Image

from manga_gateway.sources.mangafire import (
    MIN_SPLIT_COUNT,
    PIECE_SIZE,
    _ceil_div,
    _descramble_image,
)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_original(piece_w: int, piece_h: int, x_max: int, y_max: int) -> Image.Image:
    """A grid of solid-color pieces — each (x,y) block carries a distinct color."""
    width = piece_w * (x_max + 1)
    height = piece_h * (y_max + 1)
    img = Image.new("RGB", (width, height))
    for x in range(x_max + 1):
        for y in range(y_max + 1):
            color = ((x * 37) % 256, (y * 53) % 256, (x * 7 + y * 11) % 256)
            block = Image.new("RGB", (piece_w, piece_h), color)
            img.paste(block, (piece_w * x, piece_h * y))
    return img


def _scramble(original: Image.Image, offset: int) -> Image.Image:
    """Apply the INVERSE of the descramble mapping → a scrambled image S such that
    ``_descramble_image(S, offset)`` reconstructs ``original``.
    """
    width, height = original.size
    piece_w = min(PIECE_SIZE, _ceil_div(width, MIN_SPLIT_COUNT))
    piece_h = min(PIECE_SIZE, _ceil_div(height, MIN_SPLIT_COUNT))
    x_max = _ceil_div(width, piece_w) - 1
    y_max = _ceil_div(height, piece_h) - 1
    scrambled = Image.new("RGB", (width, height))
    for x in range(x_max + 1):
        for y in range(y_max + 1):
            x_dst = piece_w * x
            y_dst = piece_h * y
            w = min(piece_w, width - x_dst)
            h = min(piece_h, height - y_dst)
            x_src = piece_w * (x if x == x_max else (x_max - x + offset) % x_max)
            y_src = piece_h * (y if y == y_max else (y_max - y + offset) % y_max)
            region = original.crop((x_dst, y_dst, x_dst + w, y_dst + h))
            scrambled.paste(region, (x_src, y_src))
    return scrambled


def test_offset_zero_is_byte_identity_passthrough() -> None:
    raw = b"\x89PNG-not-even-decoded-bytes"
    assert _descramble_image(raw, 0) is raw or _descramble_image(raw, 0) == raw


def test_offset_positive_round_trip_reconstructs_original() -> None:
    # Clean 5x5 grid of 100px pieces (width=height=500 → pieceW=100, xMax=4).
    original = _make_original(piece_w=100, piece_h=100, x_max=4, y_max=4)
    offset = 2
    scrambled = _scramble(original, offset)
    # Sanity: scrambling actually moved pixels (offset>0 is a real permutation).
    assert list(scrambled.getdata()) != list(original.getdata())

    out = _descramble_image(_png_bytes(scrambled), offset)
    result = Image.open(io.BytesIO(out)).convert("RGB")
    assert result.size == original.size
    assert list(result.getdata()) == list(original.getdata())


def test_non_image_bytes_with_offset_degrade_to_passthrough() -> None:
    # A hostile / truncated non-image body must not raise — return content unchanged
    # (the packaging is_valid_image guard rejects it downstream, T-12-07).
    junk = b"this is not an image"
    assert _descramble_image(junk, 5) == junk
