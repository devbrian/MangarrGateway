"""Parametrized live ``POST /api/v1/search`` smoke (D-51 / SRCH-01..04 / REL-01).

Runs once per registered source. Asserts the search call returns at least
``profile.min_releases_returned`` releases for the planner-pinned
``default_query``, that NO warning is emitted for the source, and that every
release carries the REL-01 required fields. Validates the live response
against the OpenAPI ``ReleaseListResponse`` schema via schemathesis
(D-54 / CTRT-01 live).
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


async def test_search_returns_releases(
    source_key: str, profile: LiveSmokeProfile
) -> None:
    """POST /search for ``profile.default_query`` returns ≥1 release (SRCH/REL-01).

    Asserts:
    * 200 status.
    * ``releases`` array length ≥ ``profile.min_releases_returned``.
    * NO ``warnings[]`` entry with ``sourceKey == source_key`` (the source
      successfully fanned out — Cloudflare cleared, MangaDex 200, etc.).
    * Every release carries the REL-01 required fields:
      ``guid``, ``title``, ``sourceKey``, ``downloadHandle``, ``publishDate``.
    * Every release reports the right ``sourceKey``.
    * D-54: response body conforms to ``ReleaseListResponse``.
    """
    async with live_client_for(profile) as client:
        resp = await client.post(
            "/api/v1/search",
            json={
                "type": "chapter",
                "query": profile.default_query,
                "sources": [source_key],
            },
        )
        assert resp.status_code == 200, (
            f"{source_key}: POST /search failed: {resp.status_code} {resp.text[:400]}"
        )

        payload = resp.json()
        releases = payload.get("releases") or []
        assert len(releases) >= profile.min_releases_returned, (
            f"{source_key}: search for {profile.default_query!r} returned "
            f"{len(releases)} releases (< min_releases_returned="
            f"{profile.min_releases_returned}): {payload}"
        )

        warnings = payload.get("warnings") or []
        source_warnings = [w for w in warnings if w.get("sourceKey") == source_key]
        assert not source_warnings, (
            f"{source_key}: search emitted source-scoped warning(s): {source_warnings}"
        )

        # REL-01 required fields on every release.
        required_fields = (
            "guid",
            "title",
            "sourceKey",
            "downloadHandle",
            "publishDate",
        )
        for i, release in enumerate(releases):
            missing = [f for f in required_fields if not release.get(f)]
            assert not missing, (
                f"{source_key}: release[{i}] missing REL-01 fields {missing}: {release}"
            )
            assert release["sourceKey"] == source_key, (
                f"{source_key}: release[{i}] has wrong sourceKey "
                f"{release['sourceKey']!r}"
            )

        # D-54 / CTRT-01 live: response conforms to ReleaseListResponse.
        check_response_conforms("/search", "POST", resp)
