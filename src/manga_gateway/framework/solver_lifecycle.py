"""Bounded Patchright lifecycle (Plan 04-04 Task 1, RESEARCH Pattern 7, criterion #4).

The browser is the scarce, fragile, fingerprintable resource. ``BrowserLifecycle``
wraps a persistent context with the four invariants the phase grades on:

* **Solve-cap semaphore** — ``asyncio.Semaphore(solve_concurrency)`` bounds the
  number of simultaneous challenge solves (Pitfall 6: solve storms flag the IP).
* **Single-flight collapse** — N concurrent callers that all need a (non-forced)
  solve await ONE shared in-flight task rather than each launching its own browser
  solve (each solve is a fingerprinting event; minimize them, Pitfall 6). A
  ``force``ed solve (D-35 re-solve) always runs a fresh solve.
* **Recycle watchdog** — a strong-ref'd background task periodically closes and
  relaunches the persistent context to shed memory/zombie state (Pattern 7).
* **Cleanup-on-all-paths** — ``aclose()`` closes the context and cancels the
  watchdog even when invoked while a ``CancelledError`` is propagating (lifespan
  teardown / drain-timeout cancellation), so no orphan Chromium survives
  shutdown (Pitfall 4).

The browser is injected as two async callables (``launch`` → a context, ``solve``
→ a :class:`Clearance`) so the gate can drive a MOCKED browser with NO real
Chromium (D-42). The real Patchright launch/solve closures are built by
``CloudflareSolver`` in ``antibot.py``; the lifecycle is launch-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .antibot import Clearance


class _LaunchFn(Protocol):
    def __call__(self) -> Awaitable[Any]: ...


class _SolveFn(Protocol):
    def __call__(self, context: Any, key: str) -> Awaitable[Clearance]: ...


class BrowserLifecycle:
    """Bounded persistent-context lifecycle (semaphore + single-flight + recycle).

    Args:
        launch: async ``() -> context`` — launches the persistent context.
        solve: async ``(context, key) -> Clearance`` — drives the challenge solve
            for ``key`` and captures the cf_clearance cookie + UA from that same
            session. The ``key`` (a ``source_key``) selects the per-domain
            challenge URL so each cloudflare-gated source captures only its own
            host-scoped cookies (#88).
        solve_concurrency: max simultaneous solves (the solve-cap semaphore).
        recycle_seconds: cadence for the recycle watchdog; ``None`` disables it.
    """

    def __init__(
        self,
        *,
        launch: Callable[[], Awaitable[Any]],
        solve: Callable[[Any, str], Awaitable[Clearance]],
        solve_concurrency: int = 1,
        recycle_seconds: float | None = None,
    ) -> None:
        self._launch = launch
        self._solve = solve
        self._sem = asyncio.Semaphore(solve_concurrency)
        self._recycle_seconds = recycle_seconds
        self._context: Any = None
        self._context_lock = asyncio.Lock()  # guards launch/recycle of the context
        # Per-key single-flight + held clearance (#88): N cloudflare-gated sources
        # each get their OWN in-flight future and held Clearance, keyed by
        # ``source_key``. With exactly one cf key (comix today) these dicts
        # collapse to the historic single-value behavior.
        self._inflight: dict[str, asyncio.Future[Clearance]] = {}  # single-flight/key
        self._held: dict[str, Clearance] = {}  # D-35: reuse last-good until challenged
        self._recycle_task: asyncio.Task[None] | None = None
        self._closed = False

    # ─────────────────────────── context lifecycle ───────────────────────────

    async def _ensure_context(self) -> Any:
        """Return the live persistent context, launching it once on first use."""
        return await self.get_context()

    async def get_context(self) -> Any:
        """Return the live persistent context, launching it once on first use.

        Public accessor used by callers that need to drive the browser directly
        (e.g. ``CloudflareSolver.fetch_via_browser``). Internal callers may keep
        using the underscore alias.
        """
        if self._context is None:
            async with self._context_lock:
                if self._context is None:  # double-checked under the lock
                    self._context = await self._launch()
        return self._context

    async def _close_context(self) -> None:
        """Close the persistent context if one is live (idempotent, error-tolerant).

        Also clears EVERY held :class:`Clearance` — every cached clearance's
        cookies + UA are bound to the torn-down shared browser session, so the
        next caller of any key must re-solve against the freshly launched
        context (D-35 / #88). Clears the dict in place (NOT ``= None``): a
        half-cleared ``_held`` would hand a torn-down session's cookies to the
        next caller (RESEARCH Pitfall 3).
        """
        ctx = self._context
        self._context = None
        self._held.clear()
        if ctx is not None:
            with contextlib.suppress(Exception):
                await ctx.close()

    async def recycle_now(self) -> None:
        """Close the persistent context on-demand (crash-driven recycle, #54).

        Distinct from the time-based ``_recycle_loop`` watchdog: callers that have
        detected the underlying Playwright/Camoufox Node driver has died (e.g.
        ``fetch_via_browser`` after catching "Connection closed while reading from
        the driver" — Playwright 1.60.0 Firefox handler crash on undefined
        ``pageError.location.url``) invoke this to force a fresh launch on the
        next ``get_context()`` call. Without it, ``get_context()`` would happily
        return the cached-but-dead handle because ``self._context is not None``.

        Idempotent and error-tolerant (``_close_context`` already suppresses
        teardown exceptions on a dead handle). Safe to call concurrently with
        the time-based watchdog — both serialize through ``_context_lock``.
        """
        async with self._context_lock:
            await self._close_context()

    # ─────────────────────────── solve ───────────────────────────

    async def solve(self, key: str, *, force: bool = False) -> Clearance:
        """Return a :class:`Clearance` for ``key``, collapsing concurrent callers.

        Per-key single-flight (#88): N cloudflare-gated sources each get their
        OWN held clearance + in-flight future, keyed by ``source_key``.
        Concurrent callers of the SAME key collapse to ONE underlying solve;
        distinct keys solve independently.

        Non-``force`` callers reuse the last-good :class:`Clearance` for ``key``
        if one is held (D-35: hold until a request returns a CF challenge or the
        cookie is rejected; no proactive TTL). If no clearance is held for
        ``key``, non-``force`` callers that arrive while a solve for that key is
        already in flight await that SAME solve (Pitfall 6). A ``force``ed solve
        (the D-35 re-solve) always runs its own solve under the (shared)
        solve-cap semaphore and replaces the held clearance for ``key`` on
        success.
        """
        if not force:
            # Fast path: reuse the last-good clearance for this key until a
            # caller forces a re-solve or _close_context() (recycle/aclose)
            # invalidates the whole dict.
            held = self._held.get(key)
            if held is not None:
                return held
            # Snapshot the inflight future BEFORE awaiting so the leader's
            # ``finally`` cannot pop it between the .done() check and the await
            # (single-flight race), scoped per-key.
            inflight = self._inflight.get(key)
            if inflight is not None and not inflight.done():
                return await inflight

        if force:
            clearance = await self._run_solve(key)
            self._held[key] = clearance  # D-35 re-solve replaces the cached value
            return clearance

        # Become the single-flight leader for this key: publish a Future others
        # awaiting the same key can collapse onto.
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._inflight[key] = future
        try:
            clearance = await self._run_solve(key)
        except BaseException as exc:  # propagate to every awaiter, then re-raise
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            self._held[key] = clearance  # cache for subsequent non-forced callers
            if not future.done():
                future.set_result(clearance)
            return clearance
        finally:
            self._inflight.pop(key, None)

    async def _run_solve(self, key: str) -> Clearance:
        """Acquire the solve-cap semaphore, ensure a context, and solve ``key``."""
        async with self._sem:
            ctx = await self._ensure_context()
            return await self._solve(ctx, key)

    # ─────────────────────────── recycle watchdog ───────────────────────────

    def start_recycle_watchdog(self) -> None:
        """Launch the recycle watchdog as a strong-ref'd background task (Pattern 7).

        Mirrors the ``jobs/manager.py`` strong-ref idiom so the fire-and-forget task
        is never GC'd mid-loop. No-op if no ``recycle_seconds`` was configured.
        """
        if self._recycle_seconds is None or self._recycle_task is not None:
            return
        self._recycle_task = asyncio.create_task(self._recycle_loop())

    async def _recycle_loop(self) -> None:
        """Periodically recycle the persistent context to shed memory/zombie state."""
        assert self._recycle_seconds is not None
        try:
            while not self._closed:
                await asyncio.sleep(self._recycle_seconds)
                # Recycle under the context lock so an in-flight solve's
                # _ensure_context relaunches a fresh context rather than racing.
                async with self._context_lock:
                    await self._close_context()
        except asyncio.CancelledError:
            raise

    # ─────────────────────────── cleanup ───────────────────────────

    async def aclose(self) -> None:
        """Tear the browser down on ALL paths — incl. under ``CancelledError``.

        Cancels the recycle watchdog and closes the persistent context so no orphan
        Chromium survives shutdown (Pitfall 4). Shielded against an in-flight
        ``CancelledError`` (drain-timeout cancellation) so cleanup always completes.
        """
        self._closed = True
        task = self._recycle_task
        self._recycle_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        # Close even if we were entered under a propagating CancelledError.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._close_context())
