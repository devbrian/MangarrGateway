"""Anti-bot engine selector tests (#35 / #40).

The ``CloudflareSolver`` engine seam is a config flip — ``engine="camoufox"``
(default since #40) wires the Camoufox launch closure; ``engine="patchright"``
wires the Patchright closure as an opt-in escalation. Both back the SAME
``BrowserLifecycle`` (orthogonal to the cache/single-flight logic). These
tests assert the selector — they do NOT launch a real browser (D-42 — the
deterministic gate never imports either browser binary).

The Settings → solver wiring is also covered: ``Settings.cloudflare_engine``
defaults to ``"patchright"`` (since the comix-parallel-engine-probe finding —
only Chromium runs concurrent CF navigations, so it is the default that makes
``cloudflare_fetch_concurrency`` > 1 work). The opt-in fallback is
``GATEWAY_CLOUDFLARE_ENGINE=camoufox``, which must be paired with
``cloudflare_fetch_concurrency=1`` (enforced by a model validator, #64).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from manga_gateway.config import Settings
from manga_gateway.framework.antibot import CloudflareSolver

TEST_API_KEY = "test-key-deterministic-0123456789"


def _settings(**over: object) -> Settings:
    return Settings(api_key=TEST_API_KEY, **over)  # type: ignore[arg-type]


# ──────────────────────── Settings.cloudflare_engine ────────────────────────


def test_settings_default_engine_is_patchright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (dev/prod) — Patchright/Chromium is the engine, and it pairs with
    the default ``cloudflare_fetch_concurrency=3`` without tripping the guard.

    Hermetic against an ambient ``GATEWAY_CLOUDFLARE_ENGINE`` (a camoufox host /
    CI runner still sets it explicitly, but the default no longer needs that).
    """
    monkeypatch.delenv("GATEWAY_CLOUDFLARE_ENGINE", raising=False)
    monkeypatch.delenv("GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY", raising=False)
    settings = _settings()
    assert settings.cloudflare_engine == "patchright"
    assert settings.cloudflare_fetch_concurrency == 3


def test_settings_engine_env_override_patchright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GATEWAY_CLOUDFLARE_ENGINE=patchright`` opts into the Chromium escalation
    (#40 — Patchright is opt-in only since the dev/prod engine unification)."""
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "patchright")
    assert _settings().cloudflare_engine == "patchright"


def test_settings_engine_env_override_camoufox_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GATEWAY_CLOUDFLARE_ENGINE=camoufox`` opts into the Firefox fallback.

    Camoufox must be paired with ``fetch_concurrency=1`` (the default 3 would
    trip the ``_reject_camoufox_parallel`` guard), so this also pins it to 1 —
    exactly how a camoufox host / CI runner is expected to configure itself.
    """
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "camoufox")
    settings = _settings(cloudflare_fetch_concurrency=1)
    assert settings.cloudflare_engine == "camoufox"


def test_settings_engine_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Literal["patchright", "camoufox"]`` rejects anything else at validation."""
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "selenium")
    with pytest.raises(ValidationError):
        _settings()


# ──────────── engine x fetch_concurrency guard (#64 / issue #59) ────────────


def test_settings_camoufox_with_parallel_is_rejected() -> None:
    """camoufox + ``fetch_concurrency > 1`` fails fast (the unsafe combo).

    Camoufox/Firefox stalls concurrent Cloudflare navigations, so the pairing
    silently returned zero results before this guard. The error message names
    the fix (set concurrency=1 or use patchright)."""
    with pytest.raises(ValidationError, match=r"requires.*patchright"):
        _settings(cloudflare_engine="camoufox", cloudflare_fetch_concurrency=3)


def test_settings_camoufox_with_concurrency_1_is_ok() -> None:
    """camoufox + 1 is the supported pairing — no error."""
    settings = _settings(cloudflare_engine="camoufox", cloudflare_fetch_concurrency=1)
    assert settings.cloudflare_engine == "camoufox"
    assert settings.cloudflare_fetch_concurrency == 1


def test_settings_patchright_with_parallel_is_ok() -> None:
    """patchright + ``> 1`` is the whole point — Chromium runs parallel CF navs."""
    settings = _settings(cloudflare_engine="patchright", cloudflare_fetch_concurrency=5)
    assert settings.cloudflare_engine == "patchright"
    assert settings.cloudflare_fetch_concurrency == 5


# ──────────────────────── CloudflareSolver(engine=...) ────────────────────────


def test_solver_default_engine_wires_patchright_launch() -> None:
    """Default — the lifecycle's launch closure points at the Patchright path
    (Chromium is the single default since comix-parallel-engine-probe).

    We do NOT call it (that would import patchright + start a browser) — we just
    assert the right bound method was wired in. Asserts the property is exposed
    on the solver too, so tests / observability can read the engine choice.
    """
    solver = CloudflareSolver()
    assert solver.engine == "patchright"
    # The launch closure on the lifecycle is the patchright bound method.
    assert (
        solver._lifecycle._launch  # type: ignore[attr-defined]
        == solver._launch_patchright_context  # type: ignore[attr-defined]
    )


def test_solver_engine_camoufox_explicit_wires_camoufox_launch() -> None:
    """``engine="camoufox"`` (explicit) wires the Camoufox launch closure —
    matches the default; verifies the explicit-arg path itself."""
    solver = CloudflareSolver(engine="camoufox")
    assert solver.engine == "camoufox"
    assert (
        solver._lifecycle._launch  # type: ignore[attr-defined]
        == solver._launch_camoufox_context  # type: ignore[attr-defined]
    )


def test_solver_engine_patchright_wires_patchright_launch() -> None:
    """``engine="patchright"`` wires the Patchright launch closure (#40 opt-in
    escalation)."""
    solver = CloudflareSolver(engine="patchright")
    assert solver.engine == "patchright"
    assert (
        solver._lifecycle._launch  # type: ignore[attr-defined]
        == solver._launch_patchright_context  # type: ignore[attr-defined]
    )


def test_solver_engine_unchanged_when_lifecycle_injected() -> None:
    """When tests inject a lifecycle, ``engine`` is recorded but the launch
    closure on the injected lifecycle is left untouched — the test owns the
    mocked launch (D-42 — no real browser in the gate)."""
    from manga_gateway.framework.solver_lifecycle import BrowserLifecycle

    async def mock_launch() -> object:
        return object()  # pragma: no cover — never invoked in the selector test

    async def mock_solve(_context: object) -> object:
        return object()  # pragma: no cover

    lifecycle = BrowserLifecycle(launch=mock_launch, solve=mock_solve)  # type: ignore[arg-type]
    solver = CloudflareSolver(engine="camoufox", lifecycle=lifecycle)
    # ``engine`` is still recorded (so /status etc. can surface it),
    # but the injected lifecycle's launch is what runs.
    assert solver.engine == "camoufox"
    assert solver._lifecycle is lifecycle  # type: ignore[attr-defined]
    assert solver._lifecycle._launch is mock_launch  # type: ignore[attr-defined]


# ──────────── headed-on-datacenter Xvfb virtual display (#35 fix) ────────────


def test_virtual_display_noop_when_headless() -> None:
    """Headless launches never start Xvfb (the residential / dev default path)."""
    solver = CloudflareSolver(headless=True)
    solver._start_virtual_display()  # type: ignore[attr-defined]
    assert solver._virtual_display is None  # type: ignore[attr-defined]


def test_virtual_display_noop_when_display_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headed but a DISPLAY already exists (real desktop / xvfb-run) → no-op."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    solver = CloudflareSolver(headless=False)
    solver._start_virtual_display()  # type: ignore[attr-defined]
    assert solver._virtual_display is None  # type: ignore[attr-defined]


def test_virtual_display_noop_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headed on Windows/mac dev → no Xvfb (a real browser window opens instead)."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("DISPLAY", raising=False)
    solver = CloudflareSolver(headless=False)
    solver._start_virtual_display()  # type: ignore[attr-defined]
    assert solver._virtual_display is None  # type: ignore[attr-defined]


def test_virtual_display_starts_when_headed_linux_no_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headed + Linux + no DISPLAY → starts an Xvfb display (the datacenter path).

    Injects a fake ``pyvirtualdisplay`` so the test needs no real Xvfb binary."""
    import sys as _sys
    import types

    events: list[str] = []

    class _FakeDisplay:
        def __init__(self, **kwargs: object) -> None:
            self._kwargs = kwargs

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    fake_mod = types.ModuleType("pyvirtualdisplay")
    fake_mod.Display = _FakeDisplay  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "pyvirtualdisplay", fake_mod)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    solver = CloudflareSolver(headless=False)
    solver._start_virtual_display()  # type: ignore[attr-defined]
    assert events == ["start"]
    assert isinstance(solver._virtual_display, _FakeDisplay)  # type: ignore[attr-defined]
    # Idempotent — a second call does not start a second display.
    solver._start_virtual_display()  # type: ignore[attr-defined]
    assert events == ["start"]
