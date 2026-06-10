"""Offline acceptance for the probe's android-engine solver dispatch (Req 8/9).

``scripts/probe_rate_limits.py`` is a standalone diagnostic script — no ``src/``
package, no ``__init__.py``, and excluded from ruff/mypy. It is, however, importable
at runtime (it only pulls ``manga_gateway.*`` + stdlib), so this test loads it via
``importlib.util.spec_from_file_location`` and exercises ``_build_solver`` directly.

It proves:

* an android-engine source (``solver_engine == "android"`` — mangadot/kagane)
  dispatches to an :class:`AndroidSolver` pointed at the configured
  ``android_solver_url`` (Req 8);
* a non-android source (MangaDex, ``antibot="none"``) still returns the existing
  ``NoopSolver`` (the android branch never widens the blast radius);
* the pinned residential proxy threaded via ``_build_settings`` reaches the
  AndroidSolver verbatim through ``build_proxy`` — the SAME egress the httpx replay
  uses (Req 9) — and the proxy password is NEVER surfaced by the masked label
  (T-w1k-01 / T-11-02).

The probe header's "no pytest tests" note is now stale: this is the offline gate
acceptance for Req 8/9 (it constructs solvers only — no network, no device, NOT
``live``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from manga_gateway.framework.android_solver import AndroidSolver
from manga_gateway.framework.antibot import NoopSolver
from manga_gateway.sources.kagane import KaganeSource
from manga_gateway.sources.mangadex import MangaDexSource
from manga_gateway.sources.mangadot import MangadotSource

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_PATH = _REPO_ROOT / "scripts" / "probe_rate_limits.py"
_SOLVER_URL = "http://deploy.example:18080"


def _load_probe() -> ModuleType:
    """Import the standalone probe script at runtime (no package, mypy-excluded)."""
    spec = importlib.util.spec_from_file_location("probe_rate_limits", _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so any internal self-reference resolves.
    sys.modules.setdefault("probe_rate_limits", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The loaded probe module with ``GATEWAY_ANDROID_SOLVER_URL`` populated."""
    monkeypatch.setenv("GATEWAY_ANDROID_SOLVER_URL", _SOLVER_URL)
    return _load_probe()


@pytest.mark.parametrize("source_cls", [MangadotSource, KaganeSource])
def test_android_engine_source_dispatches_to_android_solver(
    probe: ModuleType, source_cls: type
) -> None:
    """Req 8: mangadot/kagane build an AndroidSolver at the configured url."""
    settings = probe._build_settings(None)
    assert settings.android_solver_url == _SOLVER_URL

    solver = probe._build_solver(source_cls, settings, None)

    assert isinstance(solver, AndroidSolver)
    # base_url is trailing-slash-stripped by the ctor; our url has none.
    assert solver._base_url == _SOLVER_URL
    # The source's own key + challenge URL flow into the per-source map.
    assert solver._challenge_urls == {
        source_cls.key: source_cls.cloudflare_challenge_url
    }


def test_non_android_source_returns_existing_solver(probe: ModuleType) -> None:
    """Req 8: a non-android source (MangaDex, antibot=none) is unchanged."""
    settings = probe._build_settings(None)
    solver = probe._build_solver(MangaDexSource, settings, None)
    assert not isinstance(solver, AndroidSolver)
    assert isinstance(solver, NoopSolver)


def test_android_solver_carries_pinned_proxy_without_leaking_value(
    probe: ModuleType,
) -> None:
    """Req 9: the pinned proxy threaded via _build_settings reaches the AndroidSolver
    (same egress as the httpx replay), and its value is never surfaced unmasked."""
    proxy = probe.ProxyEntry(
        index=3,
        host="proxy.invalid",
        port="8080",
        username="fakeuser",
        password="NOTAREALSECRET-zzz9",
    )
    settings = probe._build_settings(proxy)
    solver = probe._build_solver(MangadotSource, settings, proxy)

    assert isinstance(solver, AndroidSolver)
    # build_proxy emits the Playwright dict; it rides the AndroidSolver verbatim so
    # the /solve egress matches the replay egress (Req 9).
    assert solver._proxy is not None
    assert solver._proxy["server"] == proxy.server
    assert solver._proxy["username"] == proxy.username
    assert solver._proxy["password"] == proxy.password

    # T-w1k-01 / T-11-02: the masked label NEVER exposes the credential value.
    masked = probe._mask_proxy(proxy)
    assert proxy.password not in masked
    assert proxy.username not in masked
    assert f"proxy[#{proxy.index}]" in masked
