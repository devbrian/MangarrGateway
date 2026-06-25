"""SolverService control logic driven against a FAKE solve pipeline.

No real adb / redroid / WebView is touched. Asserts the SEC-01 guarantees:
api-key auth (T-10-08), the host allowlist SSRF guard (T-10-09), serialized
solves + timeout (T-10-11), and that the token value never reaches the logs
(T-10-10). Also covers the env-driven config refusing to start keyless.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time
from collections.abc import Iterator

import pytest
from android_solver.cdp import ClearanceCookie
from android_solver.config import ConfigError, SidecarConfig
from android_solver.device import AdbError
from android_solver.service import (
    AndroidSolvePipeline,
    SolveCancelled,
    SolveError,
    SolveResult,
    SolverService,
    _Handler,
    _SolverHTTPServer,
)

from android_solver import service

# A fixed, non-sensitive cookie expiry (epoch seconds) the extract stubs mint so tests
# can assert it threads through SolveResult → the /solve payload.
_STUB_EXPIRES = 2000000000.0


class FakePipeline:
    """Records calls; optionally delays / raises / reports max concurrency."""

    def __init__(
        self,
        *,
        result: SolveResult | None = None,
        delay: float = 0.0,
        healthy: bool = True,
        error: Exception | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        eval_result: object = None,
        eval_clearance: SolveResult | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.proxies: list[dict[str, object] | None] = []
        # /eval call recording (Phase 14): the js of each eval, so tests can assert
        # the pipeline was (or was NOT) driven without leaking it elsewhere.
        self.eval_js: list[str] = []
        # The proxy each /eval was driven with (Req 7 parity with ``solve`` →
        # ``self.proxies``): a separate list so the eval-vs-solve serialization test's
        # shared counters never conflate solve-proxy and eval-proxy assertions.
        self.eval_proxies: list[dict[str, object] | None] = []
        self._result = result
        # The value /eval marshals back; parametrizable to a large list for the A4
        # no-truncation regression. Defaults to a small dict.
        self._eval_result = eval_result if eval_result is not None else {"ok": True}
        # Bug 5 follow-on #3: the SolveResult the eval-with-clearance path returns
        # alongside the value (``(value, clearance)``). ``None`` models a clean clear
        # that deposited no host-scoped cookie.
        self._eval_clearance = eval_clearance
        # Records whether each eval requested clearance extraction (so a test can assert
        # the flag threaded through ``_run_eval`` → the pipeline).
        self.eval_with_clearance: list[bool] = []
        self.eval_wait_for: list[str | None] = []
        self._delay = delay
        self._healthy = healthy
        self._error = error
        # Optional gating for the 503-on-busy serialization test (#275): the worker
        # signals ``started`` once it is in flight under the service lock and blocks on
        # ``release`` so the test can deterministically fire a SECOND /solve mid-flight.
        self._started = started
        self._release = release
        self._counter_lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0
        self.cancelled = False

    def solve(
        self,
        challenge_url: str,
        host: str,
        cancel: threading.Event | None = None,
        *,
        proxy: dict[str, object] | None = None,
    ) -> SolveResult:
        with self._counter_lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            self.calls.append((challenge_url, host))
            self.proxies.append(proxy)
            if self._started is not None:
                self._started.set()
            if self._release is not None:
                # Block (cooperatively cancellable) until the test releases us.
                while not self._release.wait(timeout=0.02):
                    if cancel is not None and cancel.is_set():
                        self.cancelled = True
                        raise SolveCancelled("cancelled after timeout")
            if self._delay:
                # Cooperative: poll the cancel signal in small steps (mirrors the
                # real pipeline's adb/CDP checkpoints) so a timed-out solve frees
                # the worker promptly instead of running the full delay (#207).
                waited = 0.0
                step = 0.02
                while waited < self._delay:
                    if cancel is not None and cancel.is_set():
                        self.cancelled = True
                        raise SolveCancelled("cancelled after timeout")
                    time.sleep(step)
                    waited += step
            if self._error is not None:
                raise self._error
            return self._result or SolveResult(
                cf_clearance="MANGADOT_TOKEN",
                user_agent="Mozilla/5.0 (Android 11) WebView wv",
                host=host,
            )
        finally:
            with self._counter_lock:
                self._active -= 1

    def eval_in_webview(
        self,
        challenge_url: str,
        host: str,
        js: str,
        cancel: threading.Event | None = None,
        *,
        wait_for: str | None = None,
        deadline: float | None = None,
        proxy: dict[str, object] | None = None,
        with_clearance: bool = False,
    ) -> object:
        # Mirrors ``solve``'s recording + gating (shared counters/events) so the
        # eval-vs-solve serialization test can hold the lock with EITHER endpoint.
        with self._counter_lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            self.calls.append((challenge_url, host))
            self.eval_js.append(js)
            self.eval_proxies.append(proxy)
            self.eval_with_clearance.append(with_clearance)
            self.eval_wait_for.append(wait_for)
            if self._started is not None:
                self._started.set()
            if self._release is not None:
                while not self._release.wait(timeout=0.02):
                    if cancel is not None and cancel.is_set():
                        self.cancelled = True
                        raise SolveCancelled("cancelled after timeout")
            if self._delay:
                waited = 0.0
                step = 0.02
                while waited < self._delay:
                    if cancel is not None and cancel.is_set():
                        self.cancelled = True
                        raise SolveCancelled("cancelled after timeout")
                    time.sleep(step)
                    waited += step
            if self._error is not None:
                raise self._error
            if with_clearance:
                return (self._eval_result, self._eval_clearance)
            return self._eval_result
        finally:
            with self._counter_lock:
                self._active -= 1

    def health(self) -> bool:
        return self._healthy


def _config(**overrides: object) -> SidecarConfig:
    base: dict[str, object] = {
        "api_key": "s3cret-solver-key",
        "adb_target": "redroid:5555",
        "allowed_hosts": frozenset({"mangadot.net"}),
        "solve_timeout_s": 5.0,
    }
    base.update(overrides)
    return SidecarConfig(**base)  # type: ignore[arg-type]


def _service(pipeline: FakePipeline, **cfg: object) -> SolverService:
    return SolverService(_config(**cfg), pipeline)


@contextlib.contextmanager
def _running_server(pipeline: FakePipeline) -> Iterator[int]:
    """Serve ``pipeline`` over the real stdlib ``_Handler`` on an ephemeral port.

    Yields the bound port so a test can drive the actual HTTP transport (the
    ``do_POST`` pre-auth + body-cap rails and the JSON response writer), then
    shuts the server down. Used by the /eval transport + A4 large-result tests.
    """
    srv = _SolverHTTPServer(("127.0.0.1", 0), _Handler, service=_service(pipeline))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


# ── auth (T-10-08) ───────────────────────────────────────────────────────────


def test_solve_rejects_missing_key() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).solve(api_key=None, body=b"{}")
    assert status == 401
    assert pipeline.calls == []  # no device action on an unauthenticated call


def test_solve_rejects_wrong_key() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).solve(
        api_key="not-the-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 401
    assert pipeline.calls == []


# ── SSRF allowlist (T-10-09) ─────────────────────────────────────────────────


def test_solve_rejects_non_allowlisted_host() -> None:
    pipeline = FakePipeline()
    status, payload = _service(pipeline).solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://evil.example/"}',
    )
    assert status == 422
    assert pipeline.calls == []  # the device pipeline is NEVER invoked
    assert "allowlist" in payload["error"]


@pytest.mark.parametrize(
    "challenge_url",
    [
        b'{"challenge_url": "file://mangadot.net/etc/passwd"}',
        b'{"challenge_url": "intent://mangadot.net/#Intent;end"}',
        b'{"challenge_url": "javascript://mangadot.net/%0aalert(1)"}',
        b'{"challenge_url": "content://mangadot.net/data"}',
    ],
)
def test_solve_rejects_non_http_scheme_even_for_allowlisted_host(
    challenge_url: bytes,
) -> None:
    # WR-01: an allowlisted HOST with a non-http(s) SCHEME must still be rejected
    # before any device action — the scheme is pinned ahead of the host check.
    pipeline = FakePipeline()
    status, payload = _service(pipeline).solve(
        api_key="s3cret-solver-key", body=challenge_url
    )
    assert status == 422
    assert pipeline.calls == []  # never reaches am start -d
    assert "scheme" in payload["error"]


def test_solve_requires_challenge_url() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).solve(api_key="s3cret-solver-key", body=b"{}")
    assert status == 422
    assert pipeline.calls == []


def test_solve_rejects_malformed_body() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).solve(api_key="s3cret-solver-key", body=b"not json")
    assert status == 400
    assert pipeline.calls == []


# ── happy path ───────────────────────────────────────────────────────────────


def test_solve_returns_clearance_for_allowlisted_host() -> None:
    pipeline = FakePipeline()
    status, payload = _service(pipeline).solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 200
    assert payload == {
        "cf_clearance": "MANGADOT_TOKEN",
        "user_agent": "Mozilla/5.0 (Android 11) WebView wv",
        "host": "mangadot.net",
        "egress_ip": "",  # additive-only on the no-proxy path (D-08)
        "cf_clearance_expires": None,  # additive-only; null when none minted (D-08)
    }
    assert pipeline.calls == [("https://mangadot.net/", "mangadot.net")]


# ── per-solve proxy validation (Req 1 / T-11-06) ─────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        # proxy is not a dict
        b'{"challenge_url": "https://mangadot.net/", "proxy": "http://p:1"}',
        b'{"challenge_url": "https://mangadot.net/", "proxy": 5}',
        # server missing / not a str
        b'{"challenge_url": "https://mangadot.net/", "proxy": {}}',
        b'{"challenge_url": "https://mangadot.net/", "proxy": {"server": 5}}',
        b'{"challenge_url": "https://mangadot.net/", "proxy": {"server": ""}}',
        # server scheme not http/https
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "socks5://p:1"}}',
        # server has no hostname
        b'{"challenge_url": "https://mangadot.net/", "proxy": {"server": "http://"}}',
        # WR-04: server port out of range — must be a PRE-device 422, not a 504.
        # Without port validation in _validate_proxy this body passes validation and
        # later raises ValueError in _proxy_parts (worker thread) → misleading 504.
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "http://host:99999"}}',
        # username/password present but not a str
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "http://p:1", "username": 7}}',
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "http://p:1", "password": 7}}',
        # CR-2: a half-specified credential pair (only one of user/pass) would
        # silently fall back to an unauthenticated upstream and 504 mid-pipeline —
        # must be a PRE-device 422 instead.
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "http://p:1", "username": "u"}}',
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "http://p:1", "password": "p"}}',
    ],
)
def test_solve_rejects_malformed_proxy_before_any_device_action(body: bytes) -> None:
    # Req 1 / T-11-06: a malformed proxy is a PRE-action 422 — the device pipeline
    # is never invoked (no hop repoint, no global http_proxy set).
    pipeline = FakePipeline()
    status, payload = _service(pipeline).solve(api_key="s3cret-solver-key", body=body)
    assert status == 422
    assert pipeline.calls == []  # 422 is pre-action — no device action
    assert "proxy" in payload["error"]


def test_solve_forwards_wellformed_proxy_to_pipeline() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).solve(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://mangadot.net/",'
            b' "proxy": {"server": "http://up.example:8080",'
            b' "username": "u", "password": "p"}}'
        ),
    )
    assert status == 200
    assert pipeline.proxies == [
        {"server": "http://up.example:8080", "username": "u", "password": "p"}
    ]


def test_solve_without_proxy_passes_none_to_pipeline() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 200
    assert pipeline.proxies == [None]


# ── D-07: proxied solves use the longer proxy_solve_timeout_s ─────────────────


def test_proxied_solve_uses_proxy_timeout_not_base() -> None:
    # D-07 / Pitfall 6: with a proxy present the future is bounded by the LONGER
    # proxy_solve_timeout_s. The pipeline delay sits BETWEEN the base (tiny) and
    # the proxy (generous) timeout, so the proxied solve completes (200) where a
    # base-timeout solve would 504.
    pipeline = FakePipeline(delay=0.3)
    service = _service(
        pipeline,
        solve_timeout_s=0.05,
        proxy_solve_timeout_s=5.0,
        cancel_grace_s=2.0,
    )
    status, _ = service.solve(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://mangadot.net/",'
            b' "proxy": {"server": "http://up.example:8080"}}'
        ),
    )
    assert status == 200


def test_no_proxy_solve_uses_base_timeout_and_times_out() -> None:
    # The same delay under the base (tiny) timeout and NO proxy must 504 — proving
    # the timeout selection keys off proxy-presence, not a constant.
    pipeline = FakePipeline(delay=0.3)
    service = _service(
        pipeline,
        solve_timeout_s=0.05,
        proxy_solve_timeout_s=5.0,
        cancel_grace_s=2.0,
    )
    status, _ = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 504


# ── Req 6 + D-08: egress_ip ships in every payload, no-proxy unchanged ─────────


def test_no_proxy_payload_is_today_shape_plus_empty_egress_ip() -> None:
    # D-08 regression: a no-proxy /solve invokes the pipeline with proxy=None and
    # returns the today-shape payload keys PLUS an additive empty egress_ip — i.e.
    # the no-proxy behaviour is unchanged except for the additive field.
    pipeline = FakePipeline()
    status, payload = _service(pipeline).solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 200
    assert pipeline.proxies == [None]  # pipeline driven with proxy=None
    assert set(payload) == {
        "cf_clearance",
        "user_agent",
        "host",
        "egress_ip",
        "cf_clearance_expires",
    }
    assert payload["egress_ip"] == ""  # empty on the no-proxy path
    assert payload["cf_clearance_expires"] is None  # default when none minted


def test_proxied_payload_carries_verified_egress_ip() -> None:
    # Req 6: the verified egress on a proxied solve reaches the 200 payload.
    pipeline = FakePipeline(
        result=SolveResult(
            cf_clearance="MANGADOT_TOKEN",
            user_agent="UA-wv",
            host="mangadot.net",
            egress_ip="203.0.113.7",
        )
    )
    status, payload = _service(pipeline, proxy_solve_timeout_s=5.0).solve(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://mangadot.net/",'
            b' "proxy": {"server": "http://up.example:8080"}}'
        ),
    )
    assert status == 200
    assert payload["egress_ip"] == "203.0.113.7"


# ── serialization + timeout (T-10-11) ────────────────────────────────────────


def test_solves_are_serialized() -> None:
    # #275 req 2: a /solve arriving while one is already in flight is rejected 503 by
    # the NON-blocking lock — it does NOT queue behind the single device. The first
    # solve is gated so the second deterministically arrives mid-flight.
    started = threading.Event()
    release = threading.Event()
    pipeline = FakePipeline(started=started, release=release)
    service = _service(pipeline, solve_timeout_s=5.0)
    first_status: dict[str, int] = {}

    def first() -> None:
        status, _ = service.solve(
            api_key="s3cret-solver-key",
            body=b'{"challenge_url": "https://mangadot.net/"}',
        )
        first_status["status"] = status

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert started.wait(timeout=5)  # the first solve is in flight under the lock
        # A second /solve arriving now is rejected 503 — not queued behind the device.
        second, payload = service.solve(
            api_key="s3cret-solver-key",
            body=b'{"challenge_url": "https://mangadot.net/"}',
        )
        assert second == 503
        assert "busy" in payload["error"]
    finally:
        release.set()  # let the first solve finish
        thread.join(timeout=5)

    assert first_status["status"] == 200
    # The single device was never driven by two solves at once.
    assert pipeline.max_concurrent == 1


def test_disconnected_caller_cancels_inflight_solve() -> None:
    # #275 req 1: a caller that abandons the request mid-flight fires the cancel Event
    # and the solve returns 499 PROMPTLY — well under the (long) solve delay — instead
    # of grinding the full deadline while the single device stays pinned.
    pipeline = FakePipeline(delay=10.0)
    service = _service(pipeline, solve_timeout_s=30.0, cancel_grace_s=5.0)
    polls = {"n": 0}

    def disconnected() -> bool:
        # Connected on the first poll, then the peer is gone.
        polls["n"] += 1
        return polls["n"] > 1

    start = time.monotonic()
    status, payload = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
        disconnected=disconnected,
    )
    elapsed = time.monotonic() - start
    assert status == 499
    assert payload == {"error": "client disconnected"}
    assert pipeline.cancelled is True  # the worker saw the cancel signal
    assert elapsed < 10.0  # cancelled promptly, not after the full 10s delay


def test_slow_pipeline_times_out() -> None:
    pipeline = FakePipeline(delay=1.0)
    service = _service(pipeline, solve_timeout_s=0.1)
    status, payload = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 504
    assert "error" in payload


def test_pipeline_failure_is_504() -> None:
    pipeline = FakePipeline(error=RuntimeError("checkbox not located"))
    status, _ = _service(pipeline).solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 504


# ── cooperative cancellation on timeout (issue #207) ─────────────────────────


def test_timed_out_solve_is_cancelled_so_worker_frees_promptly() -> None:
    # AC1 (#207): a solve that exceeds the timeout is cooperatively cancelled —
    # the worker observes the signal and exits well within the grace window
    # rather than running its full (10s) work and pinning the device.
    pipeline = FakePipeline(delay=10.0)
    service = _service(pipeline, solve_timeout_s=0.1, cancel_grace_s=5.0)
    start = time.monotonic()
    status, payload = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    elapsed = time.monotonic() - start
    assert status == 504
    assert payload == {"error": "solve timed out"}
    assert pipeline.cancelled is True  # the worker saw the cancel signal
    assert elapsed < 5.0  # freed within the grace, not the full 10s delay


def test_next_solve_not_starved_by_timed_out_orphan() -> None:
    # AC2 (#207): after a solve times out and is cancelled, the NEXT /solve must
    # proceed promptly on the freed single worker — no cascading 504s behind a
    # still-running orphan.
    pipeline = FakePipeline(delay=10.0)
    service = _service(pipeline, solve_timeout_s=0.1, cancel_grace_s=5.0)
    first, _ = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert first == 504  # timed out → cancelled → worker freed

    pipeline._delay = 0.0  # the orphan is gone; the next solve is fast
    start = time.monotonic()
    second, payload = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    elapsed = time.monotonic() - start
    assert second == 200
    assert payload["cf_clearance"] == "MANGADOT_TOKEN"
    assert elapsed < 5.0  # not queued behind the cancelled orphan


# ── /eval (Phase 14: EVAL-01 / SEC-01) ───────────────────────────────────────
#
# /eval reuses /solve's rails verbatim — X-Solver-Key auth before any device
# action (T-14-01), the scheme-pinned-then-host-allowlisted SSRF guard before any
# device action (T-14-02) — and adds a js parse (T-14-03). These assert each
# rejection path takes NO device action (pipeline.calls == []), that an eval and a
# solve are mutually exclusive against the one device, and that a large result
# round-trips intact (A4). The fake pipeline records the js but the tests never
# assert it reaches a log (secret discipline, T-14-04).


def test_eval_rejects_missing_key() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).eval(api_key=None, body=b"{}")
    assert status == 401
    assert pipeline.calls == []  # no device action on an unauthenticated /eval


def test_eval_rejects_non_allowlisted_host() -> None:
    pipeline = FakePipeline()
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://evil.example/", "js": "1+1"}',
    )
    assert status == 422
    assert pipeline.calls == []  # the device pipeline is NEVER invoked
    assert "allowlist" in payload["error"]


@pytest.mark.parametrize(
    "body",
    [
        b'{"challenge_url": "file://mangadot.net/etc/passwd", "js": "1+1"}',
        b'{"challenge_url": "intent://mangadot.net/#Intent;end", "js": "1+1"}',
        b'{"challenge_url": "content://mangadot.net/data", "js": "1+1"}',
    ],
)
def test_eval_rejects_non_http_scheme_even_for_allowlisted_host(body: bytes) -> None:
    # T-14-02: an allowlisted HOST with a non-http(s) SCHEME is still rejected
    # before any device action — the scheme is pinned ahead of the host check.
    pipeline = FakePipeline()
    status, payload = _service(pipeline).eval(api_key="s3cret-solver-key", body=body)
    assert status == 422
    assert pipeline.calls == []  # never navigates
    assert "scheme" in payload["error"]


@pytest.mark.parametrize(
    "body",
    [
        b'{"challenge_url": "https://mangadot.net/"}',  # js absent
        b'{"challenge_url": "https://mangadot.net/", "js": ""}',  # js empty
        b'{"challenge_url": "https://mangadot.net/", "js": 5}',  # js not a str
    ],
)
def test_eval_rejects_missing_or_empty_js(body: bytes) -> None:
    # T-14-03: js is required — a missing/empty/non-str js is a pre-device 422.
    pipeline = FakePipeline()
    status, payload = _service(pipeline).eval(api_key="s3cret-solver-key", body=body)
    assert status == 422
    assert pipeline.calls == []  # never reaches the device
    assert "js" in payload["error"]


def test_eval_requires_challenge_url() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).eval(
        api_key="s3cret-solver-key", body=b'{"js": "1+1"}'
    )
    assert status == 422
    assert pipeline.calls == []


def test_eval_rejects_malformed_body() -> None:
    pipeline = FakePipeline()
    status, _ = _service(pipeline).eval(api_key="s3cret-solver-key", body=b"not json")
    assert status == 400
    assert pipeline.calls == []


def test_eval_returns_marshalled_value_on_success() -> None:
    pipeline = FakePipeline(eval_result={"chapters": [1, 2, 3]})
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "extract()"}',
    )
    assert status == 200
    assert payload == {"value": {"chapters": [1, 2, 3]}}
    assert pipeline.calls == [("https://mangadot.net/", "mangadot.net")]
    assert pipeline.eval_js == ["extract()"]


def test_eval_throw_surfaces_detail_in_body() -> None:
    # Mode-E follow-up: a rejected in-page promise (an ``EvalError``, e.g. a
    # comix-origin 5xx surfaced by axios) returns 504 with the bounded, token-free
    # summary under ``detail`` — so the GATEWAY can distinguish a transient origin 5xx
    # (retryable) from a generic failure / timeout. The detail carries the axios status
    # code, never the js.
    pipeline = FakePipeline(
        error=service.EvalError(
            "in-page eval threw: AxiosError: Request failed with status code 521"
        )
    )
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "extract()"}',
    )
    assert status == 504
    assert payload["error"] == "eval threw"
    assert "status code 521" in payload["detail"]


def test_eval_non_throw_failure_stays_generic_eval_failed() -> None:
    # A NON-EvalError pipeline failure (a device/CDP fault, not an in-page throw) keeps
    # the generic ``{"error": "eval failed"}`` 504 with NO ``detail`` — the gateway must
    # NOT retry it as an origin 5xx.
    pipeline = FakePipeline(error=RuntimeError("adb forward collapsed"))
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "extract()"}',
    )
    assert status == 504
    assert payload == {"error": "eval failed"}


# ── Bug 5 follow-on #3: /eval extract_clearance (mint comix's cf_clearance) ──────
# comix is a page-holder cleared ONLY via the eval path (never the destructive
# /solve). When the gateway asks (``extract_clearance: true``) the sidecar ALSO reads
# the host-scoped cf_clearance it auto-issued during the clear and returns it under a
# ``clearance`` block, so the gateway can mint+hold comix's replayable clearance.

_CLEARANCE_HOST = "mangadot.net"  # the FakePipeline config's lone allowlisted host
_EVAL_CLEARANCE = SolveResult(
    cf_clearance="EVAL-MINTED-COMIX-TOKEN",
    user_agent="Mozilla/5.0 (Android 11) WebView wv",
    host=_CLEARANCE_HOST,
    egress_ip="203.0.113.9",
    cf_clearance_expires=2_000_000_000.0,
)


def test_eval_extract_clearance_returns_value_and_clearance() -> None:
    """``extract_clearance: true`` threads ``with_clearance`` to the pipeline and
    returns ``{"value", "clearance": {cf_clearance, user_agent, cf_clearance_expires,
    egress_ip}}`` so the gateway can mint+hold comix's clearance with no ``/solve``."""
    pipeline = FakePipeline(eval_result={"ok": 1}, eval_clearance=_EVAL_CLEARANCE)
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=json.dumps(
            {
                "challenge_url": "https://mangadot.net/",
                "js": "(async () => true)()",
                "extract_clearance": True,
            }
        ).encode(),
    )
    assert status == 200
    assert payload["value"] == {"ok": 1}
    assert payload["clearance"] == {
        "cf_clearance": "EVAL-MINTED-COMIX-TOKEN",
        "user_agent": "Mozilla/5.0 (Android 11) WebView wv",
        "cf_clearance_expires": 2_000_000_000.0,
        "egress_ip": "203.0.113.9",
    }
    assert pipeline.eval_with_clearance == [True]  # the flag threaded to the pipeline


def test_eval_extract_clearance_null_when_no_cookie() -> None:
    """A clean clear that deposited NO host-scoped cookie returns ``clearance: null``
    (the gateway then treats the mint as not-ready and retries) — NOT a 504."""
    pipeline = FakePipeline(eval_result=True, eval_clearance=None)
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=json.dumps(
            {
                "challenge_url": "https://mangadot.net/",
                "js": "(async () => true)()",
                "extract_clearance": True,
            }
        ).encode(),
    )
    assert status == 200
    assert payload == {"value": True, "clearance": None}


def test_eval_without_extract_clearance_omits_clearance_and_flag() -> None:
    """D-08 parity: an ordinary eval (no ``extract_clearance``) returns the bare
    ``{"value": ...}`` with NO ``clearance`` key, and the pipeline is called WITHOUT
    ``with_clearance`` (byte-for-byte unchanged from before follow-on #3)."""
    pipeline = FakePipeline(
        eval_result={"chapters": [1]}, eval_clearance=_EVAL_CLEARANCE
    )
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "extract()"}',
    )
    assert status == 200
    assert payload == {"value": {"chapters": [1]}}
    assert "clearance" not in payload
    assert pipeline.eval_with_clearance == [False]


def test_eval_rejects_non_bool_extract_clearance() -> None:
    """A non-bool ``extract_clearance`` is a pre-device 422 (no device action)."""
    pipeline = FakePipeline()
    status, payload = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "x()",'
        b' "extract_clearance": "yes"}',
    )
    assert status == 422
    assert pipeline.calls == []
    assert "extract_clearance" in payload["error"]


def test_eval_extract_clearance_does_not_log_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-14-04: the minted cf_clearance token never appears in any log record — it rides
    only the response body."""
    pipeline = FakePipeline(eval_result=True, eval_clearance=_EVAL_CLEARANCE)
    with caplog.at_level("DEBUG"):
        status, payload = _service(pipeline).eval(
            api_key="s3cret-solver-key",
            body=json.dumps(
                {
                    "challenge_url": "https://mangadot.net/",
                    "js": "(async () => true)()",
                    "extract_clearance": True,
                }
            ).encode(),
        )
    assert status == 200
    assert payload["clearance"]["cf_clearance"] == "EVAL-MINTED-COMIX-TOKEN"
    for record in caplog.records:
        assert "EVAL-MINTED-COMIX-TOKEN" not in record.getMessage()


def test_eval_forwards_wait_for_predicate_to_pipeline() -> None:
    # wait_for is an optional JS boolean predicate string; a non-str is a 422.
    pipeline = FakePipeline()
    ok, _ = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://mangadot.net/", "js": "x()",'
            b' "wait_for": "window.ready === true"}'
        ),
    )
    assert ok == 200
    # The predicate must actually reach the pipeline, not just yield a 200.
    assert pipeline.eval_wait_for == ["window.ready === true"]

    pipeline2 = FakePipeline()
    bad, payload = _service(pipeline2).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "x()", "wait_for": 5}',
    )
    assert bad == 422
    assert pipeline2.calls == []
    assert "wait_for" in payload["error"]


def test_eval_pipeline_failure_is_504() -> None:
    pipeline = FakePipeline(error=RuntimeError("env module import failed"))
    status, _ = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "boom()"}',
    )
    assert status == 504


# ── /eval per-eval proxy parity with /solve (Req 7) ──────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        # Same malformed-proxy matrix as /solve — every shape is a PRE-device 422.
        b'{"challenge_url": "https://mangadot.net/", "js": "x()", "proxy": 5}',
        b'{"challenge_url": "https://mangadot.net/", "js": "x()", "proxy": {}}',
        b'{"challenge_url": "https://mangadot.net/", "js": "x()",'
        b' "proxy": {"server": "socks5://p:1"}}',
        b'{"challenge_url": "https://mangadot.net/", "js": "x()",'
        b' "proxy": {"server": "http://host:99999"}}',
        b'{"challenge_url": "https://mangadot.net/", "js": "x()",'
        b' "proxy": {"server": "http://p:1", "username": "u"}}',
    ],
)
def test_eval_rejects_malformed_proxy_before_any_device_action(body: bytes) -> None:
    # Req 7 / T-11-06 parity: a malformed eval proxy is a PRE-action 422 — the
    # device pipeline is never invoked (no hop repoint, no global http_proxy set).
    pipeline = FakePipeline()
    status, payload = _service(pipeline).eval(api_key="s3cret-solver-key", body=body)
    assert status == 422
    assert pipeline.calls == []  # never reaches the device
    assert "proxy" in payload["error"]


def test_eval_forwards_wellformed_proxy_to_pipeline() -> None:
    # Req 7 parity with test_solve_forwards_wellformed_proxy_to_pipeline: a valid
    # proxy rides the /eval body verbatim into the pipeline so the eval navigation
    # egresses the clearance's residential IP.
    pipeline = FakePipeline()
    status, _ = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://mangadot.net/", "js": "x()",'
            b' "proxy": {"server": "http://up.example:8080",'
            b' "username": "u", "password": "p"}}'
        ),
    )
    assert status == 200
    assert pipeline.eval_proxies == [
        {"server": "http://up.example:8080", "username": "u", "password": "p"}
    ]


def test_eval_without_proxy_passes_none_to_pipeline() -> None:
    # D-08: a no-proxy /eval drives the pipeline with proxy=None (byte-for-byte the
    # pre-Phase-14 eval body).
    pipeline = FakePipeline()
    status, _ = _service(pipeline).eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "x()"}',
    )
    assert status == 200
    assert pipeline.eval_proxies == [None]


def test_proxied_eval_uses_proxy_timeout_not_base() -> None:
    # Req 7 parity with test_proxied_solve_uses_proxy_timeout_not_base: with a proxy
    # present the eval's outer future is bounded by the LONGER proxy_solve_timeout_s
    # (it adds the hop + egress-verify overhead), so a delay between the base (tiny)
    # and proxy (generous) timeout completes (200) where a base-timeout eval 504s.
    pipeline = FakePipeline(delay=0.3)
    service = _service(
        pipeline,
        solve_timeout_s=0.05,
        proxy_solve_timeout_s=5.0,
        cancel_grace_s=2.0,
    )
    status, _ = service.eval(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://mangadot.net/", "js": "x()",'
            b' "proxy": {"server": "http://up.example:8080"}}'
        ),
    )
    assert status == 200


def test_no_proxy_eval_uses_base_timeout_and_times_out() -> None:
    # The same delay under the base (tiny) timeout and NO proxy must 504 — proving
    # the eval timeout selection keys off proxy-presence, not a constant (D-08).
    pipeline = FakePipeline(delay=0.3)
    service = _service(
        pipeline,
        solve_timeout_s=0.05,
        proxy_solve_timeout_s=5.0,
        cancel_grace_s=2.0,
    )
    status, _ = service.eval(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/", "js": "x()"}',
    )
    assert status == 504


def test_eval_and_solve_are_serialized() -> None:
    # PERF-01 / T-14-05: /eval shares /solve's Lock + single-worker executor, so an
    # eval arriving while a solve is in flight is rejected 503 (and vice-versa) —
    # the two can never run concurrently against the one redroid.
    started = threading.Event()
    release = threading.Event()
    pipeline = FakePipeline(started=started, release=release)
    service = _service(pipeline, solve_timeout_s=5.0)
    first_status: dict[str, int] = {}

    def first() -> None:
        status, _ = service.solve(
            api_key="s3cret-solver-key",
            body=b'{"challenge_url": "https://mangadot.net/"}',
        )
        first_status["status"] = status

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert started.wait(timeout=5)  # the solve is in flight under the lock
        # An /eval arriving now is rejected 503 — not queued behind the device.
        second, payload = service.eval(
            api_key="s3cret-solver-key",
            body=b'{"challenge_url": "https://mangadot.net/", "js": "1+1"}',
        )
        assert second == 503
        assert "busy" in payload["error"]
    finally:
        release.set()
        thread.join(timeout=5)

    assert first_status["status"] == 200
    assert pipeline.max_concurrent == 1  # the single device was never doubly driven


def test_eval_large_result_round_trips() -> None:
    # A4 (research §6): a chapter-list-sized result (thousands of rows) marshals
    # back through eval → do_POST → the JSON response writer with len() preserved.
    # The _MAX_BODY_BYTES cap is INBOUND-only and must not truncate the outbound
    # result. Driven over the real HTTP transport to prove no frame/body truncation.
    big = [{"i": i} for i in range(5000)]
    pipeline = FakePipeline(eval_result=big)
    with _running_server(pipeline) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/eval",
            body=b'{"challenge_url": "https://mangadot.net/", "js": "listAll()"}',
            headers={"X-Solver-Key": "s3cret-solver-key"},
        )
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
    assert resp.status == 200
    payload = json.loads(data)
    assert len(payload["value"]) == 5000  # nothing dropped on the outbound side
    assert payload["value"][-1] == {"i": 4999}  # the tail survived intact


def test_http_eval_rejects_oversized_body_before_reading() -> None:
    # The /eval branch shares /solve's pre-auth body cap: a multi-GB declared body
    # is rejected 413 on the Content-Length alone, before a byte is read.
    pipeline = FakePipeline()
    with _running_server(pipeline) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/eval", skip_host=True, skip_accept_encoding=True)
        conn.putheader("X-Solver-Key", "s3cret-solver-key")
        conn.putheader("Content-Length", str(10_000_000_000))
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 413
    assert pipeline.calls == []  # body never read → pipeline never invoked


def test_http_eval_rejects_unauthenticated_before_reading_body() -> None:
    # T-14-01: auth fails FIRST on /eval, before any attempt to read the body.
    pipeline = FakePipeline()
    with _running_server(pipeline) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/eval", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Content-Length", str(10_000_000_000))
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 401
    assert pipeline.calls == []


# ── healthz ──────────────────────────────────────────────────────────────────


def test_healthz_reflects_pipeline_health() -> None:
    assert _service(FakePipeline(healthy=True)).healthz()[0] == 200
    assert _service(FakePipeline(healthy=False)).healthz()[0] == 503


class _BootDevice:
    """Minimal device for AndroidSolvePipeline.health (WR-04): connect + is_booted."""

    def __init__(self, *, booted: bool, connect_error: Exception | None = None) -> None:
        self._booted = booted
        self._connect_error = connect_error
        self.connected = False

    def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    def is_booted(self) -> bool:
        return self._booted


def test_pipeline_health_requires_boot_completed() -> None:
    # WR-04: health is true only when the device actually booted, not merely when
    # adb connect returned (adb connect is exit-0-on-failure).
    booted = AndroidSolvePipeline(_BootDevice(booted=True), timeout_s=5.0)  # type: ignore[arg-type]
    assert booted.health() is True

    not_booted = AndroidSolvePipeline(_BootDevice(booted=False), timeout_s=5.0)  # type: ignore[arg-type]
    assert not_booted.health() is False


def test_pipeline_health_false_when_connect_raises() -> None:
    dev = _BootDevice(booted=True, connect_error=RuntimeError("unreachable"))
    pipeline = AndroidSolvePipeline(dev, timeout_s=5.0)  # type: ignore[arg-type]
    assert pipeline.health() is False


# ── HTTP transport hardening (CR-01 / IN-03) ─────────────────────────────────
#
# These exercise the real stdlib _Handler over a loopback socket to prove the
# pre-auth request path: the handler authenticates and caps the declared body
# size BEFORE reading a single byte, and a malformed Content-Length is a clean
# 400 (not a 500). The body is NEVER read on a rejected request, so the device
# pipeline is never invoked.


@pytest.fixture
def http_server(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[FakePipeline, int]]:
    pipeline = FakePipeline()
    srv = _SolverHTTPServer(("127.0.0.1", 0), _Handler, service=_service(pipeline))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield pipeline, srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_handler_has_socket_read_timeout() -> None:
    # A finite read timeout is what bounds a slow-loris dribble (CR-01).
    assert _Handler.timeout is not None
    assert _Handler.timeout > 0


def test_http_rejects_oversized_body_before_reading(
    http_server: tuple[FakePipeline, int],
) -> None:
    pipeline, port = http_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    # Declare a multi-GB body but send NONE of it: the handler must reject on the
    # declared Content-Length alone, never blocking on the (never-sent) body.
    conn.putrequest("POST", "/solve", skip_host=True, skip_accept_encoding=True)
    conn.putheader("X-Solver-Key", "s3cret-solver-key")
    conn.putheader("Content-Length", str(10_000_000_000))
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 413
    assert pipeline.calls == []  # body never read → pipeline never invoked


def test_http_rejects_unauthenticated_before_reading_body(
    http_server: tuple[FakePipeline, int],
) -> None:
    pipeline, port = http_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    # No X-Solver-Key, and a large declared body we never send: auth must fail
    # FIRST, before any attempt to read the body.
    conn.putrequest("POST", "/solve", skip_host=True, skip_accept_encoding=True)
    conn.putheader("Content-Length", str(10_000_000_000))
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 401
    assert pipeline.calls == []


def test_http_non_numeric_content_length_is_400(
    http_server: tuple[FakePipeline, int],
) -> None:
    pipeline, port = http_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/solve", skip_host=True, skip_accept_encoding=True)
    conn.putheader("X-Solver-Key", "s3cret-solver-key")
    conn.putheader("Content-Length", "not-a-number")
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 400
    assert pipeline.calls == []


def test_http_happy_path_reads_body_and_solves(
    http_server: tuple[FakePipeline, int],
) -> None:
    pipeline, port = http_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        "/solve",
        body=b'{"challenge_url": "https://mangadot.net/"}',
        headers={"X-Solver-Key": "s3cret-solver-key"},
    )
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 200
    assert pipeline.calls == [("https://mangadot.net/", "mangadot.net")]


# ── config (T-10-08, no keyless start) ───────────────────────────────────────


def test_config_refuses_to_start_without_api_key() -> None:
    with pytest.raises(ConfigError):
        SidecarConfig.from_env({})


# ── AndroidSolvePipeline re-tap loop (cold-WebView interactivity race) ─────────
#
# On a COLD WebView the challenges.cloudflare.com OOPIF frame URL appears BEFORE
# the Turnstile checkbox inside it is interactive, so a single tap misses. The
# pipeline must RE-LOCATE + RE-TAP each round until clearance is minted (or the
# deadline fires), and must STOP tapping once the widget is gone (locate → None).
# These drive the real AndroidSolvePipeline.solve() loop with fakes + a fake
# clock — no real adb / WebView / sleeps.


class FakeClock:
    """Deterministic monotonic clock; ``sleep`` simply advances ``now``."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDevice:
    """Records taps; satisfies the AdbDevice surface the pipeline calls."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.force_stops = 0
        self.removed_forwards: list[int] = []

    def connect(self) -> None:
        return None

    def force_stop_and_clear(self) -> None:
        self.force_stops += 1

    def launch_url(self, url: str) -> None:
        return None

    def pidof(self) -> int:
        return 4321

    def forward_devtools(self, pid: int) -> int:
        return 9222

    def remove_forward(self, local_port: int | None = None) -> None:
        self.removed_forwards.append(local_port if local_port is not None else 9222)

    def screencap(self) -> bytes:
        return b"PNG"

    def screen_size(self) -> tuple[int, int]:
        return (720, 1280)

    def input_tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class FakeWs:
    def __init__(self) -> None:
        self.closed = False

    def send(self, payload: str) -> None:
        return None

    def recv(self) -> str:
        return "{}"

    def close(self) -> None:
        self.closed = True


class SeqLocate:
    """``locate_checkbox`` stub: returns each result in turn; last value repeats.

    ``None`` models the widget having disappeared (challenge passed) — the loop
    must then STOP tapping.
    """

    def __init__(self, results: list[tuple[int, int] | None]) -> None:
        self._results = results
        self.calls = 0

    def __call__(self, ws, *, screencap, x_scale, y_scale):  # type: ignore[no-untyped-def]
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]


class SeqExtract:
    """``extract_clearance`` stub: returns the cookie once ``calls >= token_after``."""

    def __init__(
        self,
        token_after: int,
        token: str = "MANGADOT_TOKEN",
        expires: float | None = _STUB_EXPIRES,
    ) -> None:
        self._token_after = token_after
        self._token = token
        self._expires = expires
        self.calls = 0

    def __call__(self, ws_url, host):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls < self._token_after:
            return None
        return ClearanceCookie(value=self._token, expires=self._expires)


class SeqVerify:
    """``_in_verification`` stub: returns each result in turn; last value repeats.

    Models Turnstile's post-tap "Verification successful, waiting for <host> to
    respond" banner — ``True`` while it is up. The loop must NOT tap while it is
    ``True`` (a tap there resets the widget and aborts the solve).
    """

    def __init__(self, results: list[bool]) -> None:
        self._results = results
        self.calls = 0

    def __call__(self, ws):  # type: ignore[no-untyped-def]
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]


def _build_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    device: FakeDevice,
    locate: SeqLocate,
    extract: SeqExtract,
    clock: FakeClock,
    timeout_s: float,
    retap_interval_s: float = 5.0,
    poll_interval_s: float = 1.0,
) -> AndroidSolvePipeline:
    # Page-target discovery returns a single page ws url; the bulk CDP plumbing
    # (Page/DOM.enable, frame readiness, viewport scales) is stubbed so the test
    # exercises ONLY the locate→tap→poll loop.
    page_targets = b'[{"type":"page","webSocketDebuggerUrl":"ws://localhost:9222/p"}]'
    pipeline = AndroidSolvePipeline(
        device,  # type: ignore[arg-type]
        timeout_s=timeout_s,
        ws_factory=lambda url, *, timeout: FakeWs(),  # type: ignore[arg-type,return-value]
        http_get=lambda url, *, timeout: page_targets,
        launch_settle_s=0.0,
        poll_interval_s=poll_interval_s,
        retap_interval_s=retap_interval_s,
    )
    monkeypatch.setattr(service, "time", clock)
    monkeypatch.setattr(service, "cdp_call", lambda *a, **k: {})
    monkeypatch.setattr(service, "locate_checkbox", locate)
    monkeypatch.setattr(service, "extract_clearance", extract)
    monkeypatch.setattr(service, "webview_user_agent", lambda url, **kwargs: "UA-wv")
    # Pre-loop readiness/scale steps are covered by their own units; collapse them
    # to constants here so the loop under test runs deterministically.
    monkeypatch.setattr(
        pipeline, "_wait_for_cf_frame", lambda ws, ws_url, host, cancel=None: None
    )
    monkeypatch.setattr(pipeline, "_compute_scales", lambda ws: (2.0, 2.586))
    return pipeline


# ── BUG 5 solve-path parity: a clean already-cleared load must NOT burn the deadline ─


class _FrameTreeCdp:
    """``cdp_call`` stub for the REAL ``_wait_for_cf_frame``: answers Page.getFrameTree.

    The ``challenges.cloudflare.com`` OOPIF child frame is absent until the poll count
    reaches ``cf_after`` (``cf_after`` huge ⇒ it never renders — a clean already-cleared
    load; ``cf_after=N`` ⇒ a genuine challenge that renders on the Nth poll).
    """

    def __init__(self, cf_after: int) -> None:
        self._cf_after = cf_after
        self.calls = 0

    def __call__(self, ws, method, params=None, *, command_id):  # type: ignore[no-untyped-def]
        if method == "Page.getFrameTree":
            self.calls += 1
            tree: dict[str, object] = {
                "frame": {"url": "https://mangadot.net/", "id": "main"},
                "childFrames": [],
            }
            if self.calls >= self._cf_after:
                tree["childFrames"] = [
                    {
                        "frame": {
                            "url": "https://challenges.cloudflare.com/cdn-cgi/x",
                            "id": "cf",
                        },
                        "childFrames": [],
                    }
                ]
            return {"frameTree": tree}
        return {}


def _frame_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: FakeClock,
    cdp: _FrameTreeCdp,
    extract: object,
) -> AndroidSolvePipeline:
    # A pipeline that runs the REAL _wait_for_cf_frame (NOT the _build_pipeline stub):
    # service.time → FakeClock, service.cdp_call → the frame-tree stub, and
    # service.extract_clearance → the solve-path clean-check cookie stub.
    page_targets = b'[{"type":"page","webSocketDebuggerUrl":"ws://localhost:9222/p"}]'
    pipeline = AndroidSolvePipeline(
        FakeDevice(),  # type: ignore[arg-type]
        timeout_s=60.0,
        ws_factory=lambda url, *, timeout: FakeWs(),  # type: ignore[arg-type,return-value]
        http_get=lambda url, *, timeout: page_targets,
        launch_settle_s=0.0,
        poll_interval_s=1.0,
    )
    monkeypatch.setattr(service, "time", clock)
    monkeypatch.setattr(service, "cdp_call", cdp)
    monkeypatch.setattr(service, "extract_clearance", extract)
    return pipeline


def test_clean_solve_short_circuits_frame_poll_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug 5 / solve-path parity: a gateway-restart warm() re-solve of an
    # ALREADY-cleared host loads CLEAN (a warm/auto-issued cf_clearance, no
    # interactive Turnstile), so the challenges.cloudflare.com OOPIF NEVER appears.
    # _wait_for_cf_frame must NOT grind the full ~20s frame-poll deadline waiting for
    # a widget that won't come — it short-circuits the instant a host-scoped
    # cf_clearance is observed (nothing to tap; _tap_until_cleared reads it next).
    clock = FakeClock()
    cdp = _FrameTreeCdp(cf_after=10**9)  # CF OOPIF never renders (clean load)
    pipeline = _frame_pipeline(
        monkeypatch,
        clock=clock,
        cdp=cdp,
        extract=lambda ws_url, host: ClearanceCookie(
            value="WARM", expires=_STUB_EXPIRES
        ),
    )

    start = clock.now
    pipeline._wait_for_cf_frame(FakeWs(), "ws://localhost:9222/p", "mangadot.net")  # type: ignore[arg-type]
    elapsed = clock.now - start

    # Did NOT grind the full ~20s frame-poll deadline (the old vestigial wait).
    assert elapsed < service._FRAME_POLL_TIMEOUT_S
    # Short-circuited on the first poll (no sleep) — the warm re-solve is now fast.
    assert elapsed <= pipeline._frame_poll_interval_s


def test_genuine_challenge_solve_still_detects_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The short-circuit must NOT regress the genuine cold solve: when a real CF
    # challenge renders a few polls after load (cookie NOT yet minted),
    # _wait_for_cf_frame still detects the OOPIF and returns so the tap machinery can
    # clear it — and a clean page is never mistaken for it (the CF frame is checked
    # FIRST and no cf_clearance exists yet).
    clock = FakeClock()
    cdp = _FrameTreeCdp(cf_after=3)  # OOPIF renders on the 3rd poll (mangadot-style)
    pipeline = _frame_pipeline(
        monkeypatch,
        clock=clock,
        cdp=cdp,
        extract=lambda ws_url, host: None,  # challenge not cleared yet → no cookie
    )

    start = clock.now
    pipeline._wait_for_cf_frame(FakeWs(), "ws://localhost:9222/p", "mangadot.net")  # type: ignore[arg-type]
    elapsed = clock.now - start

    assert cdp.calls >= 3  # polled until the genuine challenge frame appeared
    # Returned ON the frame, not after grinding the full deadline.
    assert elapsed < service._FRAME_POLL_TIMEOUT_S


def test_solve_retaps_until_clearance_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The widget stays present and the first re-tap window yields no clearance;
    # a later round must re-tap and ultimately return the token.
    device = FakeDevice()
    locate = SeqLocate([(50, 100)])  # widget always present → coords every round
    extract = SeqExtract(token_after=7)  # token only after the 2nd round begins
    clock = FakeClock()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=locate,
        extract=extract,
        clock=clock,
        timeout_s=60.0,
    )

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.cf_clearance == "MANGADOT_TOKEN"
    assert result.user_agent == "UA-wv"
    # The minted cookie's expiry threads from extract_clearance → SolveResult so the
    # gateway can refresh ahead of the lapse.
    assert result.cf_clearance_expires == _STUB_EXPIRES
    # Re-tapped at least twice (cold-start race): one tap was not enough.
    assert len(device.taps) >= 2


def test_solve_raises_when_deadline_passes_without_clearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = FakeDevice()
    locate = SeqLocate([(50, 100)])  # widget never clears
    extract = SeqExtract(token_after=10_000)  # token never appears
    clock = FakeClock()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=locate,
        extract=extract,
        clock=clock,
        timeout_s=10.0,
    )

    with pytest.raises(SolveError):
        pipeline.solve("https://mangadot.net/", "mangadot.net")

    # It kept re-tapping across the bounded deadline rather than giving up at one.
    assert len(device.taps) >= 2


def test_solve_stops_tapping_once_widget_gone_then_returns_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round 1: widget present → one tap. Once the re-tap interval elapses the
    # widget is gone (locate → None) → NO further taps; the clearance shows up
    # during polling and is returned.
    device = FakeDevice()
    locate = SeqLocate([(50, 100), None])  # present once, then gone
    extract = SeqExtract(token_after=8)  # token after the re-tap window, widget gone
    clock = FakeClock()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=locate,
        extract=extract,
        clock=clock,
        timeout_s=60.0,
    )

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.cf_clearance == "MANGADOT_TOKEN"
    # Tapped exactly once — re-locate returned None (widget gone) so the elapsed
    # re-tap interval produced no second tap on a cleared page.
    assert device.taps == [(50, 100)]


def test_solve_does_not_retap_during_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for the tap-timing bug: the widget stays LOCATABLE the whole
    # time and the re-tap interval elapses, but Turnstile is in its post-tap
    # "Verification successful, waiting for <host> to respond" window. A second
    # tap there resets the widget and aborts the solve, so the loop must tap
    # exactly ONCE and then poll the clearance out without re-tapping.
    device = FakeDevice()
    locate = SeqLocate([(50, 100)])  # widget always located (never disappears)
    extract = SeqExtract(token_after=9)  # token mints late, well past the interval
    clock = FakeClock()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=locate,
        extract=extract,
        clock=clock,
        timeout_s=60.0,
    )
    # Interactive on the first probe (first tap fires), then verification is up.
    monkeypatch.setattr(pipeline, "_in_verification", SeqVerify([False, True]))

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.cf_clearance == "MANGADOT_TOKEN"
    # Exactly one tap: the verification guard suppressed every later tap even
    # though the widget was still located and the re-tap interval had elapsed.
    assert device.taps == [(50, 100)]


class RaiseThenToken:
    """``extract_clearance`` stub: raises for the first ``raises`` cycles, then mints.

    Models a transient CDP/cookie-poll blip (a refused 2nd concurrent socket, a ws
    timeout). The solve loop must SWALLOW the blip and keep polling, not abort.
    """

    def __init__(self, raises: int, token: str = "MANGADOT_TOKEN") -> None:
        self._raises = raises
        self._token = token
        self.calls = 0

    def __call__(self, ws_url, host):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls <= self._raises:
            raise RuntimeError("transient CDP hiccup")
        return ClearanceCookie(value=self._token, expires=_STUB_EXPIRES)


def test_solve_survives_transient_clearance_poll_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WR-03: a poll-cycle extract_clearance error must be caught + logged and the
    # loop continues to the next cycle — it must NOT abort the whole solve.
    device = FakeDevice()
    locate = SeqLocate([(50, 100)])
    extract = RaiseThenToken(raises=3)  # first 3 polls blow up, then the token mints
    clock = FakeClock()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=locate,
        extract=extract,  # type: ignore[arg-type]
        clock=clock,
        timeout_s=60.0,
    )

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.cf_clearance == "MANGADOT_TOKEN"
    assert extract.calls > 3  # it kept polling past the transient failures


def test_solve_keeps_token_when_ua_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # IN-01: the post-mint UA fetch is guarded — a /json/version hiccup must NOT
    # discard the already-minted (~60s-expensive) token; UA falls back to "".
    device = FakeDevice()
    locate = SeqLocate([(50, 100)])
    extract = SeqExtract(token_after=1)  # token mints immediately
    clock = FakeClock()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=locate,
        extract=extract,
        clock=clock,
        timeout_s=60.0,
    )

    def boom(url, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("/json/version hiccup")

    monkeypatch.setattr(service, "webview_user_agent", boom)

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.cf_clearance == "MANGADOT_TOKEN"  # token preserved
    assert result.user_agent == ""  # UA fell back to empty rather than aborting


def test_solve_threads_injected_getter_into_ua_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # IN-01: the UA fetch must use the pipeline's INJECTED http_get (so test/proxy
    # injection apply), not the module-default urllib getter.
    device = FakeDevice()
    locate = SeqLocate([(50, 100)])
    extract = SeqExtract(token_after=1)
    clock = FakeClock()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=locate,
        extract=extract,
        clock=clock,
        timeout_s=60.0,
    )
    captured: dict[str, object] = {}

    def recorder(url, *, http_get=None):  # type: ignore[no-untyped-def]
        captured["http_get"] = http_get
        return "UA-rec"

    monkeypatch.setattr(service, "webview_user_agent", recorder)

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.user_agent == "UA-rec"
    assert captured["http_get"] is pipeline._http_get


def test_solve_resets_device_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC3 (#207): a solve cancelled mid-flight must reset the device
    # (force_stop_and_clear) on its way out so the next solve starts on a clean
    # WebView, not a half-driven challenge page.
    device = FakeDevice()
    cancel = threading.Event()

    # The cancel fires DURING launch_url (the service signalling mid-solve); the
    # very next checkpoint then unwinds the solve.
    original_launch = device.launch_url

    def launch_then_cancel(url: str) -> None:
        original_launch(url)
        cancel.set()

    monkeypatch.setattr(device, "launch_url", launch_then_cancel)

    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=SeqLocate([(50, 100)]),
        extract=SeqExtract(token_after=10_000),
        clock=FakeClock(),
        timeout_s=60.0,
    )

    with pytest.raises(SolveCancelled):
        pipeline.solve("https://mangadot.net/", "mangadot.net", cancel)

    # force_stop_and_clear ran TWICE: the normal pre-launch reset, then the
    # cancellation-cleanup reset — the device is left clean for the next solve.
    assert device.force_stops == 2
    # The solve unwound before the locate→tap loop, so no tap landed.
    assert device.taps == []


def test_forward_torn_down_on_every_solve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #275 req 3: the adb devtools forward is removed in a finally on a NORMAL solve —
    # no leaked ESTABLISHED forward survives a completed solve.
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=SeqLocate([(50, 100)]),
        extract=SeqExtract(token_after=1),  # token mints immediately
        clock=FakeClock(),
        timeout_s=60.0,
    )

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.cf_clearance == "MANGADOT_TOKEN"
    assert device.removed_forwards == [9222]  # forward torn down after the solve


def test_forward_torn_down_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #275 req 3: the forward is removed in the finally even when the solve is
    # cancelled mid-flight AFTER the forward was established (mirrors
    # test_solve_resets_device_on_cancellation, but the cancel fires post-forward).
    device = FakeDevice()
    cancel = threading.Event()
    original_forward = device.forward_devtools

    def forward_then_cancel(pid: int) -> int:
        port = original_forward(pid)
        cancel.set()  # cancel fires right after the forward is established
        return port

    monkeypatch.setattr(device, "forward_devtools", forward_then_cancel)

    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        locate=SeqLocate([(50, 100)]),
        extract=SeqExtract(token_after=10_000),  # never mints → would run to deadline
        clock=FakeClock(),
        timeout_s=60.0,
    )

    with pytest.raises(SolveCancelled):
        pipeline.solve("https://mangadot.net/", "mangadot.net", cancel)

    assert device.removed_forwards == [9222]  # forward torn down on the cancel path


def test_config_from_env_parses_allowlist_and_timeout() -> None:
    config = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_ADB_TARGET": "redroid:5556",
            "SOLVER_ALLOWED_HOSTS": "mangadot.net, kagane.to",
            "SOLVER_SOLVE_TIMEOUT_S": "30",
            "SOLVER_CANCEL_GRACE_S": "7",
        }
    )
    assert config.api_key == "k"
    assert config.adb_target == "redroid:5556"
    assert config.allowed_hosts == frozenset({"mangadot.net", "kagane.to"})
    assert config.solve_timeout_s == 30.0
    assert config.cancel_grace_s == 7.0


def test_config_defaults_cancel_grace() -> None:
    # issue #207: the post-timeout drain window has a sane default when unset.
    config = SidecarConfig.from_env({"SOLVER_API_KEY": "k"})
    assert config.cancel_grace_s == 20.0


def test_boot_defensive_clear_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CR-1: a TRANSIENT adb hiccup at boot must not permanently skip the clear. The
    # bounded retry recovers once the device becomes ready, so a stale device-wide
    # proxy from a crashed prior solve cannot survive into the first (no-proxy) solve.
    attempts = {"connect": 0, "cleared": 0}

    class FlakyDevice:
        def __init__(self, target: str) -> None:
            self.target = target

        def connect(self) -> None:
            attempts["connect"] += 1
            if attempts["connect"] < 3:
                raise RuntimeError("adb not ready")

        def clear_global_http_proxy(self) -> None:
            attempts["cleared"] += 1

    monkeypatch.setattr(service, "AdbDevice", FlakyDevice)
    slept: list[float] = []
    ok = service._boot_defensive_clear(
        _config(), retries=5, sleep_s=0.0, sleep=slept.append
    )

    assert ok is True
    assert attempts["cleared"] == 1
    assert attempts["connect"] == 3  # 2 transient failures, then success
    assert slept == [0.0, 0.0]  # slept only between the 2 failed attempts


def test_boot_defensive_clear_gives_up_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exhausting the bounded retries returns False (logged + swallowed) — it never
    # blocks startup indefinitely, and never clears (the device never became ready).
    attempts = {"connect": 0, "cleared": 0}

    class DeadDevice:
        def __init__(self, target: str) -> None:
            self.target = target

        def connect(self) -> None:
            attempts["connect"] += 1
            raise RuntimeError("adb not ready")

        def clear_global_http_proxy(self) -> None:
            attempts["cleared"] += 1  # pragma: no cover - never reached

    monkeypatch.setattr(service, "AdbDevice", DeadDevice)
    ok = service._boot_defensive_clear(
        _config(), retries=3, sleep_s=0.0, sleep=lambda _: None
    )

    assert ok is False
    assert attempts["connect"] == 3
    assert attempts["cleared"] == 0


# ── Fix A (bug 4 cause #1): non-destructive in-page egress-verify for /eval ────
#
# The proxied eval's egress-verify must NOT navigate the main WebView away to the
# IP echo (that defeated the bug-3 fast path → every eval re-cleared comix's
# managed Cloudflare challenge). It now verifies the egress with an IN-PAGE fetch
# (no Page.navigate), keeping the byte-equal T-11-01 assertion on every eval, and
# only falls back to the destructive nav-away probe when the in-page probe is
# unavailable (CSP/CORS/transient). These drive the real ``_drive_eval`` with a
# recording cdp_call so the no-nav / mismatch / fallback behaviour is asserted.

_EVAL_HOST = "comix.to"
_EVAL_URL = "https://comix.to/"
_EVAL_JS = "(async()=>{return {marker:'EVAL_JS_MARKER'};})()"


class _DriveEvalCdp:
    """Records every cdp_call; returns scripted ``result.value`` by command id.

    Page/DOM.enable, Page.navigate, and any unscripted id return an empty value;
    ``Page.getFrameTree`` carries no ``frameTree`` so the CF-interstitial probe
    reads "not a challenge" (fail-open to the fast path).
    """

    def __init__(self, values: dict[int, object]) -> None:
        self.values = values
        self.calls: list[tuple[str, dict[str, object], int]] = []

    def __call__(self, ws, method, params=None, *, command_id):  # type: ignore[no-untyped-def]
        self.calls.append((method, params or {}, command_id))
        return {"result": {"value": self.values.get(command_id)}}

    def navigated_to(self, url: str) -> bool:
        return any(
            m == "Page.navigate" and p.get("url") == url for m, p, _ in self.calls
        )

    def ran_command(self, command_id: int) -> bool:
        return any(c == command_id for _, _, c in self.calls)


class _DeadThenAliveDevice(FakeDevice):
    """``pidof`` raises ``AdbError`` for the first ``fail_pidof_times`` calls, then
    returns a pid — models a dead ``webview_shell`` that the eval relaunches.

    ``launch_url`` records the relaunch target so the test can assert the recovery
    re-started the WebView on the challenge url.
    """

    def __init__(self, *, fail_pidof_times: int = 1) -> None:
        super().__init__()
        self._fail_pidof_times = fail_pidof_times
        self.launched: list[str] = []
        self.pidof_calls = 0

    def launch_url(self, url: str) -> None:
        self.launched.append(url)
        return None

    def pidof(self) -> int:
        self.pidof_calls += 1
        if self.pidof_calls <= self._fail_pidof_times:
            raise AdbError(
                "pidof org.chromium.webview_shell returned no pid "
                "(process not running?)"
            )
        return 4321


def _eval_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    cdp: _DriveEvalCdp,
    *,
    device: FakeDevice | None = None,
) -> AndroidSolvePipeline:
    page_targets = b'[{"type":"page","webSocketDebuggerUrl":"ws://localhost:9222/p"}]'
    pipeline = AndroidSolvePipeline(
        device or FakeDevice(),  # type: ignore[arg-type]
        timeout_s=60.0,
        ws_factory=lambda url, *, timeout: FakeWs(),  # type: ignore[arg-type,return-value]
        http_get=lambda url, *, timeout: page_targets,
        launch_settle_s=0.0,
        poll_interval_s=0.0,
        egress_read_timeout_s=1.0,
    )
    monkeypatch.setattr(service, "cdp_call", cdp)
    return pipeline


def test_eval_relaunches_dead_webview_then_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dead webview_shell: the first pidof() raises AdbError. The eval must relaunch
    # webview_shell on the challenge url, re-poll pidof() to a valid pid, then take
    # the re-nav + clear + hydrate branch (a fresh relaunch lands on the CF
    # interstitial, so _EVAL_LOCATION_CMD_ID is left UNSCRIPTED → fast path declines).
    cdp = _DriveEvalCdp(
        {
            service._EVAL_HYDRATION_CMD_ID: True,
            service._EVAL_CMD_ID: {"recovered": True},
        }
    )
    device = _DeadThenAliveDevice(fail_pidof_times=1)
    pipeline = _eval_pipeline(monkeypatch, cdp, device=device)
    monkeypatch.setattr(
        pipeline,
        "_clear_challenge_if_present",
        lambda ws, ws_url, host, cancel, deadline: None,
    )

    result = _drive(pipeline, expected_egress_ip="")

    assert result == {"recovered": True}
    assert device.launched == [_EVAL_URL]
    assert device.pidof_calls == 2
    assert cdp.navigated_to(_EVAL_URL)
    assert cdp.ran_command(service._EVAL_CMD_ID)


def test_ensure_webview_alive_expired_deadline_does_not_relaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # webview_shell is dead (pidof raises) AND the eval deadline has already passed:
    # the recovery MUST NOT relaunch the device or sleep — it raises immediately so a
    # timed-out request never keeps the single eval worker driving the device.
    cdp = _DriveEvalCdp({})
    device = _DeadThenAliveDevice(fail_pidof_times=1)
    pipeline = _eval_pipeline(monkeypatch, cdp, device=device)

    with pytest.raises(AdbError):
        pipeline._ensure_webview_alive(_EVAL_URL, None, deadline=time.monotonic() - 1.0)

    assert device.pidof_calls == 1  # only the initial probe; no post-relaunch poll
    assert device.launched == []  # deadline guard short-circuited before relaunch


def _drive(pipeline: AndroidSolvePipeline, expected_egress_ip: str):  # type: ignore[no-untyped-def]
    return pipeline._drive_eval(
        _EVAL_URL,
        _EVAL_HOST,
        _EVAL_JS,
        None,
        wait_for=None,
        deadline=time.monotonic() + 60.0,
        expected_egress_ip=expected_egress_ip,
    )


def test_proxied_eval_inpage_probe_matches_does_not_navigate_away(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # In-page probe returns the EXPECTED egress IP → the eval runs and the WebView
    # is NEVER navigated to the IP echo (the warm comix page survives → fast path).
    cdp = _DriveEvalCdp(
        {
            service._EGRESS_IN_PAGE_CMD_ID: "1.2.3.4",
            service._EVAL_LOCATION_CMD_ID: _EVAL_HOST,  # fast-path: on the comix page
            service._EVAL_HYDRATION_CMD_ID: True,  # SPA hydrated
            service._EVAL_CMD_ID: {"items": [1, 2, 3]},
        }
    )
    pipeline = _eval_pipeline(monkeypatch, cdp)

    with caplog.at_level("INFO", logger="android_solver.service"):
        result = _drive(pipeline, expected_egress_ip="1.2.3.4")

    assert result == {"items": [1, 2, 3]}
    # The in-page probe ran (id 43) and verified egress WITHOUT navigating away.
    assert cdp.ran_command(service._EGRESS_IN_PAGE_CMD_ID)
    assert not cdp.navigated_to(service._EGRESS_ECHO_URL)
    assert not cdp.navigated_to(_EVAL_URL)  # fast path: no re-nav of the main page
    assert cdp.ran_command(service._EVAL_CMD_ID)  # the gateway js DID run
    # Logged the in-page verification; NEVER the eval js or the marshalled result.
    assert "(in-page)" in caplog.text
    assert "EVAL_JS_MARKER" not in caplog.text
    assert "items" not in caplog.text


def test_proxied_eval_inpage_probe_mismatch_raises_and_runs_no_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # In-page probe returns a DIFFERENT IP → the rotating/non-sticky-proxy SolveError
    # is raised and the gateway js is NEVER evaluated (no wrong-IP eval).
    cdp = _DriveEvalCdp(
        {
            service._EGRESS_IN_PAGE_CMD_ID: "9.9.9.9",
            service._EVAL_CMD_ID: {"items": [1]},
        }
    )
    pipeline = _eval_pipeline(monkeypatch, cdp)

    with pytest.raises(SolveError, match="rotating/non-sticky proxy"):
        _drive(pipeline, expected_egress_ip="1.2.3.4")

    assert not cdp.ran_command(service._EVAL_CMD_ID)  # no eval on a mismatch
    assert not cdp.navigated_to(service._EGRESS_ECHO_URL)  # in-page, no nav-away


def test_proxied_eval_inpage_probe_unavailable_falls_back_to_destructive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # In-page probe unavailable (returns None — CSP/CORS) → fall back to the
    # destructive nav-away verify (a Page.navigate to the IP echo IS recorded), then
    # the eval still runs verified — NEVER an unverified eval.
    cdp = _DriveEvalCdp(
        {
            service._EGRESS_IN_PAGE_CMD_ID: None,  # IIFE returned null ⇒ unavailable
            service._EGRESS_READ_CMD_ID: "1.2.3.4",  # destructive innerText read
            service._EVAL_HYDRATION_CMD_ID: True,
            service._EVAL_CMD_ID: {"ok": True},
        }
    )
    pipeline = _eval_pipeline(monkeypatch, cdp)
    # The re-nav clear machinery is exercised by its own units; collapse it here so
    # the fallback-then-eval flow runs deterministically.
    monkeypatch.setattr(
        pipeline,
        "_clear_challenge_if_present",
        lambda ws, ws_url, host, cancel, deadline: None,
    )

    result = _drive(pipeline, expected_egress_ip="1.2.3.4")

    assert result == {"ok": True}
    # The destructive fallback navigated the main page AWAY to the IP echo.
    assert cdp.navigated_to(service._EGRESS_ECHO_URL)
    assert cdp.ran_command(service._EVAL_CMD_ID)  # still verified, eval still ran


def test_no_proxy_eval_runs_no_egress_probe_of_either_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No-proxy path (expected_egress_ip == "") → NEITHER the in-page probe NOR the
    # destructive nav-away runs; byte-for-byte the prior no-proxy eval (D-08).
    cdp = _DriveEvalCdp(
        {
            service._EVAL_LOCATION_CMD_ID: _EVAL_HOST,
            service._EVAL_HYDRATION_CMD_ID: True,
            service._EVAL_CMD_ID: {"x": 1},
        }
    )
    pipeline = _eval_pipeline(monkeypatch, cdp)

    result = _drive(pipeline, expected_egress_ip="")

    assert result == {"x": 1}
    assert not cdp.ran_command(service._EGRESS_IN_PAGE_CMD_ID)  # no in-page probe
    assert not cdp.navigated_to(service._EGRESS_ECHO_URL)  # no destructive nav


# ── multi-target registry + body `target` routing (LANE-03 / SEC-01) ──────────

_DEFAULT_TARGET = "redroid:5555"
_KAGANE_TARGET = "redroid-kagane:5555"


def _multi_config(**overrides: object) -> SidecarConfig:
    base: dict[str, object] = {
        "api_key": "s3cret-solver-key",
        "adb_target": _DEFAULT_TARGET,
        "adb_targets": (_DEFAULT_TARGET, _KAGANE_TARGET),
        "allowed_hosts": frozenset({"mangadot.net", "kagane.to"}),
        "solve_timeout_s": 5.0,
    }
    base.update(overrides)
    return SidecarConfig(**base)  # type: ignore[arg-type]


def _multi_service(pipelines: dict[str, FakePipeline], **cfg: object) -> SolverService:
    return SolverService(_multi_config(**cfg), pipelines=pipelines)


def test_solve_absent_target_uses_default_worker() -> None:
    default_pipe = FakePipeline()
    kagane_pipe = FakePipeline()
    service = _multi_service(
        {_DEFAULT_TARGET: default_pipe, _KAGANE_TARGET: kagane_pipe}
    )
    status, _ = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert status == 200
    # Absent target ⇒ the DEFAULT worker's pipeline only (byte-for-byte today).
    assert default_pipe.calls == [("https://mangadot.net/", "mangadot.net")]
    assert kagane_pipe.calls == []


def test_solve_explicit_target_routes_to_that_worker_only() -> None:
    default_pipe = FakePipeline()
    kagane_pipe = FakePipeline()
    service = _multi_service(
        {_DEFAULT_TARGET: default_pipe, _KAGANE_TARGET: kagane_pipe}
    )
    status, _ = service.solve(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://kagane.to/", "target": "redroid-kagane:5555"}'
        ),
    )
    assert status == 200
    assert kagane_pipe.calls == [("https://kagane.to/", "kagane.to")]
    # The default target's pipeline is NEVER invoked.
    assert default_pipe.calls == []


def test_solve_unknown_target_is_pre_device_422() -> None:
    default_pipe = FakePipeline()
    kagane_pipe = FakePipeline()
    service = _multi_service(
        {_DEFAULT_TARGET: default_pipe, _KAGANE_TARGET: kagane_pipe}
    )
    status, payload = service.solve(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://kagane.to/", "target": "redroid-nope:5555"}'
        ),
    )
    assert status == 422
    assert "target" in payload["error"]
    # NO device action on ANY worker — never build/use an AdbDevice for a bad target.
    assert default_pipe.calls == []
    assert kagane_pipe.calls == []


def test_eval_unknown_target_is_pre_device_422() -> None:
    default_pipe = FakePipeline()
    kagane_pipe = FakePipeline()
    service = _multi_service(
        {_DEFAULT_TARGET: default_pipe, _KAGANE_TARGET: kagane_pipe}
    )
    status, payload = service.eval(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://kagane.to/", "js": "1",'
            b' "target": "redroid-nope:5555"}'
        ),
    )
    assert status == 422
    assert "target" in payload["error"]
    assert default_pipe.calls == []
    assert kagane_pipe.calls == []


def test_eval_explicit_target_routes_to_that_worker_only() -> None:
    default_pipe = FakePipeline()
    kagane_pipe = FakePipeline()
    service = _multi_service(
        {_DEFAULT_TARGET: default_pipe, _KAGANE_TARGET: kagane_pipe}
    )
    status, _ = service.eval(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://kagane.to/", "js": "1",'
            b' "target": "redroid-kagane:5555"}'
        ),
    )
    assert status == 200
    assert kagane_pipe.calls == [("https://kagane.to/", "kagane.to")]
    assert default_pipe.calls == []


def test_solve_per_target_allowlist_rejects_global_host() -> None:
    # kagane lane scoped to ONLY kagane.to; mangadot.net is globally allowed but NOT
    # in kagane's scope ⇒ 422 before any device action (SEC-01).
    default_pipe = FakePipeline()
    kagane_pipe = FakePipeline()
    service = _multi_service(
        {_DEFAULT_TARGET: default_pipe, _KAGANE_TARGET: kagane_pipe},
        allowed_hosts_by_target={_KAGANE_TARGET: frozenset({"kagane.to"})},
    )
    status, payload = service.solve(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://mangadot.net/",'
            b' "target": "redroid-kagane:5555"}'
        ),
    )
    assert status == 422
    assert "allowlist" in payload["error"]
    assert kagane_pipe.calls == []
    assert default_pipe.calls == []


def test_solve_per_target_allowlist_allows_scoped_host() -> None:
    default_pipe = FakePipeline()
    kagane_pipe = FakePipeline()
    service = _multi_service(
        {_DEFAULT_TARGET: default_pipe, _KAGANE_TARGET: kagane_pipe},
        allowed_hosts_by_target={_KAGANE_TARGET: frozenset({"kagane.to"})},
    )
    # The scoped host on its lane is accepted; the default lane uses the global set.
    k_status, _ = service.solve(
        api_key="s3cret-solver-key",
        body=(
            b'{"challenge_url": "https://kagane.to/", "target": "redroid-kagane:5555"}'
        ),
    )
    d_status, _ = service.solve(
        api_key="s3cret-solver-key",
        body=b'{"challenge_url": "https://mangadot.net/"}',
    )
    assert k_status == 200
    assert d_status == 200
    assert kagane_pipe.calls == [("https://kagane.to/", "kagane.to")]
    assert default_pipe.calls == [("https://mangadot.net/", "mangadot.net")]


def test_concurrent_different_targets_do_not_503_each_other() -> None:
    # Per-target Lock: two lanes run concurrently (no cross-target 503); a SECOND
    # solve on the SAME target still serializes (503 busy) — T-10-11 per lane.
    started_a = threading.Event()
    started_b = threading.Event()
    release = threading.Event()
    pipe_a = FakePipeline(started=started_a, release=release)
    pipe_b = FakePipeline(started=started_b, release=release)
    service = _multi_service({_DEFAULT_TARGET: pipe_a, _KAGANE_TARGET: pipe_b})
    results: dict[str, int] = {}

    def run(name: str, target: str | None, host: str) -> None:
        target_field = f', "target": "{target}"' if target else ""
        body = f'{{"challenge_url": "https://{host}/"{target_field}}}'.encode()
        status, _ = service.solve(api_key="s3cret-solver-key", body=body)
        results[name] = status

    thread_a = threading.Thread(target=run, args=("a", None, "mangadot.net"))
    thread_a.start()
    thread_b = threading.Thread(target=run, args=("b", _KAGANE_TARGET, "kagane.to"))
    thread_b.start()
    try:
        assert started_a.wait(timeout=5)  # default lane in flight
        assert started_b.wait(timeout=5)  # kagane lane in flight CONCURRENTLY
        # A second solve on the DEFAULT target (its lock held) is 503 busy.
        busy_status, busy_payload = service.solve(
            api_key="s3cret-solver-key",
            body=b'{"challenge_url": "https://mangadot.net/"}',
        )
        assert busy_status == 503
        assert "busy" in busy_payload["error"]
    finally:
        release.set()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

    assert results == {"a": 200, "b": 200}
    # Each device was driven by at most one solve at a time.
    assert pipe_a.max_concurrent == 1
    assert pipe_b.max_concurrent == 1
