"""Parametrized live ``GET /api/v1/recent`` smoke (D-51 / RCNT-01..02).

Runs once per registered source. Asserts ``/recent`` returns at least one
release and that — when the source returns ≥ 2 releases — the first two
publishDate values are in descending order (newest-first, RCNT-01).
Validates the response against the OpenAPI ``ReleaseListResponse`` schema
via schemathesis (D-54).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ._helpers import check_response_conforms, live_client_for
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile


def _parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 / ISO 8601 date-time as a timezone-aware ``datetime``.

    ``datetime.fromisoformat`` accepts trailing ``Z`` from Python 3.11+, but
    we substitute ``+00:00`` so the parser works the same way on older
    fallback paths and the substitution is explicit at the call site.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


pytestmark = [
    pytest.mark.live,
    pytest.mark.parametrize("source_key", REGISTERED_KEYS),
]


async def test_recent_returns_newest_first(
    source_key: str, profile: LiveSmokeProfile
) -> None:
    """GET /recent returns releases ordered newest-first (RCNT-01/02).

    Asserts:
    * 200 status.
    * At least one release returned.
    * When ≥ 2 releases: the first two ``publishDate`` values are in
      descending order (newest-first, RCNT-01).
    * D-54: response body conforms to ``ReleaseListResponse``.
    """
    async with live_client_for(profile) as client:
        resp = await client.get(
            "/api/v1/recent",
            params={"sources": source_key, "limit": 5},
        )
        assert resp.status_code == 200, (
            f"{source_key}: GET /recent failed: {resp.status_code} {resp.text[:400]}"
        )

        payload = resp.json()
        releases = payload.get("releases") or []
        assert releases, f"{source_key}: /recent returned no releases: {payload}"

        if len(releases) >= 2:
            first = releases[0].get("publishDate")
            second = releases[1].get("publishDate")
            assert first and second, (
                f"{source_key}: /recent missing publishDate on first two "
                f"releases: {releases[:2]}"
            )
            # publishDate is RFC 3339. Lexicographic compare is correct only
            # when every source emits the same fixed-width UTC representation;
            # the OpenAPI contract just pins ``format: date-time``, so a
            # source emitting e.g. ``2026-05-30T08:00:00-04:00`` and another
            # emitting ``2026-05-30T12:00:00+00:00`` (same instant) would
            # mis-order under string compare. Parse to timezone-aware
            # datetimes first (CodeRabbit PR #33 review).
            assert _parse_rfc3339(first) >= _parse_rfc3339(second), (
                f"{source_key}: /recent not newest-first — "
                f"first={first!r} second={second!r}"
            )

        # D-54 / CTRT-01 live: response conforms to ReleaseListResponse.
        check_response_conforms("/recent", "GET", resp)
