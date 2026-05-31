"""Parametrized live ``GET /api/v1/caps`` smoke (D-51 / CAPS-01..03).

Runs once per registered source (``mangadex``, ``comix``). Asserts the
capability advertisement matches the per-source ``LiveSmokeProfile``
(``expected_caps_antibot``) and validates the response payload against the
``manga-gateway.openapi.yaml`` contract via the schemathesis 4.x
``response_schema_conformance`` check locked by
``SPIKE-schemathesis.md`` (Plan 04 modules copy that recipe verbatim,
W-04).

Cross-references:
* ``tests/live/conftest.py`` — ``REGISTERED_KEYS`` (D-47) + ``profile``
  fixture (D-49) + autouse ``_restore_real_cloudflare_warm``.
* ``tests/live/_helpers.py`` — ``live_client_for`` harness (gates
  ``solver.warm()`` on ``profile.needs_solver_warm``).
* ``tests/test_contract.py:71-75`` — the same ``CONTRACT_CHECKS`` symbol
  list re-applied to a LIVE response (D-54).
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


async def test_caps_advertises_source(
    source_key: str, profile: LiveSmokeProfile
) -> None:
    """``/caps`` lists the source with the expected antibot level (CAPS-01..03).

    D-54: the live response also conforms to the OpenAPI ``Capabilities``
    schema (schemathesis ``response_schema_conformance``).
    """
    async with live_client_for(profile) as client:
        resp = await client.get("/api/v1/caps")
        assert resp.status_code == 200, (
            f"GET /caps failed for {source_key}: {resp.status_code} {resp.text[:400]}"
        )

        payload = resp.json()
        sources = payload.get("sources") or []
        # SourceCap.key is the field name in manga-gateway.openapi.yaml
        # (NOT sourceKey — that name is on Release/SubmitRequest/SourceWarning).
        match = next(
            (s for s in sources if s.get("key") == source_key),
            None,
        )
        assert match is not None, (
            f"{source_key}: not advertised in /caps; sources keys="
            f"{[s.get('key') for s in sources]}"
        )
        assert match.get("enabled") is True, (
            f"{source_key}: advertised but enabled is not True: {match}"
        )
        assert match.get("antibot") == profile.expected_caps_antibot, (
            f"{source_key}: antibot mismatch — got {match.get('antibot')!r}, "
            f"expected {profile.expected_caps_antibot!r}"
        )

        # D-54 / CTRT-01 live: the response body conforms to the OpenAPI
        # Capabilities schema.
        check_response_conforms("/caps", "GET", resp)
