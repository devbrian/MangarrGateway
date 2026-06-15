"""Gate test: the live harness session-shares ONE AndroidSolver (#215 follow-up).

The android live tests (mangadot/kagane/mangaball) route their Cloudflare solve
through the redroid-WebView sidecar, serialized through ONE device at ~1–2 min
per ``/solve``. The harness used to build a FRESH ``AndroidSolver`` for every
per-test ``create_app`` lifespan, leaving ``AndroidSolver._held`` empty each test
→ every android live test re-minted ``cf_clearance`` (unacceptable wall-clock +
device load). ``tests/live/conftest.py`` now shares ONE AndroidSolver across the
whole live-test session — exactly how it already session-shares the Patchright
``CloudflareSolver`` — so the held clearance is reused: ONE ``/solve`` per source,
not per test.

These gate-run tests lock the hand-mirror partition + the substitution wiring in
WITHOUT a live browser, sidecar, or network call. Importing ``tests.live.conftest``
from the gate is the established pattern (``tests/test_live_collection.py``,
``tests/test_live_warm_best_effort.py``).
"""

from __future__ import annotations

import manga_gateway.app as _app_module
from manga_gateway.framework.android_solver import AndroidSolver
from tests.live.conftest import (
    _ANDROID_KEYS,
    _build_session_android_solver_kwargs,
    _session_android_warm_budget_s,
)


def test_kwargs_challenge_urls_are_exactly_the_android_sources() -> None:
    """The hand-mirror partitions cf_sources to android-engine sources ONLY.

    Guards the builder against drift from app.py's lifespan partition (the same
    invariant ``_build_session_solver_kwargs`` upholds for the patchright leg):
    the AndroidSolver's challenge-url map must be EXACTLY the android source keys
    (mangadot/kagane/mangaball) derived from the real registry — never a
    patchright source (comix). A drift would make the session-shared AndroidSolver
    warm/solve a different set than the per-test lifespan, defeating the share.
    """
    challenge_urls = _build_session_android_solver_kwargs()["challenge_urls"]
    assert set(challenge_urls) == set(_ANDROID_KEYS)
    # The android partition must never leak the patchright comix source.
    assert "comix" not in challenge_urls
    # Sanity: the set is non-empty (the three android sources are registered).
    assert challenge_urls


def test_kwargs_shape_mirrors_app_androidsolver_build() -> None:
    """The kwargs are exactly the fields app.py passes to ``AndroidSolver(...)``.

    app.py builds ``AndroidSolver(base_url=..., api_key=..., challenge_urls=...,
    timeout_s=..., proxy=playwright_proxy)`` — ``proxy`` passed UNCONDITIONALLY
    (None when unconfigured). The mirror must carry the same five keys so the
    shared instance is constructed identically to production.
    """
    kwargs = _build_session_android_solver_kwargs()
    assert set(kwargs) == {
        "base_url",
        "api_key",
        "challenge_urls",
        "timeout_s",
        "proxy",
    }
    # The kwargs construct a real AndroidSolver offline (no network until /solve).
    solver = AndroidSolver(**kwargs)
    assert isinstance(solver, AndroidSolver)


def test_app_module_androidsolver_is_the_monkeypatch_target() -> None:
    """``_substitute_app_solver`` patches ``manga_gateway.app.AndroidSolver``.

    The substitution monkeypatches the import-resolved symbol in app.py's
    namespace so every per-test lifespan's ``AndroidSolver(**kwargs)`` returns the
    shared instance. Confirm the target exists and is the class (app.py line 29's
    ``from .framework.android_solver import AndroidSolver``), exactly as the CF
    substitution relies on ``manga_gateway.app.CloudflareSolver``.
    """
    assert _app_module.AndroidSolver is AndroidSolver


def test_factory_returns_the_shared_instance() -> None:
    """The substitution factory ignores its kwargs and returns the shared solver.

    Mirrors the wiring in ``_substitute_app_solver``: a factory closed over ONE
    AndroidSolver returns that SAME object for any ``**kwargs`` the per-test
    lifespan passes — so ``_held`` clearance persists across every live test.
    Deterministic + offline (no /solve).
    """
    shared = AndroidSolver(**_build_session_android_solver_kwargs())

    def _android_factory(**_kwargs: object) -> AndroidSolver:
        return shared

    assert _android_factory(base_url="ignored", timeout_s=1.0) is shared
    assert _android_factory() is shared


def test_android_warm_budget_scales_with_key_count() -> None:
    """The android warm ceiling scales with the #android keys at the per-solve cap.

    An android solve is ~1–2 min, serialized through one device — so the flat CF
    per-domain budget is far too tight. The budget must be strictly increasing in
    the key count and floored at one key (never 0)."""
    one = _session_android_warm_budget_s(1)
    two = _session_android_warm_budget_s(2)
    three = _session_android_warm_budget_s(3)
    assert two > one
    assert three > two
    # Floored: a zero/negative count still yields the one-key (>0) budget.
    assert _session_android_warm_budget_s(0) == one
    assert _session_android_warm_budget_s(-5) == one
    # Each key must absorb a full ~1–2 min sidecar mint (>= 120s per key region):
    # three serialized android solves must comfortably exceed three inline mints.
    assert three > 120.0 * 3
