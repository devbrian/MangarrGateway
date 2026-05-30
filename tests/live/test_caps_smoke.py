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

# Contract of record (D-07) — same path tests/test_contract.py:53 reads.
_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "manga-gateway.openapi.yaml"

# Build the schema once at module load — the live smoke modules each own
# their own schema view so the parametrize id seen by schemathesis cannot
# leak across modules. ``base_url`` is purely for path resolution by the
# check; the live HTTP traffic goes through ``live_client_for``'s ASGI
# transport (which prepends its own ``base_url``).
_schema = schemathesis.openapi.from_path(_CONTRACT_PATH)
_schema.config.update(base_url="http://localhost/api/v1")


def _check_response_conforms(
    operation_path: str, method: str, response: httpx.Response
) -> None:
    """Apply schemathesis ``response_schema_conformance`` to a live response.

    Mirrors ``tests/test_contract.py``'s ``CONTRACT_CHECKS`` discipline but
    runs against a manually-issued live HTTP response (D-54). See
    ``SPIKE-schemathesis.md`` for the locked 4.x API path.
    """
    op = _schema[operation_path][method]
    case = op.Case()
    # verify=False — the ASGITransport never establishes TLS.
    s_response = SchemathesisResponse.from_httpx(response, verify=False)
    ctx = CheckContext(
        override=None,
        auth=None,
        headers=None,
        config=_schema.config.checks,
        transport_kwargs=None,
    )
    response_schema_conformance(ctx, s_response, case)


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
        _check_response_conforms("/caps", "GET", resp)
