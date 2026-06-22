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
    (comix/mangadot/kagane/mangaball — comix joined in Phase 14) derived from the
    real registry — never the patchright source (mangafire). A drift would make the
    session-shared AndroidSolver warm/solve a different set than the per-test
    lifespan, defeating the share.
    """
    challenge_urls = _build_session_android_solver_kwargs()["challenge_urls"]
    assert set(challenge_urls) == set(_ANDROID_KEYS)
    # Phase 14: comix is now an android-engine source (its in-page eval + clearance
    # both route to the redroid sidecar), so it MUST be in the android partition.
    assert "comix" in challenge_urls
    # The android partition must never leak the patchright mangafire source.
    assert "mangafire" not in challenge_urls
    # Sanity: the set is non-empty (the android sources are registered).
    assert challenge_urls


def test_kwargs_shape_mirrors_app_androidsolver_build() -> None:
    """The kwargs are exactly the fields app.py passes to ``AndroidSolver(...)``.

    app.py builds ``AndroidSolver(base_url=..., api_key=..., challenge_urls=...,
    on_demand_keys=..., warm_last_keys=..., clearance_lifetime_s=...,
    warm_gate_timeout_s=..., timeout_s=..., proxy=playwright_proxy)`` — ``proxy`` passed
    UNCONDITIONALLY
    (None when unconfigured). The mirror must carry the same keys so the shared instance
    is constructed identically to production. (``test_live_conftest_lockstep``
    AST-guards this against app.py directly, so a future app.py kwarg is caught even if
    this hardcoded set is not updated — but keep it accurate too.)
    """
    kwargs = _build_session_android_solver_kwargs()
    assert set(kwargs) == {
        "base_url",
        "api_key",
        "challenge_urls",
        "on_demand_keys",
        "warm_last_keys",
        "clearance_lifetime_s",
        "warm_gate_timeout_s",
        "timeout_s",
        "proxy",
    }
    # The kwargs construct a real AndroidSolver offline (no network until /solve).
    solver = AndroidSolver(**kwargs)
    assert isinstance(solver, AndroidSolver)


def test_on_demand_keys_mirror_app_so_warm_skips_mangaball() -> None:
    """DRIFT GUARD (debug pooltimeout-recurrence): the session-shared AndroidSolver
    MUST carry ``on_demand_keys`` exactly like app.py, or its ``warm()`` eager-solves
    mangaball against the contended home sidecar → 504 → D-33 force-disable → every
    mangaball live test fails (sticky #261) while production skips it correctly.

    mangaball declares ``cloudflare_challenge_optional=True``, so it MUST be in the
    mirror's ``on_demand_keys`` — and must never leak a non-on-demand android source
    (mangadot/kagane) into the skip set.
    """
    on_demand = _build_session_android_solver_kwargs()["on_demand_keys"]
    assert "mangaball" in on_demand
    assert "mangadot" not in on_demand
    assert "kagane" not in on_demand


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
