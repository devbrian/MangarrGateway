"""Unit tests for the mangafire lxml parsers (D-06 chapter list / D-07+D-13 cards).

All parsers are blocking-by-design (lxml C-parse) and called via ``asyncio.to_thread``
by their hooks; here they are exercised directly against recon-shaped HTML fragments.
A malformed fragment yields ``[]`` (never raises).
"""

from __future__ import annotations

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangafire import (
    MangaFireSource,
    _parse_cards,
    _parse_chapter_list,
)


class _HandleOnlyCtx:
    """Minimal ctx — ``_to_release`` only touches ``handle_store`` (#219 regression)."""

    def __init__(self) -> None:
        self.handle_store = HandleStore()


# ── chapter-list `result` HTML (D-06) ──────────────────────────────────────────
_CHAPTER_LIST = """
<ul>
  <li class="item" data-number="346.2">
    <a href="/read/blue-lockk.kw9j9/en/chapter-346.2" title="Vol 0 - Chap 346.2">
      <span>Chapter 346.2: </span><span>May 21, 2026</span>
    </a>
  </li>
  <li class="item" data-number="346.1">
    <a href="/read/blue-lockk.kw9j9/en/chapter-346.1">
      <span>Chapter 346.1</span><span>May 14, 2026</span>
    </a>
  </li>
</ul>
"""

# ── recent / search card HTML (D-07 / D-13): `.original.card-lg .unit .inner` ───
_CARDS = """
<div class="original card-lg">
  <div class="unit">
    <div class="inner">
      <a href="/manga/blue-lockk.kw9j9" class="poster">
        <img src="https://mangafire.to/static/thumb/blue.jpg">
      </a>
      <div class="info">
        <a href="/manga/blue-lockk.kw9j9">Blue Lock</a>
      </div>
    </div>
    <div class="inner">
      <a href="/manga/one-piece.abcd" class="poster">
        <img src="https://mangafire.to/static/thumb/op.jpg">
      </a>
      <div class="info">
        <a href="/manga/one-piece.abcd">One Piece</a>
      </div>
    </div>
  </div>
</div>
"""


def test_parse_chapter_list_yields_href_number_date_per_li() -> None:
    rows = _parse_chapter_list(_CHAPTER_LIST)
    assert rows == [
        {
            "href": "/read/blue-lockk.kw9j9/en/chapter-346.2",
            "number": "346.2",
            "date": "May 21, 2026",
        },
        {
            "href": "/read/blue-lockk.kw9j9/en/chapter-346.1",
            "number": "346.1",
            "date": "May 14, 2026",
        },
    ]


def test_parse_cards_yields_href_title_thumbnail_per_inner() -> None:
    cards = _parse_cards(_CARDS.encode())
    assert cards == [
        {
            "href": "/manga/blue-lockk.kw9j9",
            "title": "Blue Lock",
            "thumbnail": "https://mangafire.to/static/thumb/blue.jpg",
        },
        {
            "href": "/manga/one-piece.abcd",
            "title": "One Piece",
            "thumbnail": "https://mangafire.to/static/thumb/op.jpg",
        },
    ]


def test_parse_chapter_list_malformed_returns_empty() -> None:
    assert _parse_chapter_list("") == []
    assert _parse_chapter_list("<<<not html>>>") == []
    # A well-formed fragment with no data-number li yields no rows.
    assert _parse_chapter_list("<ul><li>no number</li></ul>") == []


def test_parse_cards_malformed_returns_empty() -> None:
    assert _parse_cards(b"") == []
    assert _parse_cards(b"<<<not html>>>") == []
    # A card with no /manga/ info link is skipped.
    no_link = b"<div class='original card-lg'><div class='inner'></div></div>"
    assert _parse_cards(no_link) == []


def test_blank_data_number_chapters_get_distinct_guids() -> None:
    """#219: two chapters with a blank data-number (ch_str=='?') in the same
    manga+language must NOT share a guid, or Mangarr dedupes one away. The unique
    read href disambiguates them."""
    source = MangaFireSource()
    ctx = _HandleOnlyCtx()
    common = dict(  # noqa: C408 — readability over literal
        manga_token="blue-lockk.kw9j9",
        manga_title="Blue Lock",
        lang="en",
        ctx=ctx,
    )
    rel_a = source._to_release(
        chapter={"href": "/read/blue-lockk.kw9j9/en/chapter-extra-a", "number": ""},
        **common,
    )
    rel_b = source._to_release(
        chapter={"href": "/read/blue-lockk.kw9j9/en/chapter-extra-b", "number": ""},
        **common,
    )
    assert rel_a is not None and rel_b is not None
    # Both render the ambiguous "?" chapter number ...
    assert ":ch-?:" in rel_a.guid and ":ch-?:" in rel_b.guid
    # ... yet the guids are distinct (the href tail disambiguates) → no dedupe loss.
    assert rel_a.guid != rel_b.guid
    assert rel_a.guid.endswith(":chapter-extra-a")
    assert rel_b.guid.endswith(":chapter-extra-b")
