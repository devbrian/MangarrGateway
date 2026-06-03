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
  ``pytest.mark.timeout(profile.download_timeout_s, method=...)`` to
  every collected item whose keywords include ``live`` AND whose callspec
  carries a ``source_key`` parameter. ``method`` is platform-aware:
  ``signal`` on POSIX (interrupts blocking C I/O via ``SIGALRM``),
  ``thread`` on Windows where ``SIGALRM`` is unavailable — both modes
  interrupt a stuck live test (RESEARCH Pitfall 2).

Additionally, the autouse ``_no_real_cloudflare_warm`` fixture in
``tests/conftest.py`` monkeypatches ``CloudflareSolver.warm`` to a no-op
for the WHOLE session so the deterministic gate never launches a real
browser. Live tests need the REAL ``warm()`` so Comix's Cloudflare
clearance is actually solved. ``_restore_real_cloudflare_warm`` below
re-binds ``CloudflareSolver.warm`` to the original coroutine captured at
THIS conftest's import time — a per-test additive counter-monkeypatch
that auto-reverts when the fixture finalizes, so the gate's no-op is
restored automatically once the live-test session ends.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio

import manga_gateway.app as _app_module
from manga_gateway.config import Settings
from manga_gateway.framework.antibot import CloudflareSolver
from manga_gateway.framework.proxy import build_proxy
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


# CR-02 (issue #29): capture the real ``CloudflareSolver.warm`` at conftest
# import time — BEFORE the gate-wide autouse no-op patch in
# ``tests/conftest.py`` ever runs (autouse fixtures run per-test; conftest
# module bodies execute at collection start). This frozen reference is what
# ``_restore_real_cloudflare_warm`` rebinds per live test, replacing the
# previous ``importlib.reload(antibot_mod)`` approach that wiped any
# module-level state and produced divergent class identities.
_ORIGINAL_CLOUDFLARE_WARM = CloudflareSolver.warm

_log = logging.getLogger(__name__)


async def _warm_best_effort(
    warm: Callable[[], Awaitable[Any]],
    *,
    timeout: float,  # noqa: ASYNC109 — wrapped in asyncio.wait_for below, not an op budget
) -> bool:
    """Run the shared live-session warm BEST-EFFORT.

    The session solver (``_session_solver``) is shared across EVERY live test,
    Cloudflare-gated or not (mangadex is pure httpx; mangaball is csrf-bootstrap —
    neither touches Cloudflare). Production ``warm()`` is fire-and-forget and
    isolates per-domain failures (D-33), and the nightly triage keeps a single
    source's failure green, flipping red only when ALL sources fail (D-58).

    A hard ``await asyncio.wait_for(warm(), 60)`` at session-fixture setup
    violated both: when one Cloudflare-gated source (comix) was slow to clear on
    the datacenter runner, the ``TimeoutError`` propagated out of session-fixture
    setup and EVERY source's tests ERRORed before their own code ran — including
    pure-httpx mangadex (nightly runs 26912244601 / 26912471852: ``31 errors``).

    So a warm timeout/failure is logged and swallowed here. The Cloudflare-gated
    source's own tests then fail on their real clearance need (correctly triaged
    to that source's sticky issue), while non-CF sources run unaffected. Returns
    ``True`` if warm completed, ``False`` if it timed out or raised.
    """
    try:
        await asyncio.wait_for(warm(), timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 — best-effort: a CF-clearance hiccup must not error non-CF sources
        _log.warning(
            "live session warm failed/timed out after %.0fs (best-effort); "
            "Cloudflare-gated source tests may fail, non-CF sources proceed",
            timeout,
        )
        return False


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
    parametrized live test (D-55), AND pin every live test to a session-
    scoped event loop so the shared CloudflareSolver fixture (built once
    on the session loop) and the tests that consume it stay on the SAME
    loop. Cross-loop sharing of asyncio primitives (Semaphore/Lock/Future)
    inside ``BrowserLifecycle`` was the architectural blocker for the
    prior session's first mitigation attempt (cf-warm-burst-timeout.md
    Mitigation Attempt 1) — pinning live tests to the session loop is
    what makes the shared solver actually viable.

    Gate tests are untouched (the marker is added only on items with the
    ``live`` keyword), so deterministic gate isolation is preserved per
    the existing ``asyncio_default_fixture_loop_scope = "function"``
    config in pyproject.toml.

    Method selection is platform-aware: ``signal`` on POSIX (interrupts
    blocking C I/O via ``SIGALRM``), ``thread`` on Windows where
    ``signal.SIGALRM`` is unavailable. pytest-timeout 2.4.0 honors a
    marker-supplied ``method`` literally and will crash on Windows if
    given ``method="signal"`` — the platform branch here is what makes
    the dev host runnable. Items without a ``source_key`` parameter or
    without the ``live`` keyword are skipped untouched. ``pytest.UsageError``
    from ``_load_profile`` is NOT swallowed here — a missing profile must
    propagate as the documented D-50 collection error.
    """
    del config  # signature requirement only
    timeout_method = "thread" if sys.platform == "win32" else "signal"
    for item in items:
        if "live" not in item.keywords:
            continue
        # Pin every live test to the session-scoped event loop so the
        # session-scoped solver fixture can share a single CloudflareSolver
        # (and its single Camoufox browser) across all live tests without
        # cross-loop asyncio-primitive errors.
        #
        # add_marker(pytest.mark.asyncio(...)) at this hook point is too
        # late: pytest-asyncio captures ``_loop_scope`` when it transforms
        # the test function into a ``Coroutine`` item earlier in collection.
        # Override the attribute directly so the live ``Coroutine``s run
        # on the session loop the ``_session_solver`` fixture lives on.
        item.add_marker(pytest.mark.asyncio(loop_scope="session"))
        if hasattr(item, "_loop_scope"):
            item._loop_scope = "session"  # type: ignore[attr-defined]
        callspec = getattr(item, "callspec", None)
        source_key = callspec.params.get("source_key") if callspec else None
        if source_key is None:
            continue
        profile = _load_profile(source_key)
        item.add_marker(
            pytest.mark.timeout(profile.download_timeout_s, method=timeout_method)
        )


@pytest.fixture(autouse=True)
def _restore_real_cloudflare_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the REAL ``CloudflareSolver.warm`` for live tests.

    ``tests/conftest.py:23-44`` registers an autouse session-wide no-op
    monkeypatch of ``CloudflareSolver.warm`` so the deterministic gate
    never tries to talk to real Cloudflare. Live tests are the explicit
    opt-in to the real path — they NEED ``warm()`` to actually solve the
    challenge. We rebind ``CloudflareSolver.warm`` to the frozen
    ``_ORIGINAL_CLOUDFLARE_WARM`` captured at conftest import (CR-02 /
    issue #29). Per-test additive monkeypatch — ``monkeypatch`` auto-undoes
    at fixture teardown, restoring the gate's no-op. No ``importlib.reload``,
    no ``sys.modules`` mutation, no divergent ``CloudflareSolver`` class
    object: any state on the module or class survives the test, and other
    modules that imported ``CloudflareSolver`` keep their original class
    reference intact.
    """
    monkeypatch.setattr(CloudflareSolver, "warm", _ORIGINAL_CLOUDFLARE_WARM)


# ─────────────────────── session-shared CloudflareSolver ───────────────────────
#
# cf-warm-cliff-followup mitigation. Building a fresh CloudflareSolver per live
# test (the prior baseline) spawns N Camoufox processes back-to-back from one
# residential IP. By the 6th comix test the warm() polling loop hit the 60s
# asyncio.wait_for ceiling 4 times in a row. Two earlier mitigations
# (cf-warm-burst-timeout.md "Mitigation Attempts") failed because the prior
# fixture either shared asyncio primitives across event loops or relied on
# user_data_dir cookie reuse without first proving the cookie was being
# honored byte-for-byte.
#
# This session fixture takes a third path:
#
#   1. The collection hook above marks every live test ``loop_scope="session"``,
#      so all live tests + this fixture share ONE event loop. Cross-loop
#      asyncio-primitive errors (the architectural blocker from Mitigation
#      Attempt 1) cannot occur.
#   2. ONE CloudflareSolver is built and warmed once for the whole live-test
#      session. The shared Camoufox browser stays up across every test.
#   3. The autouse ``_substitute_app_solver`` fixture below patches
#      ``manga_gateway.app.CloudflareSolver`` so per-test ``create_app``
#      lifespan construction returns this same session instance. The session
#      solver's ``aclose`` is neutered per-test so per-test lifespan teardown
#      cannot kill the shared browser; the real teardown happens once at
#      session shutdown.
#
# Bytes-comparison and subprocess-leak evidence informing the design lives at
# ``.planning/debug/cf-warm-cliff-followup.md``.


def _build_session_solver_kwargs() -> dict[str, Any]:
    """Mirror app.py's solver-kwargs build using a default ``Settings()``.

    Kept in lockstep with ``manga_gateway.app.lifespan`` (the solver_kwargs
    build, incl. the PROXY-01/#65 proxy gate) — the fields and source of each
    match. The only difference: this fixture uses
    a default-constructed ``Settings()`` (which picks up env vars exactly as
    production does), then derives ``cloudflare_keys`` and ``challenge_urls``
    (the #88 per-domain map) from the same registry inspection app.py performs.
    """
    # ``api_key`` is a required Settings field but does not feed any solver
    # kwarg; supply a session-internal placeholder. All cloudflare_* fields
    # still resolve from env vars exactly as production does.
    settings = Settings(api_key="session-solver-fixture-not-an-api-key")
    registry = SourceRegistry()
    register_builtin_sources(registry)
    cf_sources = {
        key: cls
        for key, cls in registry.items()
        if getattr(cls, "antibot", "none").startswith("cloudflare")
    }
    cloudflare_keys = frozenset(cf_sources)
    # Per-domain challenge-URL map (#88) — MUST match app.py's lifespan build
    # field-for-field (this fixture is the documented hand-mirror; a drift here
    # silently makes the live solver bypass per-domain clearance).
    challenge_urls = {
        key: url
        for key, cls in cf_sources.items()
        if (url := getattr(cls, "cloudflare_challenge_url", None))
    }
    kwargs: dict[str, Any] = {
        "user_data_dir": settings.cloudflare_user_data_dir,
        "headless": settings.cloudflare_headless,
        "solve_concurrency": settings.cloudflare_solve_concurrency,
        "fetch_concurrency": settings.cloudflare_fetch_concurrency,
        "cloudflare_keys": cloudflare_keys,
        "engine": settings.cloudflare_engine,
        "log_browser_events": settings.cloudflare_log_browser_events,
    }
    if challenge_urls:
        kwargs["challenge_urls"] = challenge_urls
    # PROXY-01 / #65: mirror the lifespan's proxy wiring so the session-shared
    # solver egresses through the SAME proxy as the per-test HttpxTransport
    # (which derives its own proxy from these same env-backed settings). Without
    # this, a proxy-configured live run would clear CF from the host IP while
    # httpx fetched images through the proxy — a split egress that cf_clearance
    # (IP-bound) would reject, the exact failure mode #65 guards against. Reads
    # the env-backed Settings only; no credential literal lives here.
    playwright_proxy, _ = build_proxy(settings)
    if playwright_proxy is not None:
        kwargs["proxy"] = playwright_proxy
    return kwargs


async def _noop_aclose() -> None:
    """No-op aclose: bound onto the session solver so per-test lifespan
    teardown (``app.py`` line 276 ``await solver.aclose()``) does NOT
    tear down the shared browser. The real aclose runs once at session
    teardown in ``_session_solver``'s finalizer.
    """
    return None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _session_solver() -> AsyncIterator[CloudflareSolver]:
    """Build ONE CloudflareSolver for the live-test session; warm it once.

    The warm uses the real ``_ORIGINAL_CLOUDFLARE_WARM`` captured at conftest
    import (before any per-test monkeypatch can interfere). On teardown, the
    REAL aclose is restored and called so the browser + Playwright instance
    are stopped cleanly when pytest exits — leaving no orphan Camoufox
    behind even if the suite was interrupted.
    """
    kwargs = _build_session_solver_kwargs()
    solver = CloudflareSolver(**kwargs)
    # Warm via the original (real) coroutine — the per-test autouse
    # _restore_real_cloudflare_warm fixture only runs at test scope, so at
    # session-fixture setup time the gate-wide no-op patch from
    # tests/conftest.py is still active on the class. Bind the original to
    # this instance so warm() actually solves.
    real_warm = _ORIGINAL_CLOUDFLARE_WARM.__get__(solver, CloudflareSolver)
    # Best-effort (debug/live-warm-fatal-session-gate): a slow/failed Cloudflare
    # clearance for ONE source must not error the shared session and take every
    # other (non-CF) source's tests down with it. See _warm_best_effort.
    await _warm_best_effort(real_warm, timeout=60.0)
    real_aclose = solver.aclose
    try:
        yield solver
    finally:
        # Restore and run the real teardown exactly once.
        solver.aclose = real_aclose  # type: ignore[method-assign]
        await solver.aclose()


@pytest.fixture(autouse=True)
def _substitute_app_solver(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make per-test ``create_app`` lifespans use the session-shared solver.

    Patches ``manga_gateway.app.CloudflareSolver`` to a factory that ignores
    its kwargs and returns the session solver. Each per-test ``lifespan()``
    call still executes ``CloudflareSolver(**solver_kwargs)`` (line 195 of
    app.py) — but that lookup resolves to this factory in the patched
    module namespace, so every test gets the SAME solver attached to its
    ``app.state.solver``.

    The session solver's ``aclose`` is bound to a per-instance no-op so the
    lifespan's ``await solver.aclose()`` at shutdown (app.py line 276) is a
    harmless await that does NOT close the shared Camoufox browser. The
    real teardown runs once in ``_session_solver``'s finalizer.

    Gate scope: this fixture is autouse only within ``tests/live/``. Gate
    tests in ``tests/`` see no substitution and build/tear down real
    CloudflareSolvers as before — which under the gate's autouse warm
    monkeypatch from ``tests/conftest.py`` never actually touches a browser.
    """
    if "live" not in request.node.keywords:
        return
    session_solver: CloudflareSolver = request.getfixturevalue("_session_solver")
    # Neuter aclose on the session instance for the duration of the live
    # test session. We restore the original method only inside the
    # ``_session_solver`` finalizer (above). Setting on the instance does
    # not affect the class, so other tests that build a real solver via
    # the un-patched path are unaffected.
    session_solver.aclose = _noop_aclose  # type: ignore[method-assign]

    def _factory(**_kwargs: Any) -> CloudflareSolver:
        return session_solver

    # Substitute the import-resolved symbol in app.py's namespace. Any
    # ``CloudflareSolver(**solver_kwargs)`` call from inside lifespan()
    # now returns the session instance. monkeypatch auto-undoes on teardown.
    monkeypatch.setattr(_app_module, "CloudflareSolver", _factory)


# ─────────────────────── subprocess-count diagnostic ───────────────────────
#
# Cheap insurance recommended by the cf-warm-cliff-followup adversarial
# verification (`.planning/debug/cf-warm-cliff-followup.md` "Adversarial
# Verification" section, recommendation 3). Prints the live Camoufox /
# firefox / node process count immediately before each live test runs, so
# if the cliff returns we have an inline accumulation signal in the test
# log without needing to re-spin a separate harness.

_PS_SNAPSHOT_CMD = (
    "Get-Process | Where-Object { "
    "$_.ProcessName -match 'camoufox|firefox|node' "
    "} | Group-Object ProcessName | Select-Object Name, Count | "
    "ConvertTo-Json -Compress -AsArray"
)


def _snapshot_browser_processes() -> str:
    """Return a compact one-liner summary of live browser-like processes.

    Windows-only (uses powershell). On non-Windows hosts the snapshot
    returns ``"n/a (non-win32)"`` so the diagnostic is a no-op there.
    """
    if sys.platform != "win32":
        return "n/a (non-win32)"
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", _PS_SNAPSHOT_CMD],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception as exc:  # noqa: BLE001
        return f"snapshot_error: {type(exc).__name__}: {exc}"
    raw = (result.stdout or "").strip()
    if not raw:
        return "camoufox=0 firefox=0 node=0"
    return raw.replace("\n", " ")


@pytest.fixture(autouse=True)
def _live_browser_process_snapshot(request: pytest.FixtureRequest) -> None:
    """Print the browser-process snapshot before each live test runs.

    Inline in the pytest output so a regression of the cf-warm-cliff
    symptom is visible without re-spinning a separate harness. Live-only
    (gates see no extra logging).
    """
    if "live" not in request.node.keywords:
        return
    snapshot = _snapshot_browser_processes()
    # ASCII-only — Windows cp1252 console can't encode '->' arrow glyphs.
    print(f"\n[live procs pre-test] {request.node.nodeid} -> {snapshot}")
