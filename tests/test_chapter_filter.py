"""Direct unit tests for the shared ``Source.chapter_matches`` floor-match helper.

Covers the locked predicate (quick task 260606-2ff): the gate-OFF pass-through
(``type=manga`` and a chapterless ``type=chapter``), the whole-number / floor family
for an integer request (``chapter=179``) AND a decimal request (``chapter=179.5``),
and the ``chapter_number is None`` drop under an active gate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from manga_gateway.framework.base import Source
from manga_gateway.models.search import SearchRequest

# ───────────────────────────── Gate OFF (pass-through) ──────────────────────────


def test_gate_off_type_manga_always_matches() -> None:
    # type=manga is never filtered, even when chapter is set and chapter_number is None.
    req = SearchRequest(type="manga", query="X", chapter=179)
    assert Source.chapter_matches(req, Decimal("180")) is True
    assert Source.chapter_matches(req, None) is True


def test_gate_off_chapterless_chapter_search_always_matches() -> None:
    # type=chapter but no chapter named → gate OFF, pass-through for everything.
    req = SearchRequest(type="chapter", query="X")
    assert Source.chapter_matches(req, Decimal("180")) is True
    assert Source.chapter_matches(req, None) is True


# ───────────────────────── Gate ON: integer request (179) ───────────────────────


@pytest.mark.parametrize(
    "chapter_number",
    [Decimal("179"), Decimal("179.1"), Decimal("179.5"), Decimal("179.9")],
)
def test_gate_on_integer_request_matches_floor_family(chapter_number: Decimal) -> None:
    # locked 1: chapter=179 returns the whole 179.x floor-family.
    req = SearchRequest(type="chapter", query="X", chapter=179)
    assert Source.chapter_matches(req, chapter_number) is True


@pytest.mark.parametrize("chapter_number", [Decimal("180"), Decimal("178.9")])
def test_gate_on_integer_request_excludes_other_floors(
    chapter_number: Decimal,
) -> None:
    req = SearchRequest(type="chapter", query="X", chapter=179)
    assert Source.chapter_matches(req, chapter_number) is False


# ───────────────────────── Gate ON: decimal request (179.5) ──────────────────────


@pytest.mark.parametrize("chapter_number", [Decimal("179"), Decimal("179.9")])
def test_gate_on_decimal_request_matches_same_floor_family(
    chapter_number: Decimal,
) -> None:
    # locked 2: a decimal request still matches its whole-number family (floor, not exact).
    req = SearchRequest(type="chapter", query="X", chapter=179.5)
    assert Source.chapter_matches(req, chapter_number) is True


def test_gate_on_decimal_request_excludes_other_floors() -> None:
    req = SearchRequest(type="chapter", query="X", chapter=179.5)
    assert Source.chapter_matches(req, Decimal("180")) is False


# ──────────────────────────── Gate ON: None drop ────────────────────────────────


def test_gate_on_drops_none_chapter_number() -> None:
    # locked 3: a release with no parsed chapter cannot match a requested chapter.
    req = SearchRequest(type="chapter", query="X", chapter=179)
    assert Source.chapter_matches(req, None) is False
