"""Live-test collection layer (D-47 / D-49 / D-50 / D-55).

This is the discovery seam for the per-source nightly live-smoke framework
(Phase 5). Four hooks live here, each tied to a locked decision:

* **D-47 — Auto-discovery via the registry.** Module-level
  ``REGISTERED_KEYS`` is materialized once at conftest import time by
  constructing a fresh ``SourceRegistry``, calling
  ``register_builtin_sources(reg)``, and asking the registry for its keys.
  Parametrized live-smoke modules import ``REGISTERED_KEYS`` and pass it
  to ``pytest.mark.parametrize("source_key", REGISTERED_KEYS)``.
* **D-49 — Profiles live under ``tests/live/profiles/``, not on the
  production Source class.** ``_load_profile(source_key)`` imports
  ``tests.live.profiles.{source_key}`` and reads its top-level
  ``LIVE_SMOKE: LiveSmokeProfile`` instance.
* **D-50 — Missing profile = collection-time failure (live only).**
  ``_load_profile`` raises ``pytest.UsageError`` with a message that names
  ``D-50`` and the missing key when the profile module is absent or does
  not expose ``LIVE_SMOKE``. The error is only triggered from within the
  live-collection code path (the ``profile`` fixture and the timeout-
  marker hook), so the deterministic gate's ``addopts = "-m 'not live'"``
  filter deselects the live modules BEFORE any profile is loaded — the
  D-50 error never fires during the gate (RESEARCH A7 gate-isolation
  invariant; proved by ``tests/test_live_collection.py``).
* **D-55 — Per-test timeout markers from the profile.**
  ``pytest_collection_modifyitems`` attaches
  ``pytest.mark.timeout(profile.download_timeout_s, method="signal")`` to
  every collected item whose keywords include ``live`` AND whose callspec
  carries a ``source_key`` parameter. ``method="signal"`` is the cross-
  platform default; on Windows ``signal`` is not deliverable so
  pytest-timeout silently falls back to ``thread`` — both modes interrupt
  a stuck live test (RESEARCH Pitfall 2).

Additionally, the autouse ``_no_real_cloudflare_warm`` fixture in
``tests/conftest.py`` monkeypatches ``CloudflareSolver.warm`` to a no-op
for the WHOLE session so the deterministic gate never launches a real
browser. Live tests need the REAL ``warm()`` so Comix's Cloudflare
clearance is actually solved. ``_restore_real_cloudflare_warm`` below
re-binds ``CloudflareSolver.warm`` to the original coroutine for every
live test — a counter-monkeypatch keyed to the live-conftest scope so
the gate's no-op is restored automatically once the live-test session
ends.
"""

from __future__ import annotations

import importlib

import pytest

from manga_gateway.framework.antibot import CloudflareSolver
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.sources import register_builtin_sources

from .profiles._base import LiveSmokeProfile


def _registered_keys() -> list[str]:
    """Enumerate registered source keys via ``SourceRegistry`` (D-47).

    A fresh ``SourceRegistry()`` is constructed every call so this function
    is side-effect-free against any process-level registry the app shell
    happens to own. Returns ``list[str]`` in registration order
    (``register_builtin_sources`` registers MangaDex then Comix, but
    callers MUST NOT depend on the order — the source set is unordered).
    """
    reg = SourceRegistry()
    register_builtin_sources(reg)
    return reg.keys()


# D-47: materialize at conftest-import (= live-test-collection) time, NOT
# inside a fixture. The parametrize decorator in each smoke module reads
# this constant during the collection phase before any fixture runs.
# Type is inferred from _registered_keys' return annotation. The literal
# assignment shape is also the contract the acceptance-criteria grep checks.
REGISTERED_KEYS = _registered_keys()


def _load_profile(source_key: str) -> LiveSmokeProfile:
    """Load ``tests/live/profiles/{source_key}.py``'s ``LIVE_SMOKE`` (D-49 / D-50).

    Raises ``pytest.UsageError`` with a ``D-50``-tagged message when the
    profile module is missing or does not expose ``LIVE_SMOKE``. The
    UsageError class is what pytest treats as a hard collection error.
    """
    try:
        mod = importlib.import_module(f"tests.live.profiles.{source_key}")
    except ImportError as exc:
        raise pytest.UsageError(
            f"D-50: source '{source_key}' is registered but "
            f"tests/live/profiles/{source_key}.py is missing — every "
            f"registered source MUST ship a LiveSmokeProfile in the same PR."
        ) from exc
    profile = getattr(mod, "LIVE_SMOKE", None)
    if profile is None:
        raise pytest.UsageError(
            f"D-50: tests/live/profiles/{source_key}.py exists but does "
            f"not expose a top-level LIVE_SMOKE: LiveSmokeProfile."
        )
    if not isinstance(profile, LiveSmokeProfile):
        raise pytest.UsageError(
            f"D-50: tests/live/profiles/{source_key}.py exposes LIVE_SMOKE "
            f"but it is {type(profile).__name__}, not LiveSmokeProfile."
        )
    return profile


@pytest.fixture
def profile(source_key: str) -> LiveSmokeProfile:
    """Inject the live-smoke profile for the parametrized ``source_key``.

    Parametrized live-smoke modules declare ``source_key`` via
    ``pytest.mark.parametrize("source_key", REGISTERED_KEYS)``; this
    fixture resolves the key to the per-source ``LiveSmokeProfile``.
    """
    return _load_profile(source_key)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Attach ``pytest.mark.timeout(profile.download_timeout_s)`` to every
    parametrized live test (D-55).

    ``method="signal"`` is the cross-platform default — pytest-timeout
    silently downgrades to ``thread`` on Windows (RESEARCH Pitfall 2).
    Items without a ``source_key`` parameter or without the ``live``
    keyword are skipped untouched. ``pytest.UsageError`` from
    ``_load_profile`` is NOT swallowed here — a missing profile must
    propagate as the documented D-50 collection error.
    """
    del config  # signature requirement only
    for item in items:
        if "live" not in item.keywords:
            continue
        callspec = getattr(item, "callspec", None)
        source_key = callspec.params.get("source_key") if callspec else None
        if source_key is None:
            continue
        profile = _load_profile(source_key)
        item.add_marker(
            pytest.mark.timeout(profile.download_timeout_s, method="signal")
        )


@pytest.fixture(autouse=True)
def _restore_real_cloudflare_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the REAL ``CloudflareSolver.warm`` for live tests.

    ``tests/conftest.py:23-44`` registers an autouse session-wide no-op
    monkeypatch of ``CloudflareSolver.warm`` so the deterministic gate
    never tries to talk to real Cloudflare. Live tests are the explicit
    opt-in to the real path — they NEED ``warm()`` to actually solve the
    challenge. Re-import the antibot module fresh so the unmonkeypatched
    coroutine is available, then rebind it; ``monkeypatch.undo()`` does
    not undo a sibling MonkeyPatch instance's setattr, so the rebind is
    the simplest restoration path (PLANNER NOTE in 05-03-PLAN.md).
    """
    antibot_mod = importlib.import_module("manga_gateway.framework.antibot")
    importlib.reload(antibot_mod)
    fresh_solver_cls = antibot_mod.CloudflareSolver
    monkeypatch.setattr(CloudflareSolver, "warm", fresh_solver_cls.warm)
