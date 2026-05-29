"""schemathesis contract harness (CTRT-01, D-06, D-07).

Pinned for schemathesis 4.20.2 (empirically confirmed at scaffold time — see
01-01-SUMMARY.md). Invocation shape:

    schema = schemathesis.openapi.from_path(<yaml>)   # D-07: load the FILE
    schema.app = app                                  # route in-process (ASGI)
    schema.config.update(base_url="http://testserver/api/v1")
    schema = schema.include(operation_id="getVersion")  # D-06: implemented ops
    @schema.auth() / @schema.parametrize()
    case.call_and_validate(checks=[...])              # in-process, no bound port

In 4.20.2 ``Case.call_and_validate`` does NOT take an ``app=`` kwarg; the ASGI
app is supplied by setting ``schema.app`` (propagated to every operation, which
``Case.call`` reads to select the ASGI transport).

Checks are scoped to response-conformance (status code + schema) so the harness
is stable from the first endpoint and across plans. API-key enforcement is
proven separately and exhaustively in ``tests/test_auth.py`` (Task 2).
"""

from __future__ import annotations

from pathlib import Path

import schemathesis
from schemathesis import AuthContext, Case
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import (
    response_schema_conformance,
    status_code_conformance,
)

from manga_gateway.app import create_app
from manga_gateway.config import Settings

from .conftest import BASE_URL, TEST_API_KEY

# Contract of record lives at the repo root (D-07), copied from .handoff/.
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "manga-gateway.openapi.yaml"

# Implemented operations this plan exercises (D-06). Grows as endpoints land.
# Plan 01-02 adds getStatus + getCaps; /search, /recent, /downloads* stay excluded.
IMPLEMENTED_OPERATIONS = ["getVersion", "getStatus", "getCaps"]

# Response-conformance checks: stable from endpoint #1.
CONTRACT_CHECKS = [
    not_a_server_error,
    status_code_conformance,
    response_schema_conformance,
]

# Build the app once at import with the deterministic test key (D-03).
_app = create_app(Settings(api_key=TEST_API_KEY))

schema = schemathesis.openapi.from_path(CONTRACT_PATH)
schema.app = _app
schema.config.update(base_url=BASE_URL)
schema = schema.include(operation_id=IMPLEMENTED_OPERATIONS)


@schema.auth()
class ApiKeyAuth:
    """Inject the valid API key into every generated case (D-03)."""

    def get(self, case: Case, ctx: AuthContext) -> str:
        return TEST_API_KEY

    def set(self, case: Case, data: str, ctx: AuthContext) -> None:
        case.headers = dict(case.headers or {})
        case.headers["X-Api-Key"] = data


@schema.parametrize()
def test_contract(case: Case) -> None:
    """Each implemented operation's response conforms to the OpenAPI file."""
    case.call_and_validate(checks=CONTRACT_CHECKS)
