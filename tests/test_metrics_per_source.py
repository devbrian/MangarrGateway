"""Per-source + final result_count + warnings-summary wiring (260605-e9a Task 2).

Drives the ``/search`` and ``/recent`` route handlers DIRECTLY (no network) with
fake Source instances returning known PRE-MERGE counts that differ from the final
(limit truncates), and a REAL ``Collector`` installed via ``set_collector`` over a
synchronous ``CapturingRingWriter``. Asserts:

* the umbrella ``request`` event carries ``result_count == len(final releases)`` and
  a ``warnings_summary`` listing the warned sources + codes;
* each source's ``source-result`` event carries the right PRE-MERGE ``result_count``
  AND ``candidates_enumerated``;
* the request blob round-trips method/path/query_string/body for both endpoints.

The middleware emits the umbrella ``request`` event AFTER the handler returns, so the
tests emit it themselves (with the handler-stashed contextvar still set) to witness
what the middleware would carry.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from fastapi import Request
from starlette.responses import JSONResponse

from manga_gateway.api.routes.recent import get_recent
from manga_gateway.api.routes.search import search
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.cooldown import SourceFailureCooldown
from manga_gateway.framework.enum_cache import EnumerationCache
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.framework.source_pin import SourcePinnedProxies
from manga_gateway.handles.store import HandleStore
from manga_gateway.metrics.collector import Collector, set_collector
from manga_gateway.metrics.context import current_request, current_source
from manga_gateway.metrics.snapshot import MetricSnapshotStore
from manga_gateway.metrics.store import InMemoryStore
from manga_gateway.models.search import (
    Release,
    ReleaseListResponse,
    SearchRequest,
)

from ._metrics_helpers import CapturingRingWriter

# ───────────────────────────── fakes ─────────────────────────────────────────


def _release(i: int, source_key: str) -> Release:
    return Release(
        guid=f"{source_key}:{i}",
        title=f"Chapter {i}",
        sourceKey=source_key,
        downloadHandle=f"h-{source_key}-{i}",
        publishDate=f"2026-06-{(i % 28) + 1:02d}T00:00:00Z",
    )


class _FakeSource:
    """A registry-shaped fake whose ``search``/``recent`` return a known count and
    set ``ctx.candidates_enumerated`` + a soft warning."""

    rate_limit_per_minute = 6000
    antibot = "none"
    decrypt_scheme = None
    supports_recent = True

    def __init__(
        self, key: str, *, count: int, candidates: int, warn: tuple[str, str] | None
    ) -> None:
        self.key = key
        self._count = count
        self._candidates = candidates
        self._warn = warn

    async def search(self, req: SearchRequest, ctx: SourceContext) -> list[Release]:
        ctx.candidates_enumerated = self._candidates
        if self._warn is not None:
            ctx.warn(*self._warn)
        releases = [_release(i, self.key) for i in range(self._count)]
        # Self-attributed per-source summary (mirrors the route's _run_one emit so a
        # direct-handler test exercises the same path).
        return releases

    async def recent(
        self,
        *,
        languages: list[str] | None,
        limit: int,
        since: str | None,
        ctx: SourceContext,
    ) -> list[Release]:
        ctx.candidates_enumerated = self._candidates
        # /recent does NOT collect soft warnings (no collect_warnings on its
        # fan_out) — surface a hard SourceError so the warned source appears in
        # warning_tuples → warnings_summary.
        if self._warn is not None:
            raise SourceError(self._warn[0], self._warn[1])
        return [_release(i, self.key) for i in range(self._count)]


class _FakeRegistry:
    def __init__(self, sources: dict[str, _FakeSource]) -> None:
        self._sources = sources

    def keys(self) -> list[str]:
        return list(self._sources)

    def get(self, key: str) -> object:
        src = self._sources.get(key)
        if src is None:
            return None
        # The route calls ``cls()`` — return a zero-arg callable yielding the
        # already-built fake instance.
        return lambda: src


def _make_request(method: str, path: str, query: str) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": [],
    }
    return Request(scope)


@pytest.fixture
def collector() -> Iterator[tuple[Collector, CapturingRingWriter]]:
    ring = CapturingRingWriter()
    c = Collector(
        InMemoryStore(slow_factor=3.0),
        ring_writer=cast("MetricSnapshotStore", ring),
    )
    set_collector(c)
    try:
        yield c, ring
    finally:
        set_collector(None)
        current_source.set(None)
        current_request.set(None)


def _deps() -> dict[str, object]:
    return {
        "session": SessionManager.__new__(SessionManager),  # never used (no network)
        "ratelimiter": RateLimiter(),
        "handle_store": HandleStore(),
        "solver": None,
        "health_map": {},
        "session_prep": None,
        "enum_cache": EnumerationCache(),
        "failure_cooldown": SourceFailureCooldown(base_seconds=30, max_seconds=600),
        # Phase 16: the route now takes the pinned-proxy singleton; a pool-less
        # singleton is a no-op (no source opts in here → search/recent unchanged).
        "source_pins": SourcePinnedProxies(None),
    }


# ───────────────────────────── /search ───────────────────────────────────────


@pytest.mark.asyncio
async def test_search_emits_per_source_and_final_counts(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    c, ring = collector
    sources = {
        "alpha": _FakeSource("alpha", count=172, candidates=5, warn=None),
        "beta": _FakeSource("beta", count=5, candidates=2, warn=("rate", "slowed")),
    }
    registry = _FakeRegistry(sources)
    deps = _deps()
    req = SearchRequest(type="chapter", query="naruto", sources=["alpha", "beta"])
    http_request = _make_request("POST", "/api/v1/search", "x=1")

    token = current_request.set(
        {"request_id": 1, "surface": "search", "endpoint": "POST /search"}
    )
    try:
        resp = await search(
            req=req,
            request=http_request,
            session=cast("SessionManager", deps["session"]),
            ratelimiter=cast("RateLimiter", deps["ratelimiter"]),
            registry=cast("object", registry),  # type: ignore[arg-type]
            handle_store=cast("HandleStore", deps["handle_store"]),
            solver=cast("object", deps["solver"]),  # type: ignore[arg-type]
            health_map=cast("dict", deps["health_map"]),  # type: ignore[arg-type]
            session_prep=cast("object", deps["session_prep"]),  # type: ignore[arg-type]
            enum_cache=cast("EnumerationCache", deps["enum_cache"]),
            failure_cooldown=cast("SourceFailureCooldown", deps["failure_cooldown"]),
            source_pins=cast("SourcePinnedProxies", deps["source_pins"]),
        )
        # Umbrella request event (middleware emits this after the handler returns;
        # do it here with the stashed contextvar still live).
        assert isinstance(resp, ReleaseListResponse)
        c.emit_request(outcome="ok", duration_ms=1.0, status=200)
    finally:
        current_request.reset(token)

    final_count = len(resp.releases)
    assert final_count == req.limit  # 177 merged, truncated to 50

    req_ev = next(e for e in ring.iter_recent() if e.kind == "request")
    assert req_ev.result_count == final_count
    assert req_ev.warnings_summary == [{"source_key": "beta", "code": "rate"}]
    # Request blob round-trips.
    blob = req_ev.request_blob
    assert blob is not None
    assert blob["method"] == "POST"
    assert blob["path"] == "/api/v1/search"
    assert blob["query_string"] == "x=1"
    assert isinstance(blob["body"], dict)
    assert blob["body"]["query"] == "naruto"

    # Per-source summary events.
    src_evs = {e.source_key: e for e in ring.iter_recent() if e.kind == "source-result"}
    assert src_evs["alpha"].result_count == 172
    assert src_evs["alpha"].candidates_enumerated == 5
    assert src_evs["beta"].result_count == 5
    assert src_evs["beta"].candidates_enumerated == 2


@pytest.mark.asyncio
async def test_search_bad_request_still_stashes_request_blob(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """SRCH-05: a 400 bad_request (no query, no usable id) must STILL stash the
    request blob. The stash runs BEFORE the early return (CodeRabbit #129), so a
    malformed search is reconstructable from telemetry like any other request."""
    c, ring = collector
    registry = _FakeRegistry({})
    deps = _deps()
    req = SearchRequest(type="chapter")  # no query, no ids → not usable → 400
    http_request = _make_request("POST", "/api/v1/search", "")

    token = current_request.set(
        {"request_id": 9, "surface": "search", "endpoint": "POST /search"}
    )
    try:
        resp = await search(
            req=req,
            request=http_request,
            session=cast("SessionManager", deps["session"]),
            ratelimiter=cast("RateLimiter", deps["ratelimiter"]),
            registry=cast("object", registry),  # type: ignore[arg-type]
            handle_store=cast("HandleStore", deps["handle_store"]),
            solver=cast("object", deps["solver"]),  # type: ignore[arg-type]
            health_map=cast("dict", deps["health_map"]),  # type: ignore[arg-type]
            session_prep=cast("object", deps["session_prep"]),  # type: ignore[arg-type]
            enum_cache=cast("EnumerationCache", deps["enum_cache"]),
            failure_cooldown=cast("SourceFailureCooldown", deps["failure_cooldown"]),
            source_pins=cast("SourcePinnedProxies", deps["source_pins"]),
        )
        c.emit_request(outcome="client_error", duration_ms=1.0, status=400)
    finally:
        current_request.reset(token)

    # The early return fired (400), NOT a release list.
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400

    # ...but the umbrella request event still carries the reconstruction blob.
    req_ev = next(e for e in ring.iter_recent() if e.kind == "request")
    blob = req_ev.request_blob
    assert blob is not None
    assert blob["method"] == "POST"
    assert blob["path"] == "/api/v1/search"
    assert isinstance(blob["body"], dict)


# ───────────────────────────── /recent ───────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_emits_per_source_and_final_counts(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    c, ring = collector
    sources = {
        "alpha": _FakeSource("alpha", count=80, candidates=3, warn=None),
        "beta": _FakeSource("beta", count=40, candidates=1, warn=("deg", "partial")),
    }
    registry = _FakeRegistry(sources)
    deps = _deps()
    http_request = _make_request("GET", "/api/v1/recent", "sources=alpha&sources=beta")

    token = current_request.set(
        {"request_id": 2, "surface": "search", "endpoint": "GET /recent"}
    )
    try:
        resp = await get_recent(
            request=http_request,
            session=cast("SessionManager", deps["session"]),
            ratelimiter=cast("RateLimiter", deps["ratelimiter"]),
            registry=cast("object", registry),  # type: ignore[arg-type]
            handle_store=cast("HandleStore", deps["handle_store"]),
            solver=cast("object", deps["solver"]),  # type: ignore[arg-type]
            health_map=cast("dict", deps["health_map"]),  # type: ignore[arg-type]
            session_prep=cast("object", deps["session_prep"]),  # type: ignore[arg-type]
            failure_cooldown=cast("SourceFailureCooldown", deps["failure_cooldown"]),
            source_pins=cast("SourcePinnedProxies", deps["source_pins"]),
            sources=["alpha", "beta"],
            languages=None,
            limit=None,
            since=None,
        )
        c.emit_request(outcome="ok", duration_ms=1.0, status=200)
    finally:
        current_request.reset(token)

    # beta raised a SourceError (isolated by fan_out) → 0 releases; alpha's 80
    # survive (< the /recent max of 100, so no truncation here).
    final_count = len(resp.releases)
    assert final_count == 80

    req_ev = next(e for e in ring.iter_recent() if e.kind == "request")
    assert req_ev.result_count == final_count
    assert req_ev.warnings_summary == [{"source_key": "beta", "code": "deg"}]
    blob = req_ev.request_blob
    assert blob is not None
    assert blob["method"] == "GET"
    assert blob["path"] == "/api/v1/recent"
    assert blob["query_string"] == "sources=alpha&sources=beta"
    assert blob["body"] is None

    # alpha succeeded → its source-result event fired; beta raised before its emit.
    src_evs = {e.source_key: e for e in ring.iter_recent() if e.kind == "source-result"}
    assert src_evs["alpha"].result_count == 80
    assert src_evs["alpha"].candidates_enumerated == 3
    assert "beta" not in src_evs
