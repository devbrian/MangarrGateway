"""Unit tests for MangaBall's module-level parse helpers (Task 1).

Covers the source-agnostic plumbing that does NOT need a ``SourceContext``:

* :func:`_items_and_pagination` — ``(data list, pagination)`` from the v2
  ``{data, pagination}`` envelope (261003-mangaball-api-v2 dropped the old flat
  chapter-listing shape).
* :func:`_strip_html` — the defensive HTML stripper for name fields (HTML must
  never flow raw into a Release field).
* :meth:`MangaBallSource._parse_decimal` — the SRCH-06 Decimal round-trip copied
  verbatim from MangaDex (``"23"`` and ``"23.0"`` both normalize so
  ``format(d.normalize(), "f")`` is stable).

No network, no ctx — pure functions.
"""

from __future__ import annotations

from decimal import Decimal

from manga_gateway.sources.mangaball import (
    MangaBallSource,
    _items_and_pagination,
    _strip_html,
)

# ─────────────────────── _items_and_pagination (D-07) ───────────────────────


def test_items_and_pagination_standard_envelope() -> None:
    """Standard ``{code,message,data,pagination}`` → (data list, pagination)."""
    body = {
        "code": 200,
        "message": "ok",
        "data": [{"_id": "a"}, {"_id": "b"}],
        "pagination": {"total": 2, "current_page": 1, "last_page": 1},
    }
    items, pagination = _items_and_pagination(body)
    assert items == [{"_id": "a"}, {"_id": "b"}]
    assert pagination == {"total": 2, "current_page": 1, "last_page": 1}


def test_items_and_pagination_non_list_data_yields_empty() -> None:
    """A malformed ``data`` (non-list) degrades to an empty item list, no raise."""
    items, pagination = _items_and_pagination({"data": {"oops": True}})
    assert items == []
    assert pagination is None


def test_items_and_pagination_missing_all_keys() -> None:
    """A body with no ``data``/``pagination`` → empty items + None pagination."""
    items, pagination = _items_and_pagination({"code": 200, "message": "ok"})
    assert items == []
    assert pagination is None


# ─────────────────────────── _strip_html (RECON Gotchas) ────────────────────


def test_strip_html_removes_tags_from_alternate_name() -> None:
    raw = 'One Piece<span class="text-muted">/</span>ワンピース'
    out = _strip_html(raw)
    assert "<" not in out
    assert ">" not in out
    assert "One Piece" in out
    assert "ワンピース" in out


def test_strip_html_strips_status_badge() -> None:
    raw = '<span class="badge badge-success">Ongoing</span>'
    out = _strip_html(raw)
    assert out.strip() == "Ongoing"
    assert "<" not in out


def test_strip_html_collapses_whitespace() -> None:
    raw = "  <b>A</b>\n   <i>B</i>  "
    out = _strip_html(raw)
    assert "<" not in out
    assert out == "A B"


def test_strip_html_none_and_empty() -> None:
    assert _strip_html(None) is None
    assert _strip_html("") is None
    assert _strip_html("   ") is None


def test_strip_html_plain_text_passthrough() -> None:
    assert _strip_html("Just Text") == "Just Text"


# ──────────────────────────── _parse_decimal (SRCH-06) ──────────────────────


def test_parse_decimal_int_and_trailing_zero_normalize_equal() -> None:
    d23 = MangaBallSource._parse_decimal("23")
    d23_0 = MangaBallSource._parse_decimal("23.0")
    assert d23 == d23_0 == Decimal("23")
    # The normalized "f" string round-trips identically for both (SRCH-06).
    assert format(d23.normalize(), "f") == format(d23_0.normalize(), "f") == "23"


def test_parse_decimal_preserves_three_places() -> None:
    d = MangaBallSource._parse_decimal("1.005")
    assert d == Decimal("1.005")
    assert format(d.normalize(), "f") == "1.005"


def test_parse_decimal_handles_float_input() -> None:
    d = MangaBallSource._parse_decimal(1184.1)
    assert d == Decimal(str(1184.1))


def test_parse_decimal_none_and_empty_and_garbage() -> None:
    assert MangaBallSource._parse_decimal(None) is None
    assert MangaBallSource._parse_decimal("") is None
    assert MangaBallSource._parse_decimal("not-a-number") is None
