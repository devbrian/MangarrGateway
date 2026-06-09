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
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .antibot import AntiBotSolver, Clearance

_log = logging.getLogger("manga_gateway")

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
        self, source_key: str, *, force_resolve: bool = False
    ) -> Clearance | None:
        """Dispatch to the mapped backend's ``get_clearance`` (D-41 pass-through).

        ``force_resolve`` (the D-35 403 self-heal) is forwarded ONLY when the chosen
        backend declares it as a keyword — mirroring context.py's ``_call_solver``
        ``inspect.signature`` detection so the public ``AntiBotSolver`` seam stays
        unchurned (D-41).
        """
        backend = self._backend_for(source_key)
        get = backend.get_clearance
        if force_resolve and "force_resolve" in inspect.signature(get).parameters:
            return await get(source_key, force_resolve=True)  # type: ignore[call-arg]
        return await get(source_key)

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
