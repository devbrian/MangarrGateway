"""Metrics secret-redaction tests (Task 1 — SEC-01, D-03/D-04/D-05).

Asserts the shared denylist scrub:

* ``redact_url`` masks the gateway's own injected secrets in a forensic URL —
  ``cf_clearance`` / ``token`` / ``auth`` / ``apikey`` / ``x-csrf-token`` query
  values (case-insensitive substring match) — while KEEPING benign params.
* ``redact_url`` masks proxy *credentials* (``user:pass``) but KEEPS proxy
  ``host:port`` (D-04 — the load-bearing SEC-01 assertion; host:port is forensic
  signal, not a secret).
* ``redact_url`` is a no-op on falsy input (``None`` / ``""``).
* ``redact_text`` masks ``cf_clearance`` / ``Cookie`` / ``Authorization`` /
  ``x-csrf-token`` values and proxy creds in a freeform log line, leaving
  non-secret text intact (D-05).

Also pins the event schema (``MetricEvent`` carries all 14 fields incl. the added
``method``, is slots-based, round-trips through ``dataclasses.asdict``) and the two
attribution contextvars (``mg_request`` / ``mg_source``) + ``source_scope``.
"""

from __future__ import annotations

import dataclasses

from manga_gateway.metrics.context import (
    current_request,
    current_source,
    source_scope,
)
from manga_gateway.metrics.event import MetricEvent
from manga_gateway.metrics.redact import redact_text, redact_url


# ── redact_url: proxy creds (D-04) ───────────────────────────────────────────
def test_redact_url_masks_proxy_creds_keeps_host_port() -> None:
    # The load-bearing SEC-01 assertion: mask user:pass, KEEP host:port.
    assert redact_url("http://u:p@1.2.3.4:8080/x") == "http://***:***@1.2.3.4:8080/x"


# ── redact_url: denylist query keys (D-03) ───────────────────────────────────
def test_redact_url_masks_cf_clearance_keeps_benign_param() -> None:
    out = redact_url("https://h/api?cf_clearance=SECRET&q=foo")
    assert out is not None
    assert "SECRET" not in out
    assert "cf_clearance=%2A%2A%2A" in out or "cf_clearance=***" in out
    assert "q=foo" in out


def test_redact_url_masks_all_denylist_query_keys() -> None:
    out = redact_url("https://h/api?apikey=A&token=B&auth=C&X-CSRF-Token=D&keep=E")
    assert out is not None
    for secret in ("=A", "=B", "=C", "=D"):
        assert secret not in out
    assert "keep=E" in out


def test_redact_url_masks_x_api_key_query() -> None:
    # WR-01: the gateway's own master credential, supplied as ?apikey=/?x-api-key=.
    out = redact_url("https://h/api?x-api-key=MASTERKEY&keep=ok")
    assert out is not None
    assert "MASTERKEY" not in out
    assert "keep=ok" in out


def test_redact_url_noop_on_falsy() -> None:
    assert redact_url(None) is None
    assert redact_url("") == ""


# ── redact_text: log-line pass (D-05) ────────────────────────────────────────
def test_redact_text_masks_cf_clearance() -> None:
    line = "fetching cf_clearance=ABC123; done"
    out = redact_text(line)
    assert "ABC123" not in out
    assert "cf_clearance=***" in out


def test_redact_text_masks_cookie_authorization_csrf() -> None:
    line = "Cookie: SESSION=xyz Authorization: Bearer-tok X-CSRF-Token: csrf-val"
    out = redact_text(line)
    assert "xyz" not in out
    assert "Bearer-tok" not in out
    assert "csrf-val" not in out


# ── CR-01: scheme-prefixed Authorization + multi-pair Cookie fully masked ─────
def test_redact_text_masks_full_bearer_token() -> None:
    # Regression for CR-01: the OLD value class stopped at the first space, so
    # "Bearer <token>" masked only the literal word "Bearer" and leaked the token.
    line = "Authorization: Bearer tok_SECRET_value_here"
    out = redact_text(line)
    assert "tok_SECRET_value_here" not in out
    assert "SECRET" not in out


def test_redact_text_masks_all_cookie_pairs() -> None:
    # Regression for CR-01: a multi-pair Cookie line must mask EVERY pair, not
    # just the first (the old \s-terminated class leaked later pairs).
    line = "Cookie: a=secret1; b=secret2; c=secret3"
    out = redact_text(line)
    assert "secret1" not in out
    assert "secret2" not in out
    assert "secret3" not in out


def test_redact_text_masks_x_api_key_header() -> None:
    # WR-01: a log line echoing the gateway's own X-Api-Key must be masked.
    line = "X-Api-Key: realsecretkey123"
    out = redact_text(line)
    assert "realsecretkey123" not in out
    assert "X-Api-Key: ***" in out


def test_redact_text_masks_proxy_creds() -> None:
    line = "using proxy http://puser:ppass@10.0.0.1:3128 for fetch"
    out = redact_text(line)
    assert "puser" not in out
    assert "ppass" not in out
    assert "10.0.0.1:3128" in out  # host:port kept


def test_redact_text_leaves_benign_text_intact() -> None:
    line = "GET /search?q=naruto returned 200 in 35ms"
    assert redact_text(line) == line


def test_redact_text_masks_cf_clearance_case_insensitive() -> None:
    # CodeRabbit finding 5: Cf_Clearance= / CF_CLEARANCE= must mask too; the key
    # prefix is preserved with its original case, only the value is masked.
    for line in (
        "fetching Cf_Clearance=ABC123; done",
        "fetching CF_CLEARANCE=ABC123; done",
    ):
        out = redact_text(line)
        assert "ABC123" not in out
        assert "***" in out


def test_redact_text_header_value_stops_at_comma() -> None:
    # CodeRabbit finding 6: a comma after the secret terminates the mask so a
    # trailing comma-separated field is NOT over-redacted.
    line = 'Authorization: Bearer tok_SECRET, "next_field": "keepme"'
    out = redact_text(line)
    assert "tok_SECRET" not in out  # the token IS masked
    assert "keepme" in out  # the trailing comma-separated field is preserved


# ── MetricEvent schema ───────────────────────────────────────────────────────
def test_metric_event_has_all_fields_incl_method() -> None:
    field_names = {f.name for f in dataclasses.fields(MetricEvent)}
    expected = {
        "ts",
        "kind",
        "request_id",
        "surface",
        "endpoint",
        "source_key",
        "op",
        "method",
        "url",
        "status",
        "outcome",
        "duration_ms",
        "attempt",
        "error",
        # 260605-e9a payload-only enrichment fields (all default None).
        "request_blob",
        "result_count",
        "candidates_enumerated",
        "warnings_summary",
    }
    assert field_names == expected


def test_metric_event_is_slots_and_roundtrips() -> None:
    ev = MetricEvent(
        ts=1.0,
        kind="http",
        request_id=7,
        surface="search",
        endpoint="GET /search",
        source_key="comix",
        op="get_json",
        method="GET",
        url="https://h/x",
        status=200,
        outcome="ok",
        duration_ms=12.5,
        attempt=1,
    )
    # slots-based: no __dict__
    assert not hasattr(ev, "__dict__")
    d = dataclasses.asdict(ev)
    assert d["method"] == "GET"
    assert d["request_id"] == 7
    assert d["error"] is None


# ── contextvars + source_scope ───────────────────────────────────────────────
def test_contextvar_names() -> None:
    assert current_request.name == "mg_request"
    assert current_source.name == "mg_source"


def test_source_scope_sets_and_resets() -> None:
    assert current_source.get() is None
    with source_scope("comix"):
        assert current_source.get() == "comix"
    assert current_source.get() is None
