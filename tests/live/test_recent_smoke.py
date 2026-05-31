"""Parametrized live ``GET /api/v1/recent`` smoke (D-51 / RCNT-01..02).

Runs once per registered source. Asserts ``/recent`` returns at least one
release and that — when the source returns ≥ 2 releases — the first two
publishDate values are in descending order (newest-first, RCNT-01).
Validates the response against the OpenAPI ``ReleaseListResponse`` schema
via schemathesis (D-54).
"""

from __future__ import annotations

import pytest

from ._helpers import check_response_conforms, live_client_for
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile

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
            # publishDate is RFC 3339 — lexicographic compare is correct
            # ordering for ISO 8601 / RFC 3339 strings (date-time format
            # is fixed-width and uses UTC offset).
            assert first >= second, (
                f"{source_key}: /recent not newest-first — "
                f"first={first!r} second={second!r}"
            )

        # D-54 / CTRT-01 live: response conforms to ReleaseListResponse.
        check_response_conforms("/recent", "GET", resp)
