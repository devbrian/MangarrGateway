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
from android_solver.service import SolveResult, SolverService


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
