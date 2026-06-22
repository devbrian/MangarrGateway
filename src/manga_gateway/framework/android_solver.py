"""Gateway-side Android-WebView Cloudflare solver (BOT-01/BOT-02, Phase 10).

``AndroidSolver`` implements the existing
:class:`~manga_gateway.framework.antibot.AntiBotSolver`
Protocol by httpx-calling the ``android-solver`` sidecar over the docker-internal
network — it mints a ``cf_clearance`` from a REAL Android WebView (a redroid sidecar)
for the strict Cloudflare Turnstile on ``mangadot.net`` / ``kagane.to`` that desktop
browser automation cannot clear from Linux (root cause + proof: resolved debug session
``mangadot-cf-linux-fingerprint``). R1 is preserved: the gateway process holds NO
Android machinery — it reaches the sidecar over HTTP only (the sidecar drives
redroid via adb + CDP; Plans 10-01/10-02). This module imports NOTHING from the
``android_solver`` sidecar package.

It returns the SAME :class:`Clearance` shape the existing httpx leg already injects
per request (D-40: ``cf_clearance`` cookie + the EXACT WebView UA the cookie is bound
to — a mismatched UA silently invalidates the cookie). The per-source engine selection
(comix → Patchright, mangadot/kagane → here) lives in
:class:`~manga_gateway.framework.solver_router.SolverRouter`, not in this class.

D-35 re-solve hold: the last-good clearance is held per source key; a non-force call
reuses it, a ``force_resolve=True`` call (the 403 self-heal, driven by context.py's
``inspect.signature`` pass-through, D-41) discards the held value and runs ONE fresh
sidecar solve. ``warm()`` mirrors :meth:`CloudflareSolver.warm`'s return contract
(the FAILED keys) so the lifespan disables only failures; an unconfigured sidecar URL
reports ALL android keys failed so the gate / CI / a local box without redroid stays
green. The ``cf_clearance`` value is NEVER logged (T-10-04).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from .antibot import Clearance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from pydantic import SecretStr

_log = logging.getLogger("manga_gateway")

# Proactive-refresh tuning (D-35 follow-up: keep the ~11s solve off the request hot
# path). Re-mint a held clearance this many seconds BEFORE its real cookie expiry; the
# background loop then sleeps until the soonest upcoming (expiry − lead), clamped to
# [min, max] so a far-off expiry still re-checks periodically and a near one never
# busy-loops. Expiry math is wall-clock (cookie ``expires`` is epoch seconds).
_REFRESH_LEAD_S = 120.0
_REFRESH_MIN_SLEEP_S = 30.0
_REFRESH_MAX_SLEEP_S = 600.0

# Bug 4 Fix B: the bounded-acquire budget for the single gateway-side device lock.
# Sized to EXCEED a normal solve (~30s) plus a normal eval (~10s) so a legitimate
# queue of coordinated comix evals + a refresh solve never trips the contention
# guard; a wedged device past this surfaces a clean per-source RuntimeError instead
# of an unbounded hang. Instance attr (so tests can shrink it).
_DEVICE_ACQUIRE_TIMEOUT_S = 90.0


class _RefreshDeferred(Exception):  # noqa: N818 — control-flow sentinel, not an error
    """Bug 5 Fix (a): a background (proactive-refresh) solve bailed under the device
    lock because a FOREGROUND lease (a comix solve+eval sequence inside
    :meth:`AndroidSolver.device_session`) was claimed while the solve queued on the
    lock. Raised by :meth:`AndroidSolver._solve` (so no solve-error metric is emitted)
    and caught by :meth:`AndroidSolver._refresh_tick`, which skips the solve and
    reschedules a short re-check. It is NOT a solve failure — the held clearance keeps
    serving and the reactive D-35 403 self-heal backstops a clearance that lapses while
    the device stays continuously busy. Never raised on the foreground/get_clearance
    path (those pass ``defer_if_foreground=False``)."""


def _emit_solve(
    key: str,
    *,
    outcome: str,
    duration_ms: float,
    attempt: int,
    error: str | None,
) -> None:
    """No-op-safe, failure-isolated ``emit_solve`` for the android sidecar solve.

    The browser path emits its ``kind="solve"`` event from ``solver_lifecycle``; the
    android path emitted NOTHING, so an in-band ~11s sidecar solve was invisible in the
    per-request breakdown (it surfaced only as an unexplained gap between two http
    events). This mirrors ``solver_lifecycle._emit_solve`` so the android solve shows as
    a labeled ``solve`` row. A ``None`` collector is a no-op; a collector error never
    breaks the solve. The ``cf_clearance`` value is NEVER passed here (T-10-04)."""
    from ..metrics.collector import get_collector

    collector = get_collector()
    if collector is None:
        return
    try:
        collector.emit_solve(
            source_key=key,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
            error=error,
        )
    except Exception:  # noqa: BLE001 — a metric failure must never break a solve
        pass


def _parse_expiry(raw: object) -> float | None:
    """Coerce the sidecar's ``cf_clearance_expires`` → a positive epoch float | None.

    A missing/null/non-numeric/non-positive value means "no known lifetime" → ``None``
    (that key is then refreshed reactively only). Booleans are rejected (a stray
    ``True`` is not an epoch). Mirrors the sidecar's own ``_cookie_expires`` guard so
    an older sidecar that omits the field degrades cleanly to reactive-only.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) if raw > 0 else None


class AndroidSolver:
    """Mint a per-source clearance via the android-solver sidecar (AntiBotSolver).

    Constructed with the sidecar base URL (``None`` ⇒ unconfigured: every solve
    raises so ``warm()`` boots all android keys disabled), the sidecar API key (a
    ``SecretStr`` so its repr/logs auto-redact — SEC-01/T-10-04), a per-call request
    timeout, and the android-engine source keys mapped to their
    ``cloudflare_challenge_url``. A key NOT in that map resolves to ``None`` on every
    call (BOT-01). The httpx client is owned (lazily built) unless one is injected
    (tests); ``aclose`` closes only an owned client.
    """

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: SecretStr | None,
        challenge_urls: dict[str, str],
        # Client timeout: must exceed the sidecar's per-solve cap (default 120s) +
        # margin so the gateway never abandons + re-fires a solve mid-flight (the app
        # wires this from settings.android_solver_timeout_s, default 180).
        timeout_s: float = 180.0,
        # PROXY-01 / Req 7: the ``build_proxy`` Playwright dict
        # (``{server, username?, password?}``) — the SAME value already feeding
        # the CloudflareSolver. ``None`` ⇒ no proxy in the /solve body (D-08, the
        # gate/CI/local-no-redroid path stays byte-for-byte unchanged). The dict
        # arrives already built by ``build_proxy`` (the sole SecretStr unpacker,
        # T-odg-01) — it is threaded through verbatim and NEVER logged (T-11-02).
        proxy: dict[str, str] | None = None,
        # On-demand (challenge-triggered) keys — sources with
        # ``cloudflare_challenge_optional=True``. ``warm()`` SKIPS these: their CF
        # challenge is intermittent, so an eager startup solve would loop on an absent
        # challenge, waste the single redroid device, and (failing) force-disable the
        # source for the 12h /caps window (debug pooltimeout-recurrence). They still
        # solve on-demand via ``get_clearance(force_resolve=True)`` when a request
        # actually hits a challenge.
        on_demand_keys: Iterable[str] = (),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Strip a trailing slash so ``f"{base}/solve"`` never doubles it.
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._challenge_urls = dict(challenge_urls)
        self._on_demand_keys = frozenset(on_demand_keys)
        self._timeout_s = timeout_s
        self._proxy = proxy
        self._client = client
        self._owns_client = client is None
        # Last-good clearance per source key (the D-35 hold — mirrors the browser
        # solver's persistent-context clearance reuse).
        self._held: dict[str, Clearance] = {}
        # Per-source-key single-flight registry (#296): the in-flight ``_solve`` task
        # for a key, so N concurrent callers for that key share ONE device hit instead
        # of each firing their own against the single serialized redroid (which would
        # cost ~N×~11s for one mint). Keyed by ``source_key`` with no global lock, so
        # distinct keys get distinct tasks and one key's herd never blocks another.
        self._inflight: dict[str, asyncio.Task[tuple[Clearance, float | None]]] = {}
        # Per-source wall-clock epoch expiry of the held clearance (only for keys whose
        # sidecar response carried a real cookie lifetime). Drives the proactive
        # refresh; a key absent here is refreshed only reactively (D-35 403 self-heal).
        self._expires_at: dict[str, float] = {}
        # The background proactive-refresh task (started by :meth:`start`, cancelled by
        # :meth:`aclose`). Tunables are instance attrs so tests can shrink them.
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_lead_s = _REFRESH_LEAD_S
        self._refresh_min_sleep_s = _REFRESH_MIN_SLEEP_S
        self._refresh_max_sleep_s = _REFRESH_MAX_SLEEP_S
        # Bug 4 Fix B: the SINGLE gateway-side serializer for the one redroid — every
        # sidecar device op (_solve POST + eval POST) holds it per-op so the comix
        # fan-out + the refresh loop QUEUE on the device instead of storming the
        # sidecar with `503 solver busy`. Bounded-acquire via _device_acquire_timeout_s.
        self._device_lock = asyncio.Lock()
        self._device_acquire_timeout_s = _DEVICE_ACQUIRE_TIMEOUT_S
        # Bug 4 Fix C: count of in-flight FOREGROUND android sequences (raised by
        # device_session(), read by _refresh_tick). A non-zero count defers the
        # proactive-refresh loop so it never navigates the shared WebView away while a
        # comix solve+eval sequence holds the device (the page stays warm). A counter
        # (nesting-safe) holding NO lock — the gathered evals inside still serialize
        # per-op on _device_lock.
        self._foreground_inflight = 0

    async def get_clearance(
        self,
        source_key: str,
        *,
        force_resolve: bool = False,
        solve_if_missing: bool = True,
    ) -> Clearance | None:
        """Return clearance for ``source_key`` (``None`` for non-android keys).

        A key absent from the android challenge-url map → ``None`` (BOT-01 — the
        gateway never touches the sidecar for it). Otherwise a non-force call reuses
        the held clearance when present; ``force_resolve=True`` discards it and runs a
        FRESH sidecar solve (D-35, the 403 self-heal). ``force_resolve`` is a declared
        keyword so context.py's ``_call_solver`` ``inspect.signature`` detection passes
        it through (D-41).

        ``solve_if_missing=False`` (the on-demand peek, debug pooltimeout-recurrence):
        serve the held clearance if present but return ``None`` instead of running a
        BLOCKING sidecar solve when none is held — the caller (a
        ``cloudflare_challenge_optional`` source) will let the request go out without
        clearance and only force a real solve if the response is an actual challenge.
        This is what stops an intermittent-challenge source from hanging on the sidecar
        for a clearance the site is not currently demanding.
        """
        if source_key not in self._challenge_urls:
            return None  # MangaDex et al. — no android clearance needed
        if force_resolve:
            # WR-05: discard the held entry BEFORE the fresh solve so a FAILED
            # force-resolve (the common 403 self-heal case) cannot leave the known
            # -bad token held — the next non-force call must re-solve rather than
            # silently re-serve the stale clearance that produced the 403.
            self._held.pop(source_key, None)
            self._expires_at.pop(source_key, None)
        else:
            held = self._held.get(source_key)
            if held is not None:
                return held
            if not solve_if_missing:
                # On-demand peek with nothing held — do NOT block on a sidecar solve.
                return None
        clearance, expires_at = await self._coalesced_solve(source_key)
        self._held[source_key] = clearance
        self._record_expiry(source_key, expires_at)
        return clearance

    async def _coalesced_solve(
        self, source_key: str, *, defer_if_foreground: bool = False
    ) -> tuple[Clearance, float | None]:
        """Single-flight the sidecar ``_solve`` per source key (#296).

        ``defer_if_foreground`` (Bug 5 Fix (a)) is threaded into the shared ``_solve``
        task so a proactive-refresh caller (the sole ``True`` caller, via
        :meth:`_refresh_tick`) bails under the device lock when comix holds the
        foreground lease. It is baked into the task at CREATION, so the coalescing
        contract is unchanged when no foreground lease is held (the common case, and
        every offline test): the refresh and a concurrent same-key reactive
        ``force_resolve`` still share ONE /solve.

        Concurrent ``force_resolve`` / reactive / proactive-refresh callers for the
        SAME key share ONE in-flight ``_solve`` task against the one redroid device
        rather than each firing their own — the sidecar serializes them, so an
        un-coalesced same-key herd would pay ~N×~11s for a single mint. Distinct keys
        get distinct tasks (the registry is keyed by ``source_key`` with no global
        lock), so one source's herd never head-of-line-blocks another (#296 isolation).

        The shared task is awaited under :func:`asyncio.shield` so a single abandoned
        / cancelled awaiter's cancellation cannot tear down the solve for the whole
        herd — the shared task runs to completion in the background and its result (or
        exception) fans out to every awaiter. A FAILED solve therefore propagates the
        same exception to all awaiters and leaves the per-caller post-solve swap
        un-run, so no clearance is held for that key (WR-05).
        """
        task = self._inflight.get(source_key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._solve(source_key, defer_if_foreground=defer_if_foreground)
            )
            self._inflight[source_key] = task

            def _pop(
                completed: asyncio.Task[tuple[Clearance, float | None]],
            ) -> None:
                # Clear the slot only when it still holds THIS task, so a later
                # solve's task is never clobbered by an earlier one's callback.
                if self._inflight.get(source_key) is completed:
                    del self._inflight[source_key]

            task.add_done_callback(_pop)
        return await asyncio.shield(task)

    def _record_expiry(self, source_key: str, expires_at: float | None) -> None:
        """Track (or clear) the held clearance's epoch expiry for the refresh loop."""
        if expires_at is not None:
            self._expires_at[source_key] = expires_at
        else:
            self._expires_at.pop(source_key, None)

    async def warm(self) -> list[str]:
        """Best-effort eager solve per android key; return the FAILED keys.

        Mirrors :meth:`CloudflareSolver.warm`'s return contract so the lifespan
        force-disables ONLY the sources whose eager solve failed (D-33, per-source
        isolation). When the sidecar URL is unconfigured every solve raises, so every
        android key is reported failed — they boot disabled and the gate / CI / a
        local box without redroid stays green. Each key's solve is isolated in its own
        try/except; the underlying exception is logged with ``exc_info`` (NEVER the
        clearance value).
        """
        failed: list[str] = []
        for key in self._challenge_urls:
            if key in self._on_demand_keys:
                # On-demand source: never eager-warm — it solves only when a live
                # challenge is detected (debug pooltimeout-recurrence).
                continue
            try:
                await self.get_clearance(key)
            except Exception as exc:  # noqa: BLE001 — isolate per-key warm failures
                _log.warning(
                    "AndroidSolver warm failed for source %r: %r "
                    "(boots disabled — D-33)",
                    key,
                    exc,
                    exc_info=True,
                )
                failed.append(key)
        return failed

    async def aclose(self) -> None:
        """Cancel the refresh loop and close the owned httpx client (idempotent)."""
        task = self._refresh_task
        if task is not None:
            self._refresh_task = None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._owns_client and self._client is not None:
            client = self._client
            self._client = None
            await client.aclose()

    # ─────────────────────────────── internals ───────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily build the owned httpx client (no new gateway dependency — httpx is
        already the shared transport library; this is a SEPARATE client to the
        sidecar, not the R1-shared image-fetch client)."""
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    @contextlib.asynccontextmanager
    async def _device_op(
        self, *, defer_if_foreground: bool = False
    ) -> AsyncIterator[bool]:
        """Serialize ONE sidecar device op behind the single gateway-side lock (Fix B).

        Bug 4 cause #2: the one redroid serves all android solves+evals, and the
        sidecar REJECTS concurrency with `503 solver busy` rather than queueing. The
        comix search fan-out (N candidate chapter-list evals gathered concurrently) +
        the proactive-refresh loop's solves would each hit that rejection. This
        bounded-acquire lock turns those concurrent calls into an orderly gateway-side
        QUEUE so the sidecar sees exactly one op at a time. The acquire is bounded by
        ``_device_acquire_timeout_s`` (sized to exceed a normal solve + eval) so a
        wedged device surfaces a clean per-source ``RuntimeError`` (comix maps it to a
        per-candidate ``SourceError``) instead of an unbounded hang. The lock is
        released ONLY if it was acquired — guarding the timeout-during-acquire race
        (an acquire that completes exactly as the deadline fires is released here, an
        acquire that never completed holds nothing).

        Yields ``True`` when the caller may proceed with its device op (the normal
        path). Bug 5 Fix (a): a background (proactive-refresh) op passes
        ``defer_if_foreground=True``; AFTER acquiring the lock it re-checks whether a
        FOREGROUND lease (``_foreground_inflight > 0`` — a comix sequence inside
        :meth:`device_session`) was claimed while this op queued on the lock. If so it
        RELEASES the lock and yields ``False`` (bail) so the caller skips its op rather
        than navigate the shared WebView away mid comix sequence. The re-check reads the
        lock-free ``_foreground_inflight`` counter while holding ONLY ``_device_lock``
        and takes no further lock, so it cannot deadlock with comix's per-eval
        ``_device_op`` (single lock, lock-free counter — no lock-ordering cycle). The
        foreground/get_clearance path (``defer_if_foreground=False``, the default) never
        bails — it always yields ``True`` exactly as before (Fix B unchanged).
        """
        got = False
        try:
            async with asyncio.timeout(self._device_acquire_timeout_s):
                await self._device_lock.acquire()
                got = True
        except TimeoutError as exc:
            if got:  # acquired-then-timed-out-on-context-exit: do not leak the lock
                self._device_lock.release()
            raise RuntimeError(
                "android device busy — could not acquire the single redroid within "
                f"{self._device_acquire_timeout_s:.0f}s (device contention)"
            ) from exc
        # Bug 5 Fix (a): post-acquire foreground re-check. A proactive-refresh solve
        # may have passed _refresh_tick's tick-top check while the device was FREE
        # (_foreground_inflight == 0) and then queued here behind comix's in-flight
        # device op; by the time it acquires the lock comix may hold the foreground
        # lease. Bail (release + yield False) so the queued refresh never clobbers
        # comix's warm page. Release BEFORE yielding so the bail path holds nothing.
        if defer_if_foreground and self._foreground_inflight > 0:
            self._device_lock.release()
            yield False
            return
        try:
            yield True
        finally:
            self._device_lock.release()

    @contextlib.asynccontextmanager
    async def device_session(self) -> AsyncIterator[None]:
        """Lease the redroid for a whole foreground solve+eval sequence (Fix C).

        A nesting-safe COUNTER (NOT a lock): increments ``_foreground_inflight`` on
        enter, decrements in ``finally`` (back to 0 even on exception). comix wraps its
        whole solve+eval sequence in this so the proactive-refresh loop DEFERS (see
        :meth:`_refresh_tick`) while the sequence holds the device — the background
        loop never navigates the shared WebView away mid-sequence, so the page stays
        warm on comix across the search/download evals. It holds NO lock: the gathered
        candidate evals inside it still each take ``_device_lock`` per op (via
        :meth:`_device_op`) and serialize; holding a lock ACROSS the gather would
        deadlock. Deferral is safe — the held clearance keeps serving and the reactive
        D-35 403 self-heal still backstops an expired clearance.
        """
        self._foreground_inflight += 1
        try:
            yield
        finally:
            self._foreground_inflight -= 1

    async def _solve(
        self, source_key: str, *, defer_if_foreground: bool = False
    ) -> tuple[Clearance, float | None]:
        """POST the sidecar ``/solve`` → ``(Clearance, epoch-expiry|None)`` (BOT-02).

        ``defer_if_foreground`` (Bug 5 Fix (a), set only by the proactive-refresh loop
        via :meth:`_refresh_tick`) makes the solve BAIL — raising
        :class:`_RefreshDeferred` without POSTing — when a foreground lease (comix) is
        claimed while it queues on the device lock. The foreground/get_clearance path
        leaves it ``False`` and is byte-for-byte unchanged.

        Sends ``X-Solver-Key`` (SEC-01) + ``{"challenge_url": <map[key]>}``; a
        non-200 raises (``raise_for_status``) so ``warm()`` reports the key failed and
        the lifespan disables it, exactly like a failed browser solve. The
        ``cf_clearance`` token is parsed into the SAME ``Clearance`` shape the httpx
        leg injects via D-40 — and is NEVER logged (T-10-04). The optional
        ``cf_clearance_expires`` (epoch seconds, or absent/null) rides back so the
        caller can refresh the clearance proactively before it lapses.

        Emits a ``kind="solve"`` metric event (success AND failure) so the sidecar
        solve's latency is visible in the per-request breakdown (the gap-mystery fix).
        """
        if self._base_url is None:
            raise RuntimeError(
                "android_solver_url is not configured — cannot solve "
                f"{source_key!r} (the android-solver sidecar is unwired)"
            )
        challenge_url = self._challenge_urls[source_key]
        headers: dict[str, str] = {}
        if self._api_key is not None:
            headers["X-Solver-Key"] = self._api_key.get_secret_value()
        # Req 7: thread the single static proxy into the /solve body so the
        # sidecar's CF-solve egress matches the gateway's httpx-fetch egress for
        # the same clearance. Gated like the CloudflareSolver: when unconfigured
        # the body carries ONLY ``challenge_url`` (D-08). The proxy dict is passed
        # through verbatim (already unpacked by build_proxy) and never logged.
        body: dict[str, object] = {"challenge_url": challenge_url}
        if self._proxy is not None:
            body["proxy"] = self._proxy
        start = time.perf_counter()
        try:
            # Fix B: serialize the sidecar /solve behind the single gateway-side
            # _device_lock so the refresh loop's solves + the comix fan-out's evals
            # QUEUE on the one redroid (no `503 solver busy` from our own calls).
            # Bug 5 Fix (a): a refresh solve passes defer_if_foreground=True — if a
            # comix foreground lease was claimed while it queued on the lock, _device_op
            # yields False and we BAIL (no POST) so the shared WebView is never
            # navigated away mid comix sequence.
            async with self._device_op(
                defer_if_foreground=defer_if_foreground
            ) as acquired:
                if not acquired:
                    raise _RefreshDeferred(source_key)
                resp = await self._ensure_client().post(
                    f"{self._base_url}/solve",
                    json=body,
                    headers=headers,
                    timeout=self._timeout_s,
                )
                resp.raise_for_status()
                payload = resp.json()
                token = payload["cf_clearance"]
                user_agent = payload["user_agent"]
        except _RefreshDeferred:
            # A deliberate skip, NOT a solve failure — propagate without an error
            # metric (the deferred refresh must not read as a solve error).
            raise
        except Exception as exc:
            _emit_solve(
                source_key,
                outcome="error",
                duration_ms=(time.perf_counter() - start) * 1000.0,
                attempt=1,
                error=type(exc).__name__,
            )
            raise
        _emit_solve(
            source_key,
            outcome="ok",
            duration_ms=(time.perf_counter() - start) * 1000.0,
            attempt=1,
            error=None,
        )
        expires_at = _parse_expiry(payload.get("cf_clearance_expires"))
        # Log the solve event only — never the token value (T-10-04).
        _log.info("AndroidSolver minted clearance for source %r", source_key)
        return (
            Clearance(cookies={"cf_clearance": token}, user_agent=user_agent),
            expires_at,
        )

    # ───────────────────────────── in-WebView eval ────────────────────────────

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        # ASYNC109 waived: matches the sidecar's per-eval op-budget — an explicit
        # per-call override of ``self._timeout_s`` (the chapter-list fan-out is ~8s
        # plus nav + SPA hydration, so the default MUST exceed the sidecar cap).
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        """POST the sidecar ``/eval`` → the marshalled JS result (EVAL-02).

        Runs gateway-authored ``js`` inside the warm Turnstile-cleared redroid
        WebView (the only fingerprint that clears comix.to) and returns the
        sidecar's ``{"value": <json>}`` payload's ``value``. This is an
        OFF-Protocol primitive (not part of ``AntiBotSolver.get_clearance``):
        comix calls it through :class:`SolverRouter` in Plan 03.

        Mirrors :meth:`_solve`'s request shape exactly — sends ``X-Solver-Key``
        (SEC-01, the ``SecretStr`` unpacked only at the POST call site) and a
        ``{"challenge_url", "js"}`` body (``wait_for`` added only when given) and
        ``raise_for_status()`` so a non-200 raises the same failure contract.
        ``base_url is None`` raises ``RuntimeError`` (D-33) so an unconfigured
        sidecar fails loud — comix's ``_solver_from_ctx`` surfaces it as a
        per-source ``SourceError`` (the gate / CI / a local box without redroid
        stays green) and NEVER silently no-ops to an empty result (T-14-07).

        Unlike ``_solve`` this is NOT held/cached and NOT single-flighted — each
        comix call site evals fresh (the clearance hold stays in
        ``get_clearance`` / ``_coalesced_solve``, untouched). The ``js`` and the
        eval result are NEVER logged (T-14-04).
        """
        if self._base_url is None:
            raise RuntimeError(
                "android_solver_url is not configured — cannot eval against "
                f"{challenge_url!r} (the android-solver sidecar is unwired)"
            )
        headers: dict[str, str] = {}
        if self._api_key is not None:
            headers["X-Solver-Key"] = self._api_key.get_secret_value()
        body: dict[str, object] = {"challenge_url": challenge_url, "js": js}
        if wait_for is not None:
            body["wait_for"] = wait_for
        # Req 7 parity with ``_solve``: thread the single static proxy into the
        # /eval body so the eval navigation egresses the SAME residential IP the
        # proxied clearance was minted on — a different egress IP makes the
        # WebView's cf_clearance invalid and CF re-challenges (it wedges on the
        # "Just a moment..." interstitial, the comix SPA never hydrates, and a
        # correctly-wrapped extractor still finds no env-*.js). Gated exactly like
        # ``_solve``: when unconfigured the body carries NO ``proxy`` key (D-08,
        # the no-proxy path stays byte-for-byte unchanged). Passed through verbatim
        # (already unpacked by build_proxy) and NEVER logged (T-14-04 / T-11-02).
        if self._proxy is not None:
            body["proxy"] = self._proxy
        # Fix B: serialize the sidecar /eval behind the single gateway-side
        # _device_lock — the comix candidate-chapter-list fan-out gathers N evals
        # concurrently, but each takes the lock per-op so they QUEUE on the one
        # redroid instead of N storming the sidecar into `503 solver busy`.
        async with self._device_op():
            resp = await self._ensure_client().post(
                f"{self._base_url}/eval",
                json=body,
                headers=headers,
                timeout=timeout if timeout is not None else self._timeout_s,
            )
            resp.raise_for_status()
            return resp.json()["value"]

    # ─────────────────────── proactive expiry-driven refresh ──────────────────

    def start(self) -> None:
        """Start the background proactive-refresh loop (idempotent, app lifespan).

        No-op when the sidecar is unconfigured or no android keys are mapped (gate /
        CI / a local box without redroid stays byte-for-byte unchanged — no task spins)
        or when already started. The loop is cancelled in :meth:`aclose`.
        """
        if (
            self._refresh_task is not None
            or self._base_url is None
            or not self._challenge_urls
        ):
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        """Re-mint expiring clearances ahead of their lapse until cancelled."""
        while True:
            try:
                delay = await self._refresh_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a tick failure must not kill the loop
                _log.warning("AndroidSolver refresh tick failed", exc_info=True)
                delay = self._refresh_max_sleep_s
            await asyncio.sleep(delay)

    async def _refresh_tick(self) -> float:
        """Re-mint any held clearance within the lead window; return the next sleep.

        Re-mints with "solve-then-swap" semantics (NOT ``force_resolve``): it does NOT
        discard the held entry first, so the still-valid clearance keeps serving during
        the ~11s solve and is swapped atomically only on success. A failed refresh keeps
        the old (about-to-expire) clearance — the reactive D-35 403 self-heal remains
        the backstop. On-demand keys are never proactively refreshed (they solve only on
        a live challenge). The returned delay is the soonest upcoming (expiry − lead),
        clamped to [min, max]; with no known expiries it is ``max`` (re-check later).

        Fix C: while a FOREGROUND android sequence holds the device
        (``_foreground_inflight > 0`` — a comix solve+eval sequence inside
        :meth:`device_session`) the tick DEFERS at the TICK TOP — it performs NO solve
        and returns a short re-check delay — so the background loop never navigates the
        shared WebView away mid comix sequence. The held clearance keeps serving during
        the deferral and the reactive D-35 403 self-heal still backstops a clearance
        that lapses while the device stays continuously busy.

        Bug 5 Fix (a): the tick-top check above is RACY — a refresh that passes it
        while the device is free (``_foreground_inflight == 0``) and then queues on the
        device lock can run AFTER comix claims the foreground lease, stealing the shared
        WebView mid comix sequence (the 30s-budget killer). To close that race the solve
        passes ``defer_if_foreground=True``; :meth:`_device_op` re-checks the counter
        UNDER the device lock and the solve raises :class:`_RefreshDeferred` (caught
        here) rather than POSTing. The deferred key is skipped and a SHORT re-check is
        scheduled so it retries once comix releases.
        """
        if self._foreground_inflight > 0:
            return self._refresh_min_sleep_s
        now = time.time()
        for key, expiry in list(self._expires_at.items()):
            if key in self._on_demand_keys:
                continue
            if expiry - now > self._refresh_lead_s:
                continue
            try:
                clearance, new_expiry = await self._coalesced_solve(
                    key, defer_if_foreground=True
                )
            except _RefreshDeferred:
                # Bug 5 Fix (a): a comix foreground lease was claimed while this refresh
                # queued on the device lock — abandon the solve so the shared WebView is
                # never navigated away mid comix sequence, and reschedule a SHORT
                # re-check (mirroring the tick-top defer) so the refresh retries once
                # comix releases. The held clearance keeps serving; the reactive D-35
                # 403 self-heal backstops a clearance that lapses meanwhile.
                return self._refresh_min_sleep_s
            except Exception:  # noqa: BLE001 — keep the held clearance; D-35 backstops
                _log.warning(
                    "AndroidSolver proactive refresh failed for source %r; keeping "
                    "held clearance (reactive 403 self-heal still covers)",
                    key,
                    exc_info=True,
                )
                continue
            self._held[key] = clearance
            self._record_expiry(key, new_expiry)
        upcoming = [
            expiry - self._refresh_lead_s
            for key, expiry in self._expires_at.items()
            if key not in self._on_demand_keys
        ]
        if not upcoming:
            return self._refresh_max_sleep_s
        delay = min(upcoming) - time.time()
        return max(self._refresh_min_sleep_s, min(self._refresh_max_sleep_s, delay))
