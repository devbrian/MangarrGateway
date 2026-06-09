"""SolverService control logic driven against a FAKE solve pipeline.

No real adb / redroid / WebView is touched. Asserts the SEC-01 guarantees:
api-key auth (T-10-08), the host allowlist SSRF guard (T-10-09), serialized
solves + timeout (T-10-11), and that the token value never reaches the logs
(T-10-10). Also covers the env-driven config refusing to start keyless.
"""

from __future__ import annotations

import threading
import time

import pytest
from android_solver.config import ConfigError, SidecarConfig
from android_solver.service import (
    AndroidSolvePipeline,
    SolveError,
    SolveResult,
    SolverService,
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
        self._result = result
        self._delay = delay
        self._healthy = healthy
        self._error = error
        self._counter_lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0

    def solve(self, challenge_url: str, host: str) -> SolveResult:
        with self._counter_lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            self.calls.append((challenge_url, host))
            if self._delay:
                time.sleep(self._delay)
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
    }
    assert pipeline.calls == [("https://mangadot.net/", "mangadot.net")]


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


# ── healthz ──────────────────────────────────────────────────────────────────


def test_healthz_reflects_pipeline_health() -> None:
    assert _service(FakePipeline(healthy=True)).healthz()[0] == 200
    assert _service(FakePipeline(healthy=False)).healthz()[0] == 503


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

    def connect(self) -> None:
        return None

    def force_stop_and_clear(self) -> None:
        return None

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
    monkeypatch.setattr(service, "webview_user_agent", lambda url: "UA-wv")
    # Pre-loop readiness/scale steps are covered by their own units; collapse them
    # to constants here so the loop under test runs deterministically.
    monkeypatch.setattr(pipeline, "_wait_for_cf_frame", lambda ws: None)
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
    # Round 1: widget present → one tap. Round 2+: widget gone (locate → None) →
    # NO further taps, but the clearance shows up during polling and is returned.
    device = FakeDevice()
    locate = SeqLocate([(50, 100), None])  # present once, then gone
    extract = SeqExtract(token_after=6)  # token appears in the 2nd round's poll
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
    # Tapped exactly once — never re-tapped a cleared page (no fresh challenge).
    assert device.taps == [(50, 100)]


def test_config_from_env_parses_allowlist_and_timeout() -> None:
    config = SidecarConfig.from_env(
        {
            "SOLVER_API_KEY": "k",
            "SOLVER_ADB_TARGET": "redroid:5556",
            "SOLVER_ALLOWED_HOSTS": "mangadot.net, kagane.to",
            "SOLVER_SOLVE_TIMEOUT_S": "30",
        }
    )
    assert config.api_key == "k"
    assert config.adb_target == "redroid:5556"
    assert config.allowed_hosts == frozenset({"mangadot.net", "kagane.to"})
    assert config.solve_timeout_s == 30.0
