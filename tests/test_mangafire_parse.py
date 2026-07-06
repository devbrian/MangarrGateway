"""Unit tests for the mangafire release-normalization helpers (JSON API, 260706-hgu).

The site's React-SPA rewrite dropped the lxml HTML scraping entirely — data now arrives
as plain JSON dicts. These tests exercise ``_to_release`` (guid/handle/epoch date) and
the ``createdAt`` unix-epoch conversion directly against recon-shaped chapter rows.
"""

from __future__ import annotations

from decimal import Decimal

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangafire import MangaFireSource, _iso_from_epoch


class _HandleOnlyCtx:
    """Minimal ctx — ``_to_release`` only touches ``handle_store`` (#219 regression)."""

    def __init__(self) -> None:
        self.handle_store = HandleStore()


def test_iso_from_epoch_seconds_to_rfc3339() -> None:
    # createdAt is a unix epoch in SECONDS (e.g. 1757308339 → 2025-09-08T...).
    assert _iso_from_epoch(1757308339) == "2025-09-08T05:12:19+00:00"
    assert _iso_from_epoch("1757308339") == "2025-09-08T05:12:19+00:00"
    # Missing / unparseable / absurd values fall back to None (caller uses now(UTC)).
    assert _iso_from_epoch(None) is None
    assert _iso_from_epoch("") is None
    assert _iso_from_epoch("not-a-number") is None
    assert _iso_from_epoch(10**30) is None


def test_to_release_builds_guid_handle_and_ids() -> None:
    source = MangaFireSource()
    ctx = _HandleOnlyCtx()
    rel = source._to_release(
        "l33",
        "50",
        "Naruto",
        "en",
        {"id": 4736538, "number": "700", "createdAt": 1757308339},
        ctx,  # type: ignore[arg-type]
    )
    assert rel is not None
    assert rel.chapter_number == Decimal("700")
    assert rel.guid == "mangafire:l33:ch-700:en:4736538"
    assert rel.ids == {"mangafireHid": "l33", "mangafireTitleId": "50"}
    assert rel.language == "en"
    # The handle resolves to the numeric chapter id (the /api/chapters/{id} key).
    assert rel.download_handle and ":" not in rel.download_handle


def test_blank_number_chapters_get_distinct_guids() -> None:
    """#219: two chapters with a blank ``number`` (ch_str=='?') in the same
    manga+language must NOT share a guid, or Mangarr dedupes one away. The unique
    numeric chapter id disambiguates them."""
    source = MangaFireSource()
    ctx = _HandleOnlyCtx()
    rel_a = source._to_release(
        "l33",
        "50",
        "Naruto",
        "en",
        {"id": 111, "number": ""},
        ctx,  # type: ignore[arg-type]
    )
    rel_b = source._to_release(
        "l33",
        "50",
        "Naruto",
        "en",
        {"id": 222, "number": ""},
        ctx,  # type: ignore[arg-type]
    )
    assert rel_a is not None and rel_b is not None
    # Both render the ambiguous "?" chapter number ...
    assert ":ch-?:" in rel_a.guid and ":ch-?:" in rel_b.guid
    # ... yet the guids are distinct (the chapter id tail disambiguates) → no dedupe.
    assert rel_a.guid != rel_b.guid
    assert rel_a.guid.endswith(":111")
    assert rel_b.guid.endswith(":222")


def test_to_release_missing_id_returns_none() -> None:
    source = MangaFireSource()
    ctx = _HandleOnlyCtx()
    assert (
        source._to_release("l33", "50", "Naruto", "en", {"number": "1"}, ctx)  # type: ignore[arg-type]
        is None
    )
