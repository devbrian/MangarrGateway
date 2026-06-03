"""Shared newest-first merge for multi-source release lists (RCNT-01 / SRCH).

Both ``GET /recent`` and ``POST /search`` fan out across sources and then return a
single merged ``releases[]`` capped at ``limit``. The merge MUST be sorted
newest-first by ``publishDate`` BEFORE truncation — otherwise a high-volume source
fills the cap and starves sources after it in the fan-out order, silently dropping
releases that exist upstream (issue #99). This module is the single home for that
sort so the two routes stay byte-for-byte identical.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models.search import Release

# Floor for empty/malformed timestamps so they sort oldest and never crash the
# comparison — guards a source emitting an empty publishDate (WR-05).
TS_FLOOR = datetime.min.replace(tzinfo=UTC)


def parse_publish_ts(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware datetime (handles ``Z`` and offsets).

    Lexicographic string comparison of ISO timestamps is unsafe across mixed
    ``Z``/``+00:00`` suffixes and multi-source merges (WR-02), so ``since`` filtering
    and the newest-first sort compare parsed datetimes instead. Empty or malformed
    values floor to epoch-min (compare as oldest) rather than raising.
    """
    if not raw:
        return TS_FLOOR
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return TS_FLOOR
    # Normalize naive timestamps to UTC so every comparison is aware-vs-aware.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def sort_newest_first(releases: list[Release]) -> None:
    """Sort ``releases`` in place, newest ``publishDate`` first (datetime-parsed).

    Python's sort is stable, so releases sharing a timestamp keep their fan-out
    (source) order. Call this BEFORE applying the ``limit`` truncation so the top-N
    returned are genuinely the newest across all sources, not "whatever fit before
    the cap" (issue #99).
    """
    releases.sort(key=lambda rel: parse_publish_ts(rel.publish_date), reverse=True)
