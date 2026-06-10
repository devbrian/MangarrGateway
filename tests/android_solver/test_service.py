"""SolverService control logic driven against a FAKE solve pipeline.

No real adb / redroid / WebView is touched. Asserts the SEC-01 guarantees:
api-key auth (T-10-08), the host allowlist SSRF guard (T-10-09), serialized
solves + timeout (T-10-11), and that the token value never reaches the logs
(T-10-10). Also covers the env-driven config refusing to start keyless.
"""

from __future__ import annotations

import http.client
import threading
import time
from collections.abc import Iterator

import pytest
from android_solver.config import ConfigError, SidecarConfig
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


class FakePipeline:
    """Records calls; optionally delays / raises / reports max concurrency."""

    def __init__(
        self,
        *,
        result: SolveResult | None = None,
        delay: float = 0.0,
        healthy: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.proxies: list[dict[str, object] | None] = []
        self._result = result
        self._delay = delay
        self._healthy = healthy
        self._error = error
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
        # username/password present but not a str
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "http://p:1", "username": 7}}',
        b'{"challenge_url": "https://mangadot.net/",'
        b' "proxy": {"server": "http://p:1", "password": 7}}',
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
    assert set(payload) == {"cf_clearance", "user_agent", "host", "egress_ip"}
    assert payload["egress_ip"] == ""  # empty on the no-proxy path


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
    pipeline = FakePipeline(delay=0.1)
    service = _service(pipeline, solve_timeout_s=5.0)
    results: list[int] = []
    lock = threading.Lock()

    def call() -> None:
        status, _ = service.solve(
            api_key="s3cret-solver-key",
            body=b'{"challenge_url": "https://mangadot.net/"}',
        )
        with lock:
            results.append(status)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [200, 200]
    # The lock + single-worker executor must prevent any overlap.
    assert pipeline.max_concurrent == 1


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
    """``extract_clearance`` stub: returns the token once ``calls >= token_after``."""

    def __init__(self, token_after: int, token: str = "MANGADOT_TOKEN") -> None:
        self._token_after = token_after
        self._token = token
        self.calls = 0

    def __call__(self, ws_url, host):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._token if self.calls >= self._token_after else None


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
    monkeypatch.setattr(pipeline, "_wait_for_cf_frame", lambda ws, cancel=None: None)
    monkeypatch.setattr(pipeline, "_compute_scales", lambda ws: (2.0, 2.586))
    return pipeline


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
        return self._token


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
