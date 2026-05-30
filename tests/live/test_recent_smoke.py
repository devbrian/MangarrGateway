"""Parametrized live ``GET /api/v1/recent`` smoke (D-51 / RCNT-01..02).

Runs once per registered source. Asserts ``/recent`` returns at least one
release and that — when the source returns ≥ 2 releases — the first two
publishDate values are in descending order (newest-first, RCNT-01).
Validates the response against the OpenAPI ``ReleaseListResponse`` schema
via schemathesis (D-54).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import schemathesis
from schemathesis import CheckContext
from schemathesis.core.transport import Response as SchemathesisResponse
from schemathesis.specs.openapi.checks import response_schema_conformance

from ._helpers import live_client_for
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile

pytestmark = [
    pytest.mark.live,
    pytest.mark.parametrize("source_key", REGISTERED_KEYS),
]

_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "manga-gateway.openapi.yaml"

_schema = schemathesis.openapi.from_path(_CONTRACT_PATH)
_schema.config.update(base_url="http://localhost/api/v1")


def _check_response_conforms(
    operation_path: str, method: str, response: httpx.Response
) -> None:
    """See ``test_caps_smoke._check_response_conforms`` — same recipe (W-04)."""
    op = _schema[operation_path][method]
    case = op.Case()
    s_response = SchemathesisResponse.from_httpx(response, verify=False)
    ctx = CheckContext(
        override=None,
        auth=None,
        headers=None,
        config=_schema.config.checks,
        transport_kwargs=None,
    )
    response_schema_conformance(ctx, s_response, case)


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
        _check_response_conforms("/recent", "GET", resp)
