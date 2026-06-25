"""Per-source anti-bot engine dispatch (Phase 10, SRC-01/SRC-02).

``SolverRouter`` is an :class:`~manga_gateway.framework.antibot.AntiBotSolver`
composite that dispatches ``get_clearance`` PER SOURCE to the right backend engine:
comix → the desktop :class:`CloudflareSolver` (Patchright, byte-for-byte unchanged);
mangadot/kagane → the :class:`AndroidSolver` (the redroid-WebView sidecar). The engine
for each source is named by its ``Source.solver_engine`` class-attr (default
``"patchright"``) and threaded in as a ``{source_key: engine}`` map by the caller (the
app lifespan, Plan 10-04) — the router itself NAMES NO SOURCE and holds no Android
machinery (R1: it only composes two backends).

The router satisfies the runtime-checkable ``AntiBotSolver`` Protocol so the lifespan
can drop it in as the ONE shared ``app.state.solver`` with no call-site change. The
internal ``force_resolve`` keyword (D-35 re-solve) is passed through to whichever
backend owns the key — detected via ``inspect.signature`` exactly like context.py's
``_call_solver`` (D-41), so a backend that does not declare it is still called cleanly.
``warm()`` warms BOTH backends and returns the UNION of their failed keys (so the
lifespan force-disables exactly the failed sources across both engines); ``aclose()``
closes BOTH (each guarded so one failure does not skip the other).

Beyond the clearance Protocol the router ALSO passes through the three off-Protocol
browser primitives (``fetch_via_browser`` / ``fetch_via_browser_paginated`` /
``fetch_via_browser_parallel_pages``, D-41) so that
swapping the bare :class:`CloudflareSolver` for this router as ``app.state.solver``
does not break a source's ``_solver_from_ctx`` ``hasattr`` gate (comix uses
``fetch_via_browser`` + ``fetch_via_browser_parallel_pages``). All delegate to the
patchright backend (the only engine with a real
browser; the android backend is a WebView-clearance sidecar with no browser and never
needs these), keeping criterion 4 "comix unaffected" true.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from .lanes import DEFAULT_LANE

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractAsyncContextManager

    from .antibot import AntiBotSolver, Clearance

_log = logging.getLogger("manga_gateway")


class _BrowserFetchSolver(Protocol):
    """The three off-Protocol browser primitives (D-41) the patchright backend exposes.

    Declared locally — mirroring comix's own duck-typing — so the router can
    ``cast`` ``self._patchright`` (typed as the browser-agnostic ``AntiBotSolver``
    Protocol, which intentionally omits these) to a shape that declares them, and
    delegate mypy-strict-clean with NO ``# type: ignore``.
    """

    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> Any: ...

    async def fetch_via_browser_paginated(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        next_selector: str,
        route_limit_rewrite: tuple[str, int] | None = None,
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> list[Any]: ...

    async def fetch_via_browser_parallel_pages(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        last_page_selector: str,
        page_param: str,
        route_rewrite: tuple[str, int],
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> list[Any]: ...


class _EvalSolver(Protocol):
    """The off-Protocol ``eval_in_webview`` primitive the ANDROID backend exposes.

    Declared locally — mirroring :class:`_BrowserFetchSolver` — so the router can
    ``cast`` ``self._eval_backend`` (typed as the engine-agnostic ``AntiBotSolver``
    Protocol, which intentionally omits it) to a shape that declares it, and
    delegate mypy-strict-clean with NO ``# type: ignore``. The INVERSE of the four
    ``fetch_via_browser*`` passthroughs: those cast ``self._patchright`` (the only
    engine with a real browser); this one casts ``self._eval_backend`` (the page-holder
    lane's AndroidSolver — the only engine with the cleared WebView the eval runs
    inside; LANE-02).
    """

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — matches the backend's op-budget kwarg
    ) -> Any: ...

    def device_session(self) -> AbstractAsyncContextManager[None]:
        """The bug-4 Fix C foreground-device lease the android backend exposes.

        comix wraps its whole solve+eval sequence in ``async with
        solver.device_session():`` so the proactive-refresh loop defers and the warm
        comix page survives across the sequence's evals. Declared here (not a
        coroutine — it returns an async context manager) so the router can ``cast``
        ``self._eval_backend`` and delegate mypy-strict-clean.
        """
        ...


# The android engine name (matches ``Source.solver_engine`` on mangadot/kagane).
# Every other value (notably the ``"patchright"`` default) routes to the desktop
# backend — the router is permissive so an unmapped/unknown key falls through to the
# patchright backend, which already returns None for keys it does not own (BOT-01).
_ANDROID_ENGINE = "android"


class SolverRouter:
    """Composite AntiBotSolver dispatching ``get_clearance`` by per-source engine.

    LANE-02 (Plan 15-04): the single ``android`` backend is replaced by an
    ``android_by_lane`` dict (one ``AndroidSolver`` per lane = per redroid device, each
    with its OWN device lock) plus a ``source_lane_map`` (source_key → lane_id) and an
    ``eval_backend`` (the page-holder lane's instance). An android source routes to its
    lane's instance; the off-Protocol ``eval_in_webview``/``device_session`` pass-
    throughs route to the page-holder lane (``eval_backend``), so comix's foreground
    lease defers only its own lane's refresh. ``warm()``/``aclose()`` iterate EVERY
    lane. With a single lane (``android_by_lane={"default": solver}``,
    ``source_lane_map={}``,
    ``eval_backend=solver``) this is behaviorally identical to the old single-android
    router — the single-lane collapse.
    """

    def __init__(
        self,
        *,
        patchright: AntiBotSolver,
        android_by_lane: Mapping[str, AntiBotSolver],
        source_lane_map: Mapping[str, str],
        eval_backend: AntiBotSolver,
        engine_by_source: Mapping[str, str],
    ) -> None:
        self._patchright = patchright
        # One AndroidSolver per lane (per redroid device); copied so a later mutation
        # of the caller's dict cannot re-point a lane mid-flight.
        self._android_by_lane = dict(android_by_lane)
        # source_key → lane_id; an unmapped android source falls to ``DEFAULT_LANE``.
        self._source_lane_map = dict(source_lane_map)
        # The page-holder lane's AndroidSolver — the target of the off-Protocol eval
        # passthroughs (eval_in_webview/device_session). In the single-lane collapse
        # this IS the one-and-only AndroidSolver.
        self._eval_backend = eval_backend
        self._engine_by_source = dict(engine_by_source)

    def _backend_for(self, source_key: str) -> AntiBotSolver:
        """Select the backend for ``source_key`` from the engine + lane maps.

        ``"android"`` → the AndroidSolver of the source's lane
        (``source_lane_map`` → ``android_by_lane``; an unmapped source uses
        ``DEFAULT_LANE``, and a resolved lane MISSING from ``android_by_lane`` falls
        back to the ``DEFAULT_LANE`` instance — SEC-01/T-15-07: never an arbitrary
        device). Everything else (the ``"patchright"`` default AND any unmapped key) →
        the desktop CloudflareSolver. Both backends return ``None`` for a key they do
        not own, so an
        unmapped/non-cloudflare key resolves to ``None`` whichever backend it lands on
        (BOT-01).
        """
        engine = self._engine_by_source.get(source_key, "patchright")
        if engine != _ANDROID_ENGINE:
            return self._patchright
        lane = self._source_lane_map.get(source_key, DEFAULT_LANE)
        return self._android_by_lane.get(lane, self._android_by_lane[DEFAULT_LANE])

    async def get_clearance(
        self,
        source_key: str,
        *,
        force_resolve: bool = False,
        solve_if_missing: bool = True,
    ) -> Clearance | None:
        """Dispatch to the mapped backend's ``get_clearance`` (D-41 pass-through).

        ``force_resolve`` (the D-35 403 self-heal) and ``solve_if_missing`` (the
        on-demand peek — ``False`` serves only already-held clearance) are forwarded
        ONLY when the chosen backend declares them as keywords — mirroring context.py's
        ``_call_solver`` ``inspect.signature`` detection so the public ``AntiBotSolver``
        seam stays unchurned (D-41). ``solve_if_missing=True`` (the default) is never
        forwarded, so eager dispatch is byte-for-byte unchanged.
        """
        backend = self._backend_for(source_key)
        get = backend.get_clearance
        params = inspect.signature(get).parameters
        kwargs: dict[str, Any] = {}
        if force_resolve and "force_resolve" in params:
            kwargs["force_resolve"] = True
        if not solve_if_missing and "solve_if_missing" in params:
            kwargs["solve_if_missing"] = False
        return await get(source_key, **kwargs)

    # ── off-Protocol browser primitives (D-41) — pass-through for comix ──────────
    # Delegate to the PATCHRIGHT backend, never android: comix is the only caller
    # and the only engine with a real browser is patchright (android is the
    # WebView-sidecar clearance backend, no browser). These keep comix's
    # ``_solver_from_ctx`` ``hasattr`` gate satisfied so the router is a faithful
    # superset of the bare CloudflareSolver (criterion 4: comix unaffected).
    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> Any:
        """Delegate the one-shot browser-DOM read to the patchright backend (D-41)."""
        backend = cast("_BrowserFetchSolver", self._patchright)
        return await backend.fetch_via_browser(
            url, extract=extract, wait_for=wait_for, timeout=timeout
        )

    async def fetch_via_browser_paginated(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        next_selector: str,
        route_limit_rewrite: tuple[str, int] | None = None,
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> list[Any]:
        """Delegate the paginated browser-DOM walk to the patchright backend (D-41)."""
        backend = cast("_BrowserFetchSolver", self._patchright)
        return await backend.fetch_via_browser_paginated(
            url,
            extract=extract,
            wait_for=wait_for,
            next_selector=next_selector,
            route_limit_rewrite=route_limit_rewrite,
            max_pages=max_pages,
            timeout=timeout,
        )

    async def fetch_via_browser_parallel_pages(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        last_page_selector: str,
        page_param: str,
        route_rewrite: tuple[str, int],
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> list[Any]:
        """Delegate the parallel-pagination browser fan-out to the patchright backend.

        comix's chapter-list enumeration (``_fetch_series_chapters_raw``) runs against
        the production ``app.state.solver`` — which is THIS router — so the router must
        expose the parallel primitive too (D-41). Always delegates to
        ``self._patchright`` (the android backend has no real browser)."""
        backend = cast("_BrowserFetchSolver", self._patchright)
        return await backend.fetch_via_browser_parallel_pages(
            url,
            extract=extract,
            wait_for=wait_for,
            last_page_selector=last_page_selector,
            page_param=page_param,
            route_rewrite=route_rewrite,
            max_pages=max_pages,
            timeout=timeout,
        )

    # ── off-Protocol in-WebView eval (EVAL-02) — pass-through for comix ──────────
    # The INVERSE of the four browser passthroughs above: this delegates to the
    # ANDROID backend, NOT patchright. comix runs its in-page token-mint /
    # chapter-list / manifest JS inside the redroid WebView (the only fingerprint
    # that clears comix.to's Cloudflare); only the android backend owns that
    # cleared WebView. Plan 03 flips comix's ``_solver_from_ctx`` ``hasattr`` gate
    # to ``eval_in_webview`` and calls it through this router.
    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — matches the backend's op-budget kwarg
    ) -> Any:
        """Delegate the in-WebView JS eval to the page-holder lane (EVAL-02 / LANE-02).

        Unlike the three ``fetch_via_browser*`` passthroughs (which route to
        ``self._patchright``), this routes to ``self._eval_backend`` — the page-holder
        lane's AndroidSolver, the engine that owns the Turnstile-cleared redroid WebView
        the eval runs inside. comix runs in the page-holder lane, so its eval never
        lands on another lane's device (T-15-08). In the single-lane collapse this is
        the one AndroidSolver.
        """
        backend = cast("_EvalSolver", self._eval_backend)
        return await backend.eval_in_webview(
            challenge_url, js, wait_for=wait_for, timeout=timeout
        )

    def device_session(self) -> AbstractAsyncContextManager[None]:
        """Delegate the foreground-device lease to the page-holder lane (bug 4 Fix C).

        Like :meth:`eval_in_webview` (and unlike the three ``fetch_via_browser*``
        passthroughs), this routes to ``self._eval_backend`` — the page-holder lane's
        AndroidSolver, the engine that owns comix's redroid. comix wraps its solve+eval
        sequence in ``async with self._solver_from_ctx(ctx).device_session():`` so ONLY
        that lane's proactive-refresh loop defers and the warm comix page survives
        across the sequence (T-15-08). It is a no-op-safe CM even when the backend is
        unconfigured (``base_url is None``): the lease is just a counter, so a local
        box / CI / the gate without a redroid enters+exits it cleanly (the evals raise
        ``RuntimeError`` anyway). Returns the CM (not a coroutine) so callers use it
        directly as ``async with``.
        """
        backend = cast("_EvalSolver", self._eval_backend)
        return backend.device_session()

    async def warm(self) -> list[str]:
        """Warm patchright + EVERY lane; return the UNION of failed keys (deduped).

        Each backend's ``warm()`` returns the source keys whose eager solve failed
        (D-33). The lifespan force-disables exactly those, so the router returns the
        union across patchright AND every lane's AndroidSolver (first-seen order,
        deduped). Every backend is warmed unconditionally — one backend's warm failure
        must NOT skip the others (LANE-02: a dedicated lane's warm is independent).
        """
        failed: list[str] = []
        seen: set[str] = set()
        for backend in self._all_backends():
            for key in await self._warm_backend(backend):
                if key not in seen:
                    seen.add(key)
                    failed.append(key)
        return failed

    def _all_backends(self) -> list[AntiBotSolver]:
        """patchright + every lane's AndroidSolver, deduped by identity.

        Lanes are distinct instances, but dedupe by ``id`` defensively so a lane dict
        that ever aliases the same instance (or aliases patchright) is warmed/closed
        exactly once.
        """
        backends: list[AntiBotSolver] = []
        seen_ids: set[int] = set()
        for backend in (self._patchright, *self._android_by_lane.values()):
            if id(backend) not in seen_ids:
                seen_ids.add(id(backend))
                backends.append(backend)
        return backends

    @staticmethod
    async def _warm_backend(backend: AntiBotSolver) -> list[str]:
        """Warm ONE backend, isolating a total-warm crash as "no keys failed here".

        A backend ``warm()`` is itself per-key isolated (it returns the failed keys
        rather than raising), but guard the whole call so a backend that raises
        outright does not abort the other backend's warm. ``warm`` is off the
        ``AntiBotSolver`` Protocol, so a backend lacking it contributes nothing.
        """
        warm = getattr(backend, "warm", None)
        if warm is None:
            return []
        try:
            return list(await warm())
        except Exception as exc:  # noqa: BLE001 — isolate a backend warm crash
            _log.warning(
                "SolverRouter: backend %s warm raised %r — continuing",
                type(backend).__name__,
                exc,
                exc_info=True,
            )
            return []

    async def aclose(self) -> None:
        """Close patchright + every lane, guarding each so one failure does not skip the
        others (LANE-02: each lane's AndroidSolver owns its client + refresh loop)."""
        for backend in self._all_backends():
            aclose = getattr(backend, "aclose", None)
            if aclose is None:
                continue
            with contextlib.suppress(Exception):
                await aclose()
