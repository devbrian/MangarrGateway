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
import functools
import importlib
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio

import manga_gateway.app as _app_module
from manga_gateway.config import Settings
from manga_gateway.framework.android_solver import AndroidSolver
from manga_gateway.framework.antibot import CloudflareSolver
from manga_gateway.framework.lanes import DEFAULT_LANE
from manga_gateway.framework.proxy import build_proxy
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.framework.source_pin import SourcePinnedProxies
from manga_gateway.sources import register_builtin_sources

from .profiles._base import LiveSmokeProfile


def _disabled_from_env() -> frozenset[str]:
    """Parse ``GATEWAY_DISABLED_SOURCES`` exactly as the app does.

    Mirrors ``Settings.disabled_source_keys()`` without constructing
    ``Settings`` (which would require an api_key just to read one env var).
    Used by BOTH ``_registered_keys`` (D-47 collection) and ``_ANDROID_KEYS``
    (the #215 reachability gate) so collection, the gate, and the app agree
    byte-for-byte on the disabled set.
    """
    return frozenset(
        key.strip().lower()
        for key in os.environ.get("GATEWAY_DISABLED_SOURCES", "").split(",")
        if key.strip()
    )


def _registered_keys() -> list[str]:
    """Enumerate registered source keys via ``SourceRegistry`` (D-47).

    A fresh ``SourceRegistry()`` is constructed every call so this function
    is side-effect-free against any process-level registry the app shell
    happens to own. Returns ``list[str]`` in registration order
    (``register_builtin_sources`` registers MangaDex then Comix, but
    callers MUST NOT depend on the order — the source set is unordered).
    """
    reg = SourceRegistry()
    # #198/#202: honor GATEWAY_DISABLED_SOURCES so live-smoke collection matches
    # what the app actually serves — a disabled source is not registered, hence
    # not parametrized (no profile load needed for it).
    register_builtin_sources(reg, disabled=_disabled_from_env())
    return reg.keys()


def _android_keys() -> frozenset[str]:
    """Registered source keys whose ``solver_engine`` is ``"android"`` (#215).

    Built the SAME way ``_registered_keys`` builds — a fresh ``SourceRegistry``
    honoring ``GATEWAY_DISABLED_SOURCES`` — then filtered to classes that route
    their Cloudflare solve through the Android-WebView leg. These are the only
    keys the runtime reachability gate probes, so a non-android live item makes
    ZERO network calls.
    """
    reg = SourceRegistry()
    register_builtin_sources(reg, disabled=_disabled_from_env())
    return frozenset(
        key
        for key, cls in reg.items()
        if getattr(cls, "solver_engine", "patchright") == "android"
    )


@functools.cache
def _android_solver_reachable() -> tuple[bool, str]:
    """Once-per-session probe of the home android-solver's ``/healthz`` (#215).

    Returns ``(True, "")`` only when ``GATEWAY_ANDROID_SOLVER_URL`` is set AND
    ``GET {url}/healthz`` answers HTTP 200. Otherwise returns
    ``(False, <reason>)`` — URL unset, a non-200 status, or any transport error
    — WITHOUT raising, so the live-collection gate can turn an unreachable home
    stack into a clean ``<skipped>`` instead of a test ERROR (a down home stack
    must never red the nightly). ``functools.cache`` makes this a single probe
    for the whole session. Never logs or interpolates the api key.
    """
    url = os.environ.get("GATEWAY_ANDROID_SOLVER_URL", "").strip()
    if not url:
        return (
            False,
            "GATEWAY_ANDROID_SOLVER_URL unset — android solver not configured",
        )
    healthz = f"{url.rstrip('/')}/healthz"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(healthz)
    except Exception as exc:  # noqa: BLE001 — gate must never raise (httpx.HTTPError/OSError/anything)
        return (
            False,
            f"android solver unreachable ({type(exc).__name__})",
        )
    if resp.status_code == 200:
        return (True, "")
    return (
        False,
        f"android solver /healthz returned {resp.status_code} (not 200)",
    )


# D-47: materialize at conftest-import (= live-test-collection) time, NOT
# inside a fixture. The parametrize decorator in each smoke module reads
# this constant during the collection phase before any fixture runs.
# Type is inferred from _registered_keys' return annotation. The literal
# assignment shape is also the contract the acceptance-criteria grep checks.
REGISTERED_KEYS = _registered_keys()

# #215 Model A: the subset of REGISTERED_KEYS that route their Cloudflare solve
# through the Android-WebView leg (mangadot/kagane/mangaball). Materialized at
# collection time alongside REGISTERED_KEYS; the collection hook probes the home
# android-solver ONLY for items keyed to one of these, so the deterministic gate
# (live items already deselected) makes no network call.
_ANDROID_KEYS: frozenset[str] = _android_keys()


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


# Per-cloudflare-domain wall-clock budget for the shared-session warm. The inner
# CloudflareSolver._solve_real has its own 60s cf_clearance deadline PER domain
# (src/manga_gateway/framework/antibot.py), and warm() solves each cloudflare-
# gated source SEQUENTIALLY on the one shared context. 75s = that 60s inner
# deadline + headroom for launch/goto/poll overhead.
_PER_DOMAIN_WARM_BUDGET_S = 75.0


def _session_warm_budget_s(num_cf_domains: int) -> float:
    """Total wall-clock ceiling for the shared-session warm, scaled by #CF domains.

    debug datacenter-cf-warm-regression (secondary finding): the warm was bounded
    by a single flat ``asyncio.wait_for(warm(), 60.0)``. But ``warm()`` solves N
    cloudflare-gated domains sequentially, EACH with its own internal 60s
    ``_solve_real`` deadline — so a flat 60s outer ceiling cannot even cover one
    slow inner solve plus a second domain. The moment any single domain is slow
    (e.g. comix on a flagged datacenter IP), the outer ``wait_for`` cancels
    mid-solve and misreports the WHOLE warm as failed, even when the other
    domain(s) cleared fine. Scale the ceiling with the number of cf-gated domains
    (``_PER_DOMAIN_WARM_BUDGET_S`` each), floored at one domain so a zero/one-CF
    session still gets a sane budget.
    """
    return _PER_DOMAIN_WARM_BUDGET_S * max(1, num_cf_domains)


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
        # CI gating (#197/#198): a source whose profile declares ``ci_skip_reason``
        # cannot pass in the nightly environment for a tracked reason — skip every
        # one of its parametrized live tests rather than let it be a perpetual
        # false-red. ``<skipped>`` is non-failing to ``scripts/nightly_triage.py``,
        # so the source's sticky nightly issue auto-closes while the real fix is
        # tracked in the referenced issue.
        if profile.ci_skip_reason:
            item.add_marker(pytest.mark.skip(reason=profile.ci_skip_reason))
        # Android reachability gate (#215 Model A). The three android-engine
        # sources (mangadot/kagane/mangaball) are no longer unconditionally
        # ci_skip'd — they RUN when the home android-solver answers /healthz and
        # SKIP (not fail) when it does not, so a down home stack triages clean
        # instead of ERRORing the search leg's AndroidSolver /solve. Probe ONLY
        # android-keyed items (the cached probe fires at most once per session),
        # so non-android live items make zero network calls.
        if source_key in _ANDROID_KEYS:
            reachable, reason = _android_solver_reachable()
            if not reachable:
                item.add_marker(
                    pytest.mark.skip(
                        reason=(
                            f"android solver unreachable ({reason}) — #215 Model A: "
                            "home android-solver must be up + tailnet-reachable"
                        )
                    )
                )
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
    # #198/#202: honor GATEWAY_DISABLED_SOURCES here too (mirrors app.py exactly)
    # so the session-shared solver's cloudflare_keys EXCLUDE disabled sources —
    # otherwise a disabled-but-unclearable source (kagane/mangadot) would still be
    # warmed at session setup, re-introducing the warm storm and diverging from
    # both REGISTERED_KEYS and the app's registry.
    register_builtin_sources(registry, disabled=settings.disabled_source_keys())
    cf_sources = {
        key: cls
        for key, cls in registry.items()
        if getattr(cls, "antibot", "none").startswith("cloudflare")
    }
    # Phase 10: this session-shared CloudflareSolver IS the router's Patchright leg
    # (the autouse ``_substitute_app_solver`` injects it where app.py builds the
    # CloudflareSolver). PARTITION cf_sources to patchright sources ONLY (exclude
    # ``solver_engine == "android"``) — mirroring app.py's partition — so the live
    # session never tries to Patchright-warm the unclearable-from-Linux mangadot/
    # kagane (they route to the AndroidSolver leg, ci_skip-gated, no redroid in CI).
    patchright_sources = {
        key: cls
        for key, cls in cf_sources.items()
        if getattr(cls, "solver_engine", "patchright") != "android"
    }
    cloudflare_keys = frozenset(patchright_sources)
    # Per-domain challenge-URL map (#88) — MUST match app.py's lifespan build
    # field-for-field (this fixture is the documented hand-mirror; a drift here
    # silently makes the live solver bypass per-domain clearance). Partitioned to
    # patchright sources only, exactly like app.py's Patchright-leg challenge map.
    challenge_urls = {
        key: url
        for key, cls in patchright_sources.items()
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


def _build_session_android_solver_kwargs() -> dict[str, Any]:
    """Documented hand-mirror of app.py's ``AndroidSolver(...)`` build.

    Kept in LOCKSTEP with ``manga_gateway.app.lifespan``'s android-leg
    construction (the ``android_challenge_urls`` map + the ``AndroidSolver(...)``
    kwargs) exactly as ``_build_session_solver_kwargs`` mirrors the Patchright
    leg. A silent drift here breaks the live android leg: the session-shared
    AndroidSolver would warm/solve a different set of sources (or none) than the
    per-test ``create_app`` lifespan expects, so the held ``cf_clearance`` would
    not be reused and every android live test would re-mint via the sidecar.

    Like ``_build_session_solver_kwargs`` this uses a default-constructed
    ``Settings()`` (picking up env vars exactly as production does), a fresh
    ``SourceRegistry`` honoring ``GATEWAY_DISABLED_SOURCES``, and the SAME
    registry partition app.py performs: cloudflare-gated sources whose
    ``solver_engine`` is ``"android"``. The ``proxy`` kwarg mirrors app.py's
    ``proxy=playwright_proxy`` — passed unconditionally (``None`` when
    ``cloudflare_proxy_*`` is unconfigured, exactly like the lifespan).
    """
    # ``api_key`` is a required Settings field but does not feed any AndroidSolver
    # kwarg; supply a session-internal placeholder. All android_solver_* fields
    # still resolve from env vars exactly as production does.
    settings = Settings(api_key="session-solver-fixture-not-an-api-key")
    registry = SourceRegistry()
    register_builtin_sources(registry, disabled=settings.disabled_source_keys())
    cf_sources = {
        key: cls
        for key, cls in registry.items()
        if getattr(cls, "antibot", "none").startswith("cloudflare")
    }
    # PARTITION cf_sources to ANDROID sources ONLY (``solver_engine == "android"``)
    # — the mirror image of ``_build_session_solver_kwargs``'s patchright partition.
    android_sources = {
        key: cls
        for key, cls in cf_sources.items()
        if getattr(cls, "solver_engine", "patchright") == "android"
    }
    # Per-domain android challenge-URL map — MUST match app.py's lifespan build
    # field-for-field (app.py: ``key in android_keys and (url := ...)``).
    android_challenge_urls = {
        key: url
        for key, cls in android_sources.items()
        if (url := getattr(cls, "cloudflare_challenge_url", None))
    }
    # On-demand keys (debug pooltimeout-recurrence): MUST mirror app.py's lifespan,
    # which passes ``on_demand_keys=on_demand_keys & android_keys`` so ``warm()`` SKIPS
    # an intermittent-challenge source (mangaball). Without this the session-shared
    # AndroidSolver eager-warms mangaball against the contended home sidecar → 504 →
    # D-33 force-disable → every mangaball live test fails (sticky #261), even though
    # production (which DOES pass on_demand_keys) skips it correctly. This is exactly
    # the "silent drift breaks the live android leg" hazard this hand-mirror warns of.
    on_demand_keys = frozenset(
        key
        for key, cls in android_sources.items()
        if getattr(cls, "cloudflare_challenge_optional", False)
    )
    # Bug 5 follow-on #3 (A2): page-holding android sources (``holds_webview_page`` —
    # comix) are EXCLUDED from the eager ``cf_clearance`` ``/solve`` and EVAL-PRIMED
    # instead — ``/solve`` runs ``pm clear``, cold-wiping the warm WebView (cf_clearance
    # + __cf_bm + cached env-*.js) comix depends on, so a comix ``/solve`` mints no
    # clearance and burns its deadline → 503/504 → D-33 force-disable → every comix live
    # test fails. MUST mirror app.py's lifespan ``warm_last_keys=webview_page_keys`` —
    # the same "silent drift breaks the live android leg" hazard that bit on_demand_keys
    # (``test_android_solver_kwargs_lockstep`` now AST-guards exactly this drift).
    warm_last_keys = frozenset(
        key
        for key, cls in android_sources.items()
        if getattr(cls, "holds_webview_page", False)
    )
    # PROXY-01 / Req 7: mirror the lifespan's proxy wiring so the session-shared
    # AndroidSolver's /solve egress matches the per-test httpx-fetch egress (one
    # IP — cf_clearance is IP-bound). app.py passes ``proxy=playwright_proxy``
    # UNCONDITIONALLY (None when unconfigured), so do the same here.
    playwright_proxy, _ = build_proxy(settings)
    # Phase 16 (PROXY-04/PROXY-06): mirror the lifespan's pinned-proxy wiring. app.py
    # builds ONE ``SourcePinnedProxies(image_proxy_pool)`` and passes it +
    # ``solve_search_keys=(solve_search_keys & plan.source_keys)`` to each solver.
    # The session-shared solver is the single-lane collapse (all android keys on one
    # lane), so the slice is ``solve_search_keys & android_keys``. The pool is None here
    # (the live session-shared solver is built without an image_proxy_pool) ⇒ the
    # singleton is a no-op, byte-for-byte. Mirrored so the lockstep AST guard stays ok.
    solve_search_keys = frozenset(
        key
        for key, cls in registry.items()
        if getattr(cls, "solve_search_via_proxy_pool", False)
    )
    android_solve_search_keys = solve_search_keys & frozenset(android_sources)
    source_pins = SourcePinnedProxies(None)
    # Lockstep with app.py's lifespan ``AndroidSolver(...)`` build: EVERY kwarg app.py
    # passes must appear here (``test_android_solver_kwargs_lockstep`` AST-asserts the
    # two sets are equal). ``clearance_lifetime_s`` (Lever A) + ``warm_gate_timeout_s``
    # (Fix #2) default-resolve from the SAME Settings fields app.py reads, so the
    # session-shared solver behaves identically to production. The ``client`` kwarg is
    # the one exception neither passes (the solver lazily builds its own).
    return {
        "base_url": settings.android_solver_url,
        "api_key": settings.android_solver_api_key,
        "challenge_urls": android_challenge_urls,
        "on_demand_keys": on_demand_keys,
        "warm_last_keys": warm_last_keys,
        "clearance_lifetime_s": settings.android_clearance_lifetime_s,
        "warm_gate_timeout_s": settings.android_warm_gate_timeout_s,
        "timeout_s": settings.android_solver_timeout_s,
        # Debug kagane-search-timeout part 2: the block-window discriminator (mirror of
        # the sidecar inner tap-poll deadline). Same Settings field app.py reads.
        "solve_block_deadline_s": settings.android_solve_block_deadline_s,
        "proxy": playwright_proxy,
        # LANE-02 (Plan 15-04): the session-shared solver mirrors the single-lane
        # collapse — adb_target=None (no ``target`` sent) + lane="default". Present here
        # so test_android_solver_kwargs_lockstep stays green against app.py's per-lane
        # AndroidSolver(...) build (which now passes adb_target=plan.adb_target + lane).
        "adb_target": None,
        "lane": DEFAULT_LANE,
        # Phase 16 (PROXY-04/PROXY-06): the pinned-proxy singleton + this lane's slice
        # of opted-in source keys, mirroring app.py's per-lane AndroidSolver(...) build.
        "source_pins": source_pins,
        "solve_search_keys": android_solve_search_keys,
    }


def _session_android_warm_budget_s(num_android_keys: int) -> float:
    """Total wall-clock ceiling for the shared-session ANDROID warm.

    An android solve routes through the redroid-WebView sidecar's ``/solve`` and
    is SERIALIZED through ONE device — each mint is ~1–2 min (the gateway client
    timeout is ``android_solver_timeout_s``, default 180s). The flat CF budget
    (``_PER_DOMAIN_WARM_BUDGET_S`` = 75s) is far too tight here, so scale the
    ceiling by the number of android challenge keys at the per-solve timeout plus
    a launch/transport margin, floored at one key so a zero/one-android session
    still gets a sane budget. EAGER warming in ``_session_android_solver`` keeps
    this whole budget OFF every per-test clock (see that fixture).
    """
    settings = Settings(api_key="session-solver-fixture-not-an-api-key")
    margin_s = 30.0
    return settings.android_solver_timeout_s * max(1, num_android_keys) + margin_s


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
    # Budget scales with the number of cf-gated domains (debug
    # datacenter-cf-warm-regression): warm() solves them sequentially, each with
    # its own 60s inner deadline, so a flat 60s ceiling false-fails an N>1 warm.
    num_cf_domains = len(kwargs.get("cloudflare_keys") or ())
    await _warm_best_effort(real_warm, timeout=_session_warm_budget_s(num_cf_domains))
    real_aclose = solver.aclose
    try:
        yield solver
    finally:
        # Restore and run the real teardown exactly once.
        solver.aclose = real_aclose  # type: ignore[method-assign]
        await solver.aclose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _session_android_solver() -> AsyncIterator[AndroidSolver]:
    """Build ONE AndroidSolver for the live-test session; EAGER-warm it once.

    The android live tests (mangadot/kagane/mangaball) route their Cloudflare
    solve through the redroid-WebView sidecar, serialized through ONE device at
    ~1–2 min per mint. Building a fresh AndroidSolver per ``create_app`` lifespan
    (the prior baseline) leaves ``AndroidSolver._held`` empty each test, so every
    android live test re-mints ``cf_clearance`` via ``/solve`` — unacceptable
    wall-clock + device load. Sharing ONE instance across the session means the
    held clearance is reused: ONE ``/solve`` per source, not per test.

    The warm is EAGER and BEST-EFFORT (mirroring ``_session_solver``): we warm at
    session-fixture setup so the ~1–2 min first-mint per source lands OFF every
    per-test clock (the D-55 ``pytest.mark.timeout`` is the per-source
    ``download_timeout_s`` — far too tight to absorb an inline first-mint). An
    unconfigured/unreachable/uncleared android stack must NOT error the session
    and take comix/mangadex tests down: ``warm()`` raises per key when
    ``android_solver_url`` is unset, and ``_warm_best_effort`` swallows it. The
    #215 reachability gate already skips the android items in that case, so a
    non-android live run pays ~no cost here.
    """
    kwargs = _build_session_android_solver_kwargs()
    solver = AndroidSolver(**kwargs)
    # AndroidSolver.warm is NOT monkeypatched by the gate (the no-op warm patch in
    # tests/conftest.py targets CloudflareSolver.warm only), so call it directly —
    # no _ORIGINAL_* capture dance needed.
    num_android_keys = len(kwargs.get("challenge_urls") or ())
    await _warm_best_effort(
        solver.warm, timeout=_session_android_warm_budget_s(num_android_keys)
    )
    real_aclose = solver.aclose
    try:
        yield solver
    finally:
        # Restore and run the real teardown exactly once (closes the owned httpx
        # client to the sidecar). Per-test SolverRouter teardown neutered it.
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

    # ── share ONE AndroidSolver across the session, same pattern (#215 follow-up) ──
    # Each per-test ``create_app`` lifespan builds a fresh AndroidSolver, leaving its
    # ``_held`` clearance cache empty → every android live test would re-mint
    # ``cf_clearance`` via the sidecar ``/solve`` (~1–2 min each, serialized through
    # ONE redroid). Substituting the shared instance means the per-test lifespan's
    # ``AndroidSolver(**kwargs)`` returns it, so ``_held`` persists across every live
    # test: ONE ``/solve`` per source, not per test.
    session_android: AndroidSolver = request.getfixturevalue("_session_android_solver")
    # Neuter aclose on the shared android instance for the session: the per-test
    # SolverRouter wraps BOTH shared backends and its lifespan teardown calls
    # ``await solver.aclose()`` — which (SolverRouter.aclose) closes BOTH legs. Left
    # un-neutered, per-test teardown would close the shared sidecar httpx client.
    # The real aclose is restored + run once in ``_session_android_solver``'s
    # finalizer (same contract as the CloudflareSolver above).
    session_android.aclose = _noop_aclose  # type: ignore[method-assign]

    def _android_factory(**_kwargs: Any) -> AndroidSolver:
        return session_android

    monkeypatch.setattr(_app_module, "AndroidSolver", _android_factory)


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
