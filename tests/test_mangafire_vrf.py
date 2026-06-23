"""Unit tests for the vendored pure-Python MangaFire search ``vrf`` generator.

No network, no browser: ``compute_vrf`` is a deterministic pure function of the
keyword. These vectors pin the reverse-engineered output byte-exact against the
validated live corpus (build ``scripts.js?69b68df23``) so any silent drift in the
embedded constants or the pipeline is caught immediately.
"""

from __future__ import annotations

import re

from manga_gateway.sources.mangafire_vrf import compute_vrf

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def test_compute_vrf_blue_lock_byte_exact() -> None:
    assert (
        compute_vrf("blue lock")
        == "5fcaUfZo7rW1-Z3vTEvXO5sJBfP2UeTM2NIVmfvEuGhYVy8cvonUJecs8YLq"
    )


def test_compute_vrf_naruto_byte_exact() -> None:
    assert (
        compute_vrf("naruto")
        == "5fcaUfZo7rW1-Z3vTEvXO5sJBfP2MeTM2NIVmftCuGhYJy8cvYlf_w"
    )


def test_compute_vrf_unicode_shape_and_deterministic() -> None:
    # A non-ASCII keyword exercises the encodeURIComponent → latin-1 path.
    token = compute_vrf("ワンピース")
    assert isinstance(token, str)
    assert token  # non-empty
    assert _BASE64URL.match(token)  # base64url, no padding
    # Deterministic: a pure function returns the same token across calls.
    assert compute_vrf("ワンピース") == token
    # Byte-exact pin (validated corpus value) belt-and-suspenders.
    assert token == (
        "5fcaUfZo7rW1-Z3vTEvXO5sJBfP2V-TM2NIVmftQuGhYMy8cuom8pbiBGPRZBIWMgdMd6eielj1i62Tv9_FHUZRRwupjBRwEgiLSNi6Ong"
    )
