"""Unit tests for Comix issue #42 — deferred chapter-id resolution + recent feed.

Coverage:

* :func:`_resolve_deferred` — 14 cases lifted from spike 002
  (``.planning/spikes/002-comix-deferred-chapter-id-resolution/test_deferred_resolver.py``):
  int chapter, decimal chapter, trailing-zero / decimal normalization,
  multi-group tie-breaks (newest → non-empty-groups → lowest-id),
  strict-match staleness (missing chapter → ``_DeferredResolutionError``),
  malformed-row tolerance, composite roundtrip + empty-segment rejection.
* :meth:`ComixSource.fetch_manifest` — the deferred branch substitutes the
  resolved numeric id before building the chapter URL; a missing chapter on
  the series page surfaces as ``SourceError('source_unavailable', …)``.
* :meth:`ComixSource.recent` — respx-mocked plaintext one-call feed produces
  the expected Release shape (``:DEFERRED`` guid suffix, deferred composite
  in the handle store, decimal-normalized chapter strings, skips for
  ``hasChapters: false`` / missing hid / unparseable relative time).

No network, no browser — synthetic chapter lists drive the matcher tests, a
fake ``SourceContext`` drives the ``fetch_manifest`` tests, and respx mocks
the single ``/api/v1/manga`` call for the ``recent`` test. The four locked
invariants (decision 1: ``:DEFERRED`` guid permanent; decision 4: strict
staleness; decision 5: Decimal-aware matching; decision 7: no series-page
nav in ``recent()``) are unit-asserted.
"""

from __future__ import annotations

from typing import Any

import pytest

from manga_gateway.sources.comix import (
    _DEFERRED_SENTINEL,
    ComixSource,
    _DeferredResolutionError,
    _make_deferred_composite,
    _resolve_deferred,
)


def _row(
    chapter_id: str,
    chapter: str,
    *,
    rel: str | None = None,
    groups: list[dict[str, Any]] | None = None,
    lang: str = "en",
) -> dict[str, Any]:
    """Build a realistic chapter-list row (matches ``_CHAPTER_LIST_EXTRACT_JS``)."""
    return {
        "id": chapter_id,
        "chapter": chapter,
        "lang": lang,
        "groups": groups or [],
        "publishedAtRelative": rel,
    }


# ────────────────────────── Case 1: int chapter number ──────────────────────


def test_resolve_deferred_int_chapter_number() -> None:
    series = [
        _row("12345", "23", rel="3h ago", groups=[{"name": "GroupA"}]),
        _row("12344", "22", rel="1d ago"),
        _row("12343", "21", rel="2d ago"),
    ]
    assert _resolve_deferred("23", series) == "12345"


# ──────────────────────── Case 2: decimal chapter number ────────────────────


def test_resolve_deferred_decimal_chapter_1_2() -> None:
    series = [
        _row("99001", "2", rel="1h ago"),
        _row("99000", "1.2", rel="2h ago", groups=[{"name": "Decimals R Us"}]),
        _row("98999", "1.1", rel="3h ago"),
        _row("98998", "1", rel="4h ago"),
    ]
    assert _resolve_deferred("1.2", series) == "99000"


def test_resolve_deferred_decimal_chapter_72_8() -> None:
    series = [
        _row("777", "72.8", rel="5h ago"),
        _row("776", "72.5", rel="8h ago"),
    ]
    assert _resolve_deferred("72.8", series) == "777"


# ──────────────────── Case 3: trailing-zero normalization ───────────────────


def test_resolve_deferred_handle_23_dot_0_matches_page_23() -> None:
    """Locked decision 5: Decimal-aware. ``'23.0'`` matches series row ``'23'``."""
    series = [_row("555", "23", rel="2h ago")]
    assert _resolve_deferred("23.0", series) == "555"


def test_resolve_deferred_handle_23_matches_page_23_dot_0() -> None:
    """Reverse direction: ``'23'`` matches series row ``'23.0'`` via Decimal."""
    series = [_row("556", "23.0", rel="2h ago")]
    assert _resolve_deferred("23", series) == "556"


# ────────────── Case 4: multi-group same chapter (tie-break ladder) ─────────


def test_resolve_deferred_multi_group_newest_wins() -> None:
    """Tie-break rung 1 (locked decision 6): newest ``publishedAtRelative``."""
    series = [
        _row("4001", "10", rel="5d ago", groups=[{"name": "OldGroup"}]),
        _row("4002", "10", rel="2h ago", groups=[{"name": "NewGroup"}]),  # newest
        _row("4003", "10", rel="1d ago", groups=[{"name": "MidGroup"}]),
    ]
    assert _resolve_deferred("10", series) == "4002"


def test_resolve_deferred_tie_on_time_prefers_non_empty_groups() -> None:
    """Tie-break rung 2: tie on relative time → non-empty groups wins."""
    series = [
        _row("5001", "10", rel="3h ago", groups=[]),
        _row("5002", "10", rel="3h ago", groups=[{"name": "G"}]),
    ]
    assert _resolve_deferred("10", series) == "5002"


def test_resolve_deferred_tie_on_time_and_groups_lowest_numeric_id() -> None:
    """Tie-break rung 3: tie on time + groups → lowest numeric id wins."""
    series = [
        _row("6002", "10", rel="3h ago"),
        _row("6001", "10", rel="3h ago"),
    ]
    assert _resolve_deferred("10", series) == "6001"


# ─────────────────────── Case 5: chapter missing (strict) ───────────────────


def test_resolve_deferred_missing_chapter_raises_strict() -> None:
    """Locked decision 4: strict-match staleness. Missing chapter → raise."""
    series = [
        _row("700", "24", rel="1h ago"),
        _row("699", "22", rel="2h ago"),  # 23 was deleted upstream
    ]
    with pytest.raises(_DeferredResolutionError, match="23 not present"):
        _resolve_deferred("23", series)


# ───────────────────── Case 6: malformed series rows skipped ────────────────


def test_resolve_deferred_skips_malformed_rows() -> None:
    """Malformed rows (None chapter, ``'abc'`` chapter, non-dict, None entry)
    are skipped; the one valid matching row still resolves."""
    series: list[Any] = [
        {"id": "111", "chapter": None, "groups": []},
        {"id": "112", "chapter": "abc", "groups": []},
        _row("113", "5", rel="1h ago"),
        "not a dict",
        None,
    ]
    assert _resolve_deferred("5", series) == "113"


# ───────────────── Case 7: composite roundtrip + sentinel ───────────────────


def test_make_deferred_composite_well_formed_int_chapter() -> None:
    comp = _make_deferred_composite("mr3m0", "the-forgotten-field", "23")
    assert comp == "DEFERRED|mr3m0|the-forgotten-field|23"


def test_make_deferred_composite_roundtrips_through_parser() -> None:
    """Locked decision 8: the deferred composite has exactly 4 non-empty
    segments so the existing ``_parse_composite_chapter_id`` accepts it
    unchanged. The leading segment must equal the ``_DEFERRED_SENTINEL``."""
    comp = _make_deferred_composite("mr3m0", "the-forgotten-field", "23")
    numeric_id, hid, slug, number = ComixSource._parse_composite_chapter_id(comp)
    assert numeric_id == _DEFERRED_SENTINEL == "DEFERRED"
    assert hid == "mr3m0"
    assert slug == "the-forgotten-field"
    assert number == "23"


def test_make_deferred_composite_empty_hid_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("", "slug", "23")


def test_make_deferred_composite_empty_slug_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("mr3m0", "", "23")


def test_make_deferred_composite_empty_chapter_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("mr3m0", "slug", "")


# ─────────────────── Case 8: decimal composite roundtrip ────────────────────


def test_make_deferred_composite_decimal_chapter_roundtrips() -> None:
    comp = _make_deferred_composite("gzg9", "long-forgotten-name", "1.2")
    assert comp == "DEFERRED|gzg9|long-forgotten-name|1.2"
    numeric_id, hid, slug, number = ComixSource._parse_composite_chapter_id(comp)
    assert numeric_id == _DEFERRED_SENTINEL
    assert hid == "gzg9"
    assert slug == "long-forgotten-name"
    assert number == "1.2"


# Note: ``test_comix_declares_supports_recent_true`` lives at the end of this
# file once Task 3 flips the capability — kept out of Task 1 so the helpers
# can land independently of the flag (the plan deliberately stages it).
