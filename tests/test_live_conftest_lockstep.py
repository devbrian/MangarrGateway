"""Lockstep guard: ``tests/live/conftest.py`` must mirror ``app.py``'s solver builds.

Bug 5 follow-on #3 regression. ``tests/live/conftest.py`` hand-mirrors the
``manga_gateway.app.lifespan`` ``AndroidSolver(...)`` construction so the
session-shared live solver behaves IDENTICALLY to production. That mirror has
drifted twice — first ``on_demand_keys`` (sticky #261), then ``warm_last_keys``
(the A2 page-holder exclusion) — and each silent drift broke the live android leg:
the session solver warmed/solved a different set than production, so comix re-minted
via the destructive ``/solve`` and every comix live test errored with a 5xx.

These tests AST-parse BOTH source files (no import of the live conftest needed, so
they run in the OFFLINE gate) and assert the keyword sets stay in lockstep — the
exact invariant the conftest's own docstring demands ("Kept in LOCKSTEP with
``manga_gateway.app.lifespan``").
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_PY = _REPO_ROOT / "src" / "manga_gateway" / "app.py"
_LIVE_CONFTEST = _REPO_ROOT / "tests" / "live" / "conftest.py"

# The one kwarg app.py / the mirror deliberately do NOT pass (the solver lazily builds
# its own httpx client to the sidecar) — excluded from both sides of the comparison.
_NEVER_PASSED = {"client"}


def _call_kwarg_names(source: str, callee: str) -> set[str]:
    """Keyword-arg names of the (single) ``callee(...)`` call in ``source``.

    Excludes ``**kwargs`` splats (``keyword.arg is None``). Asserts the call appears
    exactly once so a refactor that adds a second construction site is caught.
    """
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == callee
    ]
    assert len(calls) == 1, (
        f"expected exactly one {callee}(...) call, found {len(calls)}"
    )
    return {kw.arg for kw in calls[0].keywords if kw.arg is not None}


def _returned_dict_keys(source: str, func_name: str) -> set[str]:
    """String-literal keys of the ``return {...}`` dict inside ``func_name``."""
    tree = ast.parse(source)
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == func_name
    )
    returns = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert len(returns) == 1, (
        f"expected one dict-return in {func_name}, found {len(returns)}"
    )
    dict_node = returns[0].value
    assert isinstance(dict_node, ast.Dict)
    keys: set[str] = set()
    for key in dict_node.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            f"non-literal-str key in {func_name}'s return dict"
        )
        keys.add(key.value)
    return keys


def test_android_solver_kwargs_lockstep() -> None:
    """The live conftest's ``_build_session_android_solver_kwargs`` must pass EXACTLY
    the kwargs app.py's lifespan passes to ``AndroidSolver(...)`` (minus ``client``).

    This is the guard the conftest docstring asks for: it catches a NEW app.py kwarg
    the mirror omits (``warm_last_keys`` / ``on_demand_keys`` drift → comix /solve →
    5xx) AND a mirror kwarg app.py never passes (a stale leftover)."""
    app_kwargs = _call_kwarg_names(_APP_PY.read_text(), "AndroidSolver") - _NEVER_PASSED
    mirror_kwargs = (
        _returned_dict_keys(
            _LIVE_CONFTEST.read_text(), "_build_session_android_solver_kwargs"
        )
        - _NEVER_PASSED
    )
    assert mirror_kwargs == app_kwargs, (
        "tests/live/conftest.py::_build_session_android_solver_kwargs has DRIFTED from "
        "app.py's AndroidSolver(...) build. "
        f"Only in app.py: {sorted(app_kwargs - mirror_kwargs)}; "
        f"only in the mirror: {sorted(mirror_kwargs - app_kwargs)}. "
        "Update the hand-mirror to restore lockstep (silent drift breaks the live "
        "android leg — comix re-mints via the destructive /solve → 5xx)."
    )
