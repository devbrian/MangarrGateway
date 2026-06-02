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

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
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

# Comix (04-03) is now registered, so every generated ``search``/``getRecent`` case
# fans out to its ``cloudflare+encrypted`` host. Without a stub that host is
# unroutable (``.example``) and tenacity would retry the ConnectError on exponential
# backoff for EVERY generated case — turning the contract suite into a multi-minute,
# network-dependent hang. A fast permanent 403 short-circuits Comix to a per-source
# ``warnings[]`` entry instantly (no retry, no real network), keeping the gate
# deterministic (D-42) while still exercising both sources' contract paths.
_COMIX_HOST = "comix.to"
# 07-03: MangaBall is now registered too, so every generated search/getRecent case
# also fans out to mangaball.net. Stub it to a fast permanent 403 for the same
# reason as Comix — a per-source warnings[] entry, no retry, no real network.
_MANGABALL_HOST = "mangaball.net"

# Contract of record lives at the repo root (D-07), copied from .handoff/.
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "manga-gateway.openapi.yaml"

# Implemented operations this plan exercises (D-06). Grows as endpoints land.
# Plan 03-04 adds the four download-surface ops (submit/list/get/remove) now that the
# full submit→fetch→package→poll→delete loop is contract-faithful.
IMPLEMENTED_OPERATIONS = [
    "getVersion",
    "getStatus",
    "getCaps",
    "search",
    "getRecent",
    "submitDownload",
    "getDownloads",
    "getDownload",
    "removeDownload",
]

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


@pytest.fixture(autouse=True)
def _stub_source_hosts() -> Iterator[None]:
    """Intercept all source HTTP so the contract suite touches NO real network.

    schemathesis generates many ``search``/``getRecent`` cases; each fans out to
    every registered source. We stub the upstream hosts so the suite is fast and
    deterministic (D-42): MangaDex returns an empty-but-valid collection (→ 0
    releases, still 200), and Comix returns a fast permanent 403 (→ a per-source
    ``warnings[]`` entry, no retry). The gateway's contract-level response shape —
    the only thing schemathesis validates here — is unchanged either way.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="api.mangadex.org").mock(
            return_value=httpx.Response(
                200,
                json={"result": "ok", "response": "collection", "data": []},
            )
        )
        mock.route(host=_COMIX_HOST).mock(return_value=httpx.Response(403))
        mock.route(host=_MANGABALL_HOST).mock(return_value=httpx.Response(403))
        yield


@schema.parametrize()
def test_contract(case: Case) -> None:
    """Each implemented operation's response conforms to the OpenAPI file."""
    case.call_and_validate(checks=CONTRACT_CHECKS)
