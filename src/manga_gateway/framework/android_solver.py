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
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from .antibot import Clearance

if TYPE_CHECKING:
    from collections.abc import Iterable

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

    async def _coalesced_solve(self, source_key: str) -> tuple[Clearance, float | None]:
        """Single-flight the sidecar ``_solve`` per source key (#296).

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
            task = asyncio.create_task(self._solve(source_key))
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

    async def _solve(self, source_key: str) -> tuple[Clearance, float | None]:
        """POST the sidecar ``/solve`` → ``(Clearance, epoch-expiry|None)`` (BOT-02).

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
        """
        now = time.time()
        for key, expiry in list(self._expires_at.items()):
            if key in self._on_demand_keys:
                continue
            if expiry - now > self._refresh_lead_s:
                continue
            try:
                clearance, new_expiry = await self._coalesced_solve(key)
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
