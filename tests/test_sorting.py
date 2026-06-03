"""Newest-first merge shared by /search and /recent (issue #99).

``fan_out`` concatenates each source's releases in source order, so the merged list
arrives grouped by source, NOT by recency. Both routes must sort newest-first by
``publishDate`` BEFORE truncating to ``limit`` — otherwise a high-volume source fills
the cap and starves sources after it of releases that exist upstream. These tests pin
the shared helper so /search can't regress to the unsorted truncation that #99 fixed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from manga_gateway.api.sorting import TS_FLOOR, parse_publish_ts, sort_newest_first
from manga_gateway.models.search import Release


def _rel(guid: str, source: str, publish_date: str) -> Release:
    return Release(
        guid=guid,
        title=guid,
        source_key=source,
        download_handle=f"h-{guid}",
        publish_date=publish_date,
    )


def test_sort_orders_by_publishdate_descending() -> None:
    releases = [
        _rel("old", "a", "2026-05-01T00:00:00+00:00"),
        _rel("new", "a", "2026-06-02T00:00:00+00:00"),
        _rel("mid", "a", "2026-05-20T00:00:00+00:00"),
    ]
    sort_newest_first(releases)
    assert [r.guid for r in releases] == ["new", "mid", "old"]


def test_high_volume_source_does_not_starve_a_newer_release_under_limit() -> None:
    """The #99 regression guard: a flooding source's OLD releases must not crowd a
    NEWER release out of the truncated top-N just because it was fanned out later."""
    # fan_out concatenation order: the flooding source (45 OLD releases) FIRST, then a
    # low-volume source carrying the single NEWEST release LAST. Pre-#99 a small-limit
    # truncation of this unsorted list dropped the newer release entirely.
    flood = [
        _rel(f"flood-{i}", "mangaball", "2026-05-01T00:00:00+00:00") for i in range(45)
    ]
    newest = _rel("newest", "mangadot", "2026-06-02T00:00:00+00:00")
    merged = [*flood, newest]  # flooder first — the order fan_out would produce

    sort_newest_first(merged)
    top = merged[:5]  # the route's [: limit] step, small limit

    assert top[0].guid == "newest"  # newest survived despite being fanned out last
    assert any(r.source_key == "mangadot" for r in top)  # its source isn't starved


def test_sort_is_stable_for_equal_timestamps() -> None:
    # Ties keep fan-out (source) order — Python's sort is stable.
    same = "2026-05-15T00:00:00+00:00"
    releases = [_rel("first", "a", same), _rel("second", "b", same)]
    sort_newest_first(releases)
    assert [r.guid for r in releases] == ["first", "second"]


def test_empty_or_malformed_publishdate_sorts_oldest() -> None:
    releases = [
        _rel("empty", "a", ""),
        _rel("real", "a", "2026-06-01T00:00:00+00:00"),
        _rel("bad", "a", "not-a-date"),
    ]
    sort_newest_first(releases)
    assert releases[0].guid == "real"  # the only parseable date sorts newest
    assert {r.guid for r in releases[1:]} == {"empty", "bad"}  # floored to oldest


def test_parse_publish_ts_handles_z_offset_and_malformed() -> None:
    assert parse_publish_ts("2026-06-02T00:00:00Z") == datetime(2026, 6, 2, tzinfo=UTC)
    # Naive timestamp is normalized to UTC so comparisons stay aware-vs-aware.
    assert parse_publish_ts("2026-06-02T00:00:00").tzinfo is UTC
    assert parse_publish_ts("") == TS_FLOOR
    assert parse_publish_ts("garbage") == TS_FLOOR
