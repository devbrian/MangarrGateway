"""Request-blob capture + per-source summary + browser emit (260605-e9a Task 1).

Drives the collector emit catalog over a synchronous ``CapturingRingWriter`` (the
same stand-in the seam tests use) with a real ``Collector`` installed via
``set_collector``, so each emitted event is read back without aiosqlite.

Covers the four NEW payload-only ``MetricEvent`` fields (all default ``None`` so
asdict round-trips unchanged for every existing emit):

* ``request_blob`` — {method, path, query_string, body} rebuilds the original
  request 1:1 EXCEPT headers; secrets redacted at stash time; body size-capped.
* ``result_count`` / ``warnings_summary`` — ride the umbrella ``request`` event.
* ``candidates_enumerated`` — rides a per-source ``source-result`` event.

Plus the two new emit verbs: ``emit_source_result`` (kind=source-result,
self-attributed via ``current_source``) and ``emit_browser`` (kind=browser, url
redacted at the ingest boundary).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest

from manga_gateway.metrics.collector import Collector, set_collector
from manga_gateway.metrics.context import (
    REQUEST_BLOB_BODY_CAP,
    current_request,
    current_source,
    source_scope,
    stash_request_blob,
    stash_request_result,
)
from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.redact import redact_blob
from manga_gateway.metrics.snapshot import MetricSnapshotStore
from manga_gateway.models.metrics import RequestBlob, WarningSummaryItem

from ._metrics_helpers import CapturingRingWriter


@pytest.fixture
def collector() -> Iterator[tuple[Collector, CapturingRingWriter]]:
    ring = CapturingRingWriter()
    c = Collector(
        InMemoryStore_(),
        ring_writer=cast("MetricSnapshotStore", ring),
    )
    set_collector(c)
    try:
        yield c, ring
    finally:
        set_collector(None)
        current_source.set(None)
        current_request.set(None)


def InMemoryStore_() -> object:  # noqa: N802 - tiny indirection to keep imports local
    from manga_gateway.metrics.store import InMemoryStore

    return InMemoryStore(slow_factor=3.0)


def _request_scope(request_id: int = 1) -> dict[str, object]:
    return {
        "request_id": request_id,
        "surface": "search",
        "endpoint": "POST /search",
    }


# ─────────────────────────── MetricEvent shape ───────────────────────────────


def test_metric_event_new_fields_default_none() -> None:
    """The four new payload fields default None so existing positional/asdict
    round-trips are unchanged."""
    ev = MetricEvent(
        ts=1.0,
        kind="http",
        request_id=1,
        surface="search",
        endpoint="POST /search",
        source_key="comix",
        op="get_json",
        method="GET",
        url="https://h/x",
        status=200,
        outcome="ok",
        duration_ms=1.0,
        attempt=1,
    )
    assert ev.request_blob is None
    assert ev.result_count is None
    assert ev.candidates_enumerated is None
    assert ev.warnings_summary is None


# ─────────────────────────── drift guards (model ⇔ producer) ─────────────────


def test_request_blob_shape_lockstep_with_model() -> None:
    """RequestBlob ⇔ stash_request_blob keyset parity (the drift guard).

    Drives the REAL ``stash_request_blob`` producer for BOTH an untruncated and a
    truncated body, reads the stored dict back from ``current_request``, and asserts
    its keyset equals ``RequestBlob.model_fields`` (body_truncated present, and
    True/False on the respective path). A drift fails with an actionable message
    naming the offending key(s) AND the two files to keep in sync.
    """
    model_keys = set(RequestBlob.model_fields)

    # — untruncated body: body_truncated must be present and False —
    token = current_request.set(_request_scope())
    try:
        stash_request_blob(
            method="POST",
            path="/api/v1/search",
            query_string="x=1",
            body={"type": "chapter", "query": "naruto"},
        )
        stored = current_request.get()["request_blob"]  # type: ignore[index]
    finally:
        current_request.reset(token)
    assert isinstance(stored, dict)
    assert set(stored) == model_keys, (
        f"RequestBlob/stash_request_blob keyset drift: {sorted(set(stored) ^ model_keys)}. "
        "Keep src/manga_gateway/metrics/context.py::stash_request_blob and "
        "src/manga_gateway/models/metrics.py::RequestBlob in sync."
    )
    assert stored["body_truncated"] is False

    # — truncated body: same keyset, body_truncated True —
    token = current_request.set(_request_scope())
    try:
        stash_request_blob(
            method="POST",
            path="/api/v1/search",
            query_string="",
            body={"q": "x" * (REQUEST_BLOB_BODY_CAP + 5000)},
        )
        stored_big = current_request.get()["request_blob"]  # type: ignore[index]
    finally:
        current_request.reset(token)
    assert isinstance(stored_big, dict)
    assert set(stored_big) == model_keys, (
        f"RequestBlob/stash_request_blob keyset drift (truncated path): "
        f"{sorted(set(stored_big) ^ model_keys)}. Keep "
        "src/manga_gateway/metrics/context.py::stash_request_blob and "
        "src/manga_gateway/models/metrics.py::RequestBlob in sync."
    )
    assert stored_big["body_truncated"] is True


def test_warning_summary_item_shape_lockstep() -> None:
    """WarningSummaryItem ⇔ the search.py/recent.py producer shape (drift guard).

    The producers build ``{"source_key": key, "code": code}`` from the warning
    tuples (no shared helper to import), so assert the model field set against that
    literal shape plus a validating round-trip. A drift fails with an actionable
    message naming the producers AND the model.
    """
    assert set(WarningSummaryItem.model_fields) == {"source_key", "code"}, (
        f"WarningSummaryItem shape drift: "
        f"{sorted(set(WarningSummaryItem.model_fields) ^ {'source_key', 'code'})}. "
        "Keep src/manga_gateway/api/routes/search.py + recent.py warning-summary "
        "producers and src/manga_gateway/models/metrics.py::WarningSummaryItem in sync."
    )
    item = WarningSummaryItem.model_validate({"source_key": "comix", "code": "timeout"})
    assert item.model_dump() == {"source_key": "comix", "code": "timeout"}


# ─────────────────────────── request-blob round-trip ─────────────────────────


def test_request_blob_roundtrips_on_request_event(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """stash_request_blob → emit_request carries {method, path, query_string, body}
    on the request event; re-deriving the request from the blob equals the original
    (headers excluded — never captured)."""
    c, ring = collector
    token = current_request.set(_request_scope())
    try:
        body = {"type": "chapter", "query": "naruto", "sources": ["comix"]}
        stash_request_blob(
            method="POST", path="/api/v1/search", query_string="x=1", body=body
        )
        c.emit_request(outcome="ok", duration_ms=12.0, status=200)
    finally:
        current_request.reset(token)

    reqs = [e for e in ring.iter_recent() if e.kind == "request"]
    assert len(reqs) == 1
    blob = reqs[0].request_blob
    assert blob is not None
    assert blob["method"] == "POST"
    assert blob["path"] == "/api/v1/search"
    assert blob["query_string"] == "x=1"
    assert blob["body"] == body
    # Headers are never captured.
    assert "headers" not in blob


def test_request_blob_redacts_secret_in_query_and_body(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """An X-Api-Key in query_string AND a token in the body are masked to *** — the
    real secret never enters the stored blob."""
    c, ring = collector
    token = current_request.set(_request_scope())
    try:
        stash_request_blob(
            method="POST",
            path="/api/v1/search",
            query_string="apikey=SUPERSECRET&q=ok",
            body={"query": "x", "authorization": "Bearer LEAK", "ok": "keep"},
        )
        c.emit_request(outcome="ok", duration_ms=1.0, status=200)
    finally:
        current_request.reset(token)

    blob = next(e for e in ring.iter_recent() if e.kind == "request").request_blob
    assert blob is not None
    assert "SUPERSECRET" not in blob["query_string"]
    # _mask_query urlencodes the *** masked value (matches the SEC-01 served form).
    assert "%2A%2A%2A" in blob["query_string"]
    assert "q=ok" in blob["query_string"]
    body = blob["body"]
    assert isinstance(body, dict)
    assert body["authorization"] == "***"
    assert "LEAK" not in str(body)
    assert body["ok"] == "keep"


def test_request_blob_body_size_capped_and_flagged(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """A body whose serialized size exceeds the cap is truncated + flagged."""
    c, ring = collector
    token = current_request.set(_request_scope())
    try:
        big = {"query": "x" * (REQUEST_BLOB_BODY_CAP + 5000)}
        stash_request_blob(
            method="POST", path="/api/v1/search", query_string="", body=big
        )
        c.emit_request(outcome="ok", duration_ms=1.0, status=200)
    finally:
        current_request.reset(token)

    blob = next(e for e in ring.iter_recent() if e.kind == "request").request_blob
    assert blob is not None
    assert blob.get("body_truncated") is True
    # The serialized body is bounded by the cap.
    import json

    assert len(json.dumps(blob["body"])) <= REQUEST_BLOB_BODY_CAP + 256


def test_request_blob_redaction_survives_truncation(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """Regression (CodeRabbit #129): an OVERSIZED dict body must STILL be redacted.

    Redaction runs before the size cap, so a denylisted key is masked even when the
    body is too large to keep verbatim — the raw secret value must never appear in
    the blob. (The prior order truncated the dict to a string first, which let
    redact_blob silently no-op on the now-non-dict body.)
    """
    c, ring = collector
    token = current_request.set(_request_scope())
    try:
        # Secret key first (dict insertion order) so its masked value lands within
        # the cap; the oversized filler forces truncation to a string.
        big_secret_body = {
            "authorization": "SUPERSECRETVALUE",
            "filler": "x" * (REQUEST_BLOB_BODY_CAP + 5000),
        }
        stash_request_blob(
            method="POST",
            path="/api/v1/search",
            query_string="",
            body=big_secret_body,
        )
        c.emit_request(outcome="ok", duration_ms=1.0, status=200)
    finally:
        current_request.reset(token)

    blob = next(e for e in ring.iter_recent() if e.kind == "request").request_blob
    assert blob is not None
    assert blob.get("body_truncated") is True
    # The raw secret never survives, even though the body was truncated to a string.
    assert "SUPERSECRETVALUE" not in str(blob["body"])
    assert "***" in str(blob["body"])


def test_stash_request_result_rides_request_event(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """stash_request_result populates result_count + warnings_summary on the
    umbrella request event."""
    c, ring = collector
    token = current_request.set(_request_scope())
    try:
        stash_request_result(
            result_count=42,
            warnings_summary=[{"source_key": "comix", "code": "timeout"}],
        )
        c.emit_request(outcome="ok", duration_ms=1.0, status=200)
    finally:
        current_request.reset(token)

    ev = next(e for e in ring.iter_recent() if e.kind == "request")
    assert ev.result_count == 42
    assert ev.warnings_summary == [{"source_key": "comix", "code": "timeout"}]


def test_stash_helpers_noop_without_request_scope(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """Stash helpers are no-ops when no current_request is set (defensive)."""
    current_request.set(None)
    # Must not raise.
    stash_request_blob(method="GET", path="/x", query_string="", body=None)
    stash_request_result(result_count=1, warnings_summary=[])


# ─────────────────────────── emit_source_result ──────────────────────────────


def test_emit_source_result_records_counts_under_source(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """emit_source_result records result_count + candidates_enumerated under the
    source bound by current_source (kind=source-result)."""
    c, ring = collector
    token = current_request.set(_request_scope())
    try:
        with source_scope("comix"):
            c.emit_source_result(result_count=172, candidates_enumerated=5)
    finally:
        current_request.reset(token)

    evs = [e for e in ring.iter_recent() if e.kind == "source-result"]
    assert len(evs) == 1
    ev = evs[0]
    assert ev.source_key == "comix"
    assert ev.result_count == 172
    assert ev.candidates_enumerated == 5
    assert ev.op == "result"


# ─────────────────────────── emit_browser ────────────────────────────────────


def test_emit_browser_records_redacted_url(
    collector: tuple[Collector, CapturingRingWriter],
) -> None:
    """emit_browser records a kind=browser event with the url redacted at the ingest
    boundary."""
    c, ring = collector
    token = current_request.set(_request_scope())
    try:
        with source_scope("comix"):
            c.emit_browser(
                op="fetch_via_browser",
                url="https://comix.to/title/abc?cf_clearance=LEAKED",
                status=None,
                outcome="ok",
                duration_ms=234.0,
                attempt=1,
            )
    finally:
        current_request.reset(token)

    evs = [e for e in ring.iter_recent() if e.kind == "browser"]
    assert len(evs) == 1
    ev = evs[0]
    assert ev.source_key == "comix"
    assert ev.op == "fetch_via_browser"
    assert ev.outcome == "ok"
    assert ev.attempt == 1
    assert ev.url is not None
    assert "LEAKED" not in ev.url


# ─────────────────────────── redact_blob unit ────────────────────────────────


def test_redact_blob_masks_denylisted_keys() -> None:
    """redact_blob masks denylisted query + body keys, reusing the single denylist."""
    out = redact_blob(
        {
            "method": "POST",
            "path": "/x",
            "query_string": "x-api-key=SECRET&keep=1",
            "body": {"token": "T", "nested_keep": "v"},
        }
    )
    assert "SECRET" not in out["query_string"]
    assert "%2A%2A%2A" in out["query_string"]
    assert "keep=1" in out["query_string"]
    assert out["body"]["token"] == "***"
    assert out["body"]["nested_keep"] == "v"


def test_redact_blob_handles_none_body() -> None:
    """A GET request blob (body=None) redacts cleanly."""
    out = redact_blob(
        {"method": "GET", "path": "/recent", "query_string": "auth=X", "body": None}
    )
    assert "X" not in out["query_string"]
    assert out["body"] is None
