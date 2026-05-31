"""Anti-bot engine selector tests (#35).

The ``CloudflareSolver`` engine seam is a config flip — ``engine="patchright"``
(default) wires the Patchright launch closure; ``engine="camoufox"`` wires the
Camoufox closure. Both back the SAME ``BrowserLifecycle`` (orthogonal to the
cache/single-flight logic). These tests assert the selector — they do NOT
launch a real browser (D-42 — the deterministic gate never imports either
browser binary).

The Settings → solver wiring is also covered: ``Settings.cloudflare_engine``
defaults to ``"patchright"`` and ``GATEWAY_CLOUDFLARE_ENGINE=camoufox`` flips
it. This is what the nightly-live-smoke workflow uses on ubuntu-latest.
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
    """Default (dev/Windows) — Patchright is the engine.

    Hermetic against an ambient ``GATEWAY_CLOUDFLARE_ENGINE`` (e.g. the
    nightly-live-smoke workflow sets it to ``camoufox``).
    """
    monkeypatch.delenv("GATEWAY_CLOUDFLARE_ENGINE", raising=False)
    assert _settings().cloudflare_engine == "patchright"


def test_settings_engine_env_override_camoufox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI flips ``GATEWAY_CLOUDFLARE_ENGINE=camoufox`` per #35."""
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "camoufox")
    assert _settings().cloudflare_engine == "camoufox"


def test_settings_engine_env_override_patchright_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``patchright`` env value is also accepted."""
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "patchright")
    assert _settings().cloudflare_engine == "patchright"


def test_settings_engine_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Literal["patchright", "camoufox"]`` rejects anything else at validation."""
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "selenium")
    with pytest.raises(ValidationError):
        _settings()


# ──────────────────────── CloudflareSolver(engine=...) ────────────────────────


def test_solver_default_engine_wires_patchright_launch() -> None:
    """Default — the lifecycle's launch closure points at the Patchright path.

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


def test_solver_engine_camoufox_wires_camoufox_launch() -> None:
    """``engine="camoufox"`` wires the Camoufox launch closure (#35 CI escalation)."""
    solver = CloudflareSolver(engine="camoufox")
    assert solver.engine == "camoufox"
    assert (
        solver._lifecycle._launch  # type: ignore[attr-defined]
        == solver._launch_camoufox_context  # type: ignore[attr-defined]
    )


def test_solver_engine_explicit_patchright_wires_patchright_launch() -> None:
    """``engine="patchright"`` (explicit) wires the Patchright launch closure."""
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
