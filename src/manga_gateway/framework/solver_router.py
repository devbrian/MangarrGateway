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

Beyond the clearance Protocol the router ALSO passes through the four off-Protocol
browser primitives (``fetch_via_browser`` / ``fetch_via_browser_paginated`` /
``fetch_via_browser_typed`` / ``fetch_via_browser_parallel_pages``, D-41) so that
swapping the bare :class:`CloudflareSolver` for this router as ``app.state.solver``
does not break a source's ``_solver_from_ctx`` ``hasattr`` gate (comix uses
``fetch_via_browser`` + ``fetch_via_browser_parallel_pages``; MangaFire keyword search
uses the typed one). All delegate to the patchright backend (the only engine with a real
browser; the android backend is a WebView-clearance sidecar with no browser and never
needs these), keeping criterion 4 "comix unaffected" true.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
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

    async def fetch_via_browser_typed(
        self,
        url: str,
        *,
        type_selector: str,
        type_text: str,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> Any: ...

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
    ``cast`` ``self._android`` (typed as the engine-agnostic ``AntiBotSolver``
    Protocol, which intentionally omits it) to a shape that declares it, and
    delegate mypy-strict-clean with NO ``# type: ignore``. The INVERSE of the four
    ``fetch_via_browser*`` passthroughs: those cast ``self._patchright`` (the only
    engine with a real browser); this one casts ``self._android`` (the only engine
    with the cleared WebView the eval runs inside).
    """

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — matches the backend's op-budget kwarg
    ) -> Any: ...


# The android engine name (matches ``Source.solver_engine`` on mangadot/kagane).
# Every other value (notably the ``"patchright"`` default) routes to the desktop
# backend — the router is permissive so an unmapped/unknown key falls through to the
# patchright backend, which already returns None for keys it does not own (BOT-01).
_ANDROID_ENGINE = "android"


class SolverRouter:
    """Composite AntiBotSolver dispatching ``get_clearance`` by per-source engine."""

    def __init__(
        self,
        *,
        patchright: AntiBotSolver,
        android: AntiBotSolver,
        engine_by_source: dict[str, str],
    ) -> None:
        self._patchright = patchright
        self._android = android
        self._engine_by_source = dict(engine_by_source)

    def _backend_for(self, source_key: str) -> AntiBotSolver:
        """Select the backend for ``source_key`` from the engine map.

        ``"android"`` → the AndroidSolver; everything else (the ``"patchright"``
        default AND any unmapped key) → the desktop CloudflareSolver. Both backends
        return ``None`` for a key they do not own, so an unmapped/non-cloudflare key
        resolves to ``None`` whichever backend it lands on (BOT-01).
        """
        engine = self._engine_by_source.get(source_key, "patchright")
        return self._android if engine == _ANDROID_ENGINE else self._patchright

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

    async def fetch_via_browser_typed(
        self,
        url: str,
        *,
        type_selector: str,
        type_text: str,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches CloudflareSolver's op-budget kwarg
    ) -> Any:
        """Delegate the real-keyboard browser-DOM read to the patchright backend (D-41).

        MangaFire keyword search's ``_solver_from_ctx`` ``hasattr`` gate runs against
        the production ``app.state.solver`` — which is THIS router — so the router must
        expose the typed primitive too. Always delegates to ``self._patchright`` (the
        android backend has no real browser).
        """
        backend = cast("_BrowserFetchSolver", self._patchright)
        return await backend.fetch_via_browser_typed(
            url,
            type_selector=type_selector,
            type_text=type_text,
            extract=extract,
            wait_for=wait_for,
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
        """Delegate the in-WebView JS eval to the ANDROID backend (EVAL-02).

        Unlike the four ``fetch_via_browser*`` passthroughs (which route to
        ``self._patchright``), this routes to ``self._android`` — the engine that
        owns the Turnstile-cleared redroid WebView the eval runs inside.
        """
        backend = cast("_EvalSolver", self._android)
        return await backend.eval_in_webview(
            challenge_url, js, wait_for=wait_for, timeout=timeout
        )

    async def warm(self) -> list[str]:
        """Warm BOTH backends; return the UNION of their failed keys (deduped).

        Each backend's ``warm()`` returns the source keys whose eager solve failed
        (D-33). The lifespan force-disables exactly those, so the router returns the
        union across both engines (first-seen order, deduped). Both backends are
        warmed unconditionally — a patchright warm failure must NOT skip the android
        warm (and vice versa).
        """
        failed: list[str] = []
        seen: set[str] = set()
        for backend in (self._patchright, self._android):
            for key in await self._warm_backend(backend):
                if key not in seen:
                    seen.add(key)
                    failed.append(key)
        return failed

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
        """Close BOTH backends, guarding each so one failure does not skip the other."""
        for backend in (self._patchright, self._android):
            aclose = getattr(backend, "aclose", None)
            if aclose is None:
                continue
            with contextlib.suppress(Exception):
                await aclose()
