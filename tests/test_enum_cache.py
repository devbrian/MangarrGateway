"""Unit tests for the source-agnostic enumeration cache (Phase 09 Plan 01).

Network-free. Exercises:
  * ``Enumeration`` + ``covers_floor`` completeness math (CACHE-04)
  * ``EnumerationCache.enum_key`` / ``resolve_key`` shape (CACHE-03, D-01) —
    type/chapter NEVER in the key; language-order-insensitive
  * ``SingleFlightCache`` single-flight collapse, error cleanup (D-04/D-05),
    kill-switch (D-08), maxsize (D-07), per-source TTL override (D-09/CACHE-05)
  * ``EnumerationCache`` two-layer composition + the redacted, failure-isolated
    ``kind="cache"`` metric emit (D-06)
"""

from __future__ import annotations

from decimal import Decimal

from manga_gateway.framework.enum_cache import Enumeration, EnumerationCache

# ───────────────────────────── Enumeration / covers_floor ────────────────────


def test_exhausted_enumeration_covers_any_floor() -> None:
    enum = Enumeration(
        items=[], chapter_numbers=(), exhausted=True, requested_limit=10
    )
    assert enum.covers_floor(999.0) is True


def test_non_exhausted_empty_window_covers_nothing() -> None:
    enum = Enumeration(
        items=[], chapter_numbers=(), exhausted=False, requested_limit=10
    )
    assert enum.covers_floor(1.0) is False


def test_covers_floor_within_window_true_below_window_false() -> None:
    enum = Enumeration(
        items=[object(), object()],
        chapter_numbers=(Decimal("10"), Decimal("20")),
        exhausted=False,
        requested_limit=10,
    )
    # below the cached window → older chapters never fetched → refetch, not empty
    assert enum.covers_floor(5.0) is False
    # inside the window → confidently answerable
    assert enum.covers_floor(15.0) is True
    # at the boundaries (floor math) → still covered
    assert enum.covers_floor(10.0) is True
    assert enum.covers_floor(20.0) is True


# ───────────────────────────── key builders ──────────────────────────────────


def test_enum_key_shape_and_excludes_type_chapter() -> None:
    key = EnumerationCache.enum_key("mangadex", "series-123", ["en", "ja"])
    assert key == ("mangadex", "series-123", ("en", "ja"))


def test_resolve_key_shape() -> None:
    key = EnumerationCache.resolve_key("mangadex", "one piece", ["en"])
    assert key == ("mangadex", "one piece", ("en",))


def test_keys_are_language_order_insensitive() -> None:
    a = EnumerationCache.enum_key("mangadex", "s", ["ja", "en"])
    b = EnumerationCache.enum_key("mangadex", "s", ["en", "ja"])
    assert a == b
    c = EnumerationCache.resolve_key("mangadex", "q", ["ja", "en"])
    d = EnumerationCache.resolve_key("mangadex", "q", ["en", "ja"])
    assert c == d
