"""Anti-bot engine selector tests (#35 / #40).

The ``CloudflareSolver`` engine seam is a config flip — ``engine="camoufox"``
(default since #40) wires the Camoufox launch closure; ``engine="patchright"``
wires the Patchright closure as an opt-in escalation. Both back the SAME
``BrowserLifecycle`` (orthogonal to the cache/single-flight logic). These
tests assert the selector — they do NOT launch a real browser (D-42 — the
deterministic gate never imports either browser binary).

The Settings → solver wiring is also covered: ``Settings.cloudflare_engine``
defaults to ``"camoufox"`` (since #40 — dev mirrors prod so Firefox-only
failure modes like issue #54 surface in local repro). The opt-in path is
``GATEWAY_CLOUDFLARE_ENGINE=patchright``.
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


def test_settings_default_engine_is_camoufox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (dev/CI/prod) — Camoufox is the engine (#40 single default).

    Hermetic against an ambient ``GATEWAY_CLOUDFLARE_ENGINE`` (the workflow
    still sets it explicitly as belt-and-braces documentation, but the
    default no longer needs CI to override it).
    """
    monkeypatch.delenv("GATEWAY_CLOUDFLARE_ENGINE", raising=False)
    assert _settings().cloudflare_engine == "camoufox"


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
    """An explicit ``camoufox`` env value is also accepted (matches the
    default; verifies the env override path itself, not just the absence)."""
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "camoufox")
    assert _settings().cloudflare_engine == "camoufox"


def test_settings_engine_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Literal["patchright", "camoufox"]`` rejects anything else at validation."""
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_ENGINE", "selenium")
    with pytest.raises(ValidationError):
        _settings()


# ──────────────────────── CloudflareSolver(engine=...) ────────────────────────


def test_solver_default_engine_wires_camoufox_launch() -> None:
    """Default — the lifecycle's launch closure points at the Camoufox path
    (#40 — Camoufox is the single dev/CI/prod default).

    We do NOT call it (that would import camoufox + start a browser) — we just
    assert the right bound method was wired in. Asserts the property is exposed
    on the solver too, so tests / observability can read the engine choice.
    """
    solver = CloudflareSolver()
    assert solver.engine == "camoufox"
    # The launch closure on the lifecycle is the camoufox bound method.
    assert (
        solver._lifecycle._launch  # type: ignore[attr-defined]
        == solver._launch_camoufox_context  # type: ignore[attr-defined]
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
