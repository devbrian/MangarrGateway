"""Unit tests for ``MangadotSource`` pure parsing + the SSRF allowlist matrix.

Covers the field-normalization helpers (no network, no ctx):

* ``_parse_decimal`` — int (``1``) and string (``"26.0"``) chapter numbers.
* ``_normalize_publish_date`` — the space-separated ``+00`` ``date_added`` shape
  (``"2026-05-12 12:21:39+00"``) → RFC3339 ``T``-separated; empty/malformed →
  ``now(UTC)`` fallback (REL-03).
* ``_is_allowed_image_url`` — the same-origin ``/chapters/`` SSRF allowlist
  accept/reject matrix (host PINNED to mangadot.net, T-07).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from manga_gateway.sources.mangadot import MangadotSource, _is_allowed_image_url

# ───────────────────────────── _parse_decimal ──────────────────────────────


def test_parse_decimal_handles_int_and_string() -> None:
    assert MangadotSource._parse_decimal(1) == Decimal("1")
    assert MangadotSource._parse_decimal("26.0") == Decimal("26.0")
    assert MangadotSource._parse_decimal(1.5) == Decimal("1.5")


def test_parse_decimal_none_and_malformed() -> None:
    assert MangadotSource._parse_decimal(None) is None
    assert MangadotSource._parse_decimal("") is None
    assert MangadotSource._parse_decimal("not-a-number") is None


# ───────────────────────── _normalize_publish_date ─────────────────────────


def test_normalize_publish_date_space_separated_offset() -> None:
    # The live shape: space separator + ``+00`` offset → RFC3339 with a T separator.
    out = MangadotSource._normalize_publish_date("2026-05-12 12:21:39+00")
    assert out == "2026-05-12T12:21:39+00:00"
    # Round-trips cleanly through fromisoformat (contract date-time conformance).
    assert datetime.fromisoformat(out).tzinfo is not None


def test_normalize_publish_date_empty_falls_back_to_now() -> None:
    # Assert the fallback lands within the call window (not just the year) so a
    # wrong future/past timestamp can't slip through (review).
    before = datetime.now(UTC)
    out = MangadotSource._normalize_publish_date("")
    after = datetime.now(UTC)
    parsed = datetime.fromisoformat(out)
    assert before <= parsed <= after


def test_normalize_publish_date_malformed_falls_back_to_now() -> None:
    before = datetime.now(UTC)
    out = MangadotSource._normalize_publish_date("garbage")
    after = datetime.now(UTC)
    parsed = datetime.fromisoformat(out)
    assert before <= parsed <= after


# ───────────────────────── SSRF allowlist matrix ───────────────────────────


def test_ssrf_accepts_same_origin_chapters_image() -> None:
    assert _is_allowed_image_url(
        "https://mangadot.net/chapters/manga_5296/chapter_26/001.webp"
    )
    # User-source folder shape (manga_{id}/user_{uid}_…/) — also accepted.
    assert _is_allowed_image_url(
        "https://mangadot.net/chapters/manga_20277/user_42_ch1_en_abc/001.jpg"
    )
    # All allowed image extensions.
    for ext in ("webp", "jpg", "jpeg", "png"):
        assert _is_allowed_image_url(f"https://mangadot.net/chapters/m/c/01.{ext}")


def test_ssrf_rejects_http_scheme() -> None:
    assert not _is_allowed_image_url(
        "http://mangadot.net/chapters/manga_5296/chapter_26/001.webp"
    )


def test_ssrf_rejects_off_host() -> None:
    assert not _is_allowed_image_url("https://evil.com/chapters/m/c/01.webp")
    # A host that merely CONTAINS mangadot.net is not the exact origin.
    assert not _is_allowed_image_url(
        "https://mangadot.net.evil.com/chapters/m/c/01.webp"
    )
    assert not _is_allowed_image_url("https://notmangadot.net/chapters/m/c/01.webp")
    # Host is pinned to the EXACT origin (review): even a real subdomain is
    # rejected — mangadot serves page images same-origin, never off a subdomain.
    assert not _is_allowed_image_url("https://cdn.mangadot.net/chapters/m/c/01.webp")


def test_ssrf_rejects_traversal() -> None:
    assert not _is_allowed_image_url(
        "https://mangadot.net/chapters/../../etc/passwd.webp"
    )


def test_ssrf_rejects_non_image_extension_and_off_namespace() -> None:
    # Non-image extension.
    assert not _is_allowed_image_url("https://mangadot.net/chapters/m/c/01.svg")
    assert not _is_allowed_image_url("https://mangadot.net/chapters/m/c/script.js")
    # Off the /chapters/ namespace.
    assert not _is_allowed_image_url("https://mangadot.net/api/chapters/1/images")
    assert not _is_allowed_image_url("https://mangadot.net/covers/1.webp")
