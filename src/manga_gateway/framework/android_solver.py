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
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from .antibot import Clearance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterable

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

# The single redroid sidecar is SHARED (multiple gateways / a hammered dev box / the
# per-test warm-prime fleet) and rejects a CONCURRENT op with ``503 solver busy`` via
# its NON-blocking lock — a deliberate backpressure signal, NOT a failure. The gateway's
# ``_device_op`` only serializes ITS OWN calls, so a 503 from another caller's in-flight
# op must be RETRIED (bounded), not surfaced as a hard ``upstream 503``. Sized so a
# normal in-flight op (~1–2s eval / ~10s solve) drains within the budget; past it the
# last 503 is returned for the caller's ``raise_for_status`` (a genuinely wedged device
# still fails loud). Instance attrs so tests can shrink them. ``504`` (timeout/failed)
# and 4xx are NEVER retried — only the ``503`` busy-backpressure is.
_SIDECAR_BUSY_RETRY_ATTEMPTS = 8
_SIDECAR_BUSY_RETRY_BACKOFF_S = 0.75
_SIDECAR_BUSY_STATUS = 503

# Mode-E follow-up (debug ``comix-warm-hydration-wait``): a SINGLE bounded retry of an
# ``eval_in_webview`` that THREW a TRANSIENT comix-origin 5xx. comix's in-page
# env-module axios call (``c.list`` / ``chapters``) occasionally hits comix's own flaky
# origin and gets a Cloudflare 521/520/522 (or a 5xx) — the sidecar now surfaces that as
# a ``504`` whose body is ``{"error": "eval threw", "detail": "...status code 5xx..."}``
# (vs. a generic ``eval failed`` / ``eval timed out``). Without a retry that transient
# origin blip returns 0 releases within the SINGLE search (it still failed single-shot
# callers like ``external_links[comix]`` even with the negative-cache self-heal, which
# only helps the NEXT search). One quick retry is ONE extra device eval, only on the
# rare origin 5xx, and fits the per-source 30s search budget (a 5xx fails FAST — the
# axios rejects on the edge response, not a hang to the timeout). It is NOT retried
# on: a CLEAN empty result (a normal ``200`` ``[]`` — a genuine no-match the
# negative-cache handles), a 4xx, a non-origin (CF/clearance/hydration) throw, or a
# timeout/cancel (a distinct ``504`` body). Instance attrs so tests can shrink them.
_EVAL_ORIGIN_5XX_RETRY_ATTEMPTS = 1
_EVAL_ORIGIN_5XX_RETRY_BACKOFF_S = 0.5
_SIDECAR_EVAL_THREW_STATUS = 504
# The sidecar EvalError detail for an origin failure is the env-module axios message,
# e.g. ``in-page eval threw: AxiosError: Request failed with status code 521`` — match
# the 3-digit HTTP status it reports and retry ONLY when it is in the 5xx band.
_EVAL_THROW_STATUS_RE = re.compile(r"status code (\d{3})")


def _is_origin_5xx_eval_throw(response: httpx.Response) -> bool:
    """True iff a sidecar ``/eval`` response is a TRANSIENT origin-5xx in-page throw.

    The sidecar returns ``504 {"error": "eval threw", "detail": <summary>}`` when the
    in-page promise rejected (Mode-E). The ``detail`` is the bounded, token-free first
    line of the JS error (built by the sidecar's ``_raise_if_eval_threw``); for a
    comix-origin failure it carries the axios ``"Request failed with status code NNN"``.
    Returns ``True`` ONLY when that ``NNN`` is a 5xx (a transient origin blip worth one
    retry). Any other 504 (a 4xx throw, a non-origin/CF/hydration throw with no status
    code, ``eval failed``, or ``eval timed out``) and any non-504 returns ``False`` so
    the caller re-raises immediately. A malformed / unreadable body is also ``False``
    (never retry on an ambiguous signal). The ``detail`` is matched, never logged."""
    if response.status_code != _SIDECAR_EVAL_THREW_STATUS:
        return False
    try:
        body = response.json()
    except (ValueError, TypeError):
        return False
    if not isinstance(body, dict) or body.get("error") != "eval threw":
        return False
    detail = body.get("detail")
    if not isinstance(detail, str):
        return False
    match = _EVAL_THROW_STATUS_RE.search(detail)
    if match is None:
        return False
    return 500 <= int(match.group(1)) <= 599


# Bug 5 follow-on #3 (Option A2): the NO-OP JS expression used to EVAL-PRIME a
# page-holding source (comix) during ``warm()`` via the EVAL path. The sidecar's
# ``_drive_eval`` warm-navigates → clears-if-challenged (the interactive Turnstile tap,
# NO ``pm clear``) → waits for hydration BEFORE running this JS, so evaluating a trivial
# resolved Promise leaves the shared WebView parked on a CLEARED + hydrated comix page
# WITHOUT running an extractor or minting a downloadHandle. It is an EXPRESSION (the
# sidecar runs ``Runtime.evaluate {expression, awaitPromise:true}`` — comix's real
# extractors are async IIFEs of the same shape), so an async-arrow IIFE that resolves to
# ``true`` is the minimal valid prime. The ``js`` is never logged (T-14-04).
_WEBVIEW_PRIME_JS = "(async () => true)()"


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


def _emit_eval(
    key: str | None,
    *,
    outcome: str,
    duration_ms: float,
    error: str | None,
) -> None:
    """No-op-safe, failure-isolated ``emit_eval`` for the android-WebView ``/eval``.

    comix's real upstream work (search / recent / chapter-list / chapter-pages) all
    rides :meth:`AndroidSolver.eval_in_webview`, which emitted NOTHING — so Search
    showed only 0ms CACHE rows and Recent showed nothing. This mirrors
    :func:`_emit_solve` so each outer ``eval`` shows as a labeled, REAL-timed row. A
    ``None`` collector is a clean no-op; a collector error never breaks the eval. The
    eval ``js``, the eval result, and any ``cf_clearance`` token are NEVER passed here
    (T-14-04) — redaction is structural (``emit_eval`` records ``url=None``)."""
    from ..metrics.collector import get_collector

    collector = get_collector()
    if collector is None:
        return
    try:
        collector.emit_eval(
            source_key=key,
            outcome=outcome,
            duration_ms=duration_ms,
            error=error,
        )
    except Exception:  # noqa: BLE001 — a metric failure must never break an eval
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
        # Bug 5 perf (warm-ordering): the page-HOLDING android keys (comix) — sources
        # that run their whole sequence as in-page evals and keep the shared redroid
        # WebView parked on their cleared page. ``warm()`` eager-solves these LAST so
        # the device ends startup on the holder's page and the lighter clearance-only
        # keys are already cleared before the holder takes the page (their proactive
        # refresh then has nothing due that would navigate the WebView away mid-search).
        # Empty (default) ⇒ challenge_urls order is preserved exactly (unchanged).
        warm_last_keys: Iterable[str] = (),
        # Bug 5 Lever A: cap the proactive-refresh interval at ``minted_at + this`` for
        # EVERY held android clearance whenever that is sooner than the (meaningless,
        # ~1yr) cookie expiry — so comix is re-minted ahead of CF's real re-challenge
        # cadence and a search never pays a cold clear. ``None`` / non-positive ⇒
        # DISABLED (cookie-expiry-only, byte-for-byte unchanged). The VALUE is tuned
        # from a live measurement window (see ``Settings.android_refresh_max_age_s``).
        refresh_max_age_s: float | None = None,
        # Bug 5 Fix #2 (startup-warm gate): the bounded wait an android FOREGROUND
        # sequence (a comix solve+eval inside :meth:`device_session`) spends at the
        # device door for the startup :meth:`warm` to finish BEFORE it claims the
        # device, so the first post-boot search finds every android source cleared and
        # the shared WebView parked on the page-holder (no startup-warm steal). The wait
        # falls THROUGH on timeout (warm slow/hung) rather than hanging the search; it
        # is a no-op until :meth:`arm_warm_gate` is called (the app lifespan), so a
        # solver that never warms (tests / unconfigured sidecar / CI) never waits.
        warm_gate_timeout_s: float = 25.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Strip a trailing slash so ``f"{base}/solve"`` never doubles it.
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._challenge_urls = dict(challenge_urls)
        self._on_demand_keys = frozenset(on_demand_keys)
        # Bug 5 perf (warm-ordering): page-holding keys eager-warmed LAST (ctor doc).
        self._warm_last_keys = frozenset(warm_last_keys)
        # Bug 5 Lever A: effective proactive-refresh age cap (None ⇒ disabled). A
        # non-positive value is normalised to None so "0 / -1 disables" is unambiguous.
        self._refresh_max_age_s = (
            refresh_max_age_s
            if refresh_max_age_s is not None and refresh_max_age_s > 0
            else None
        )
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
        # Bounded retry of the sidecar's ``503 solver busy`` backpressure (shared dev).
        self._busy_retry_attempts = _SIDECAR_BUSY_RETRY_ATTEMPTS
        self._busy_retry_backoff_s = _SIDECAR_BUSY_RETRY_BACKOFF_S
        # Mode-E follow-up: a SINGLE bounded retry of an ``eval_in_webview`` that threw
        # a TRANSIENT comix-origin 5xx (see ``_is_origin_5xx_eval_throw``) so a one-off
        # origin blip self-recovers WITHIN the single search instead of returning 0.
        self._eval_origin_5xx_retry_attempts = _EVAL_ORIGIN_5XX_RETRY_ATTEMPTS
        self._eval_origin_5xx_retry_backoff_s = _EVAL_ORIGIN_5XX_RETRY_BACKOFF_S
        # Bug 4 Fix C: count of in-flight FOREGROUND android sequences (raised by
        # device_session(), read by _refresh_tick). A non-zero count defers the
        # proactive-refresh loop so it never navigates the shared WebView away while a
        # comix solve+eval sequence holds the device (the page stays warm). A counter
        # (nesting-safe) holding NO lock — the gathered evals inside still serialize
        # per-op on _device_lock.
        self._foreground_inflight = 0
        # Bug 5 Fix #2 (startup-warm gate). ``_warm_complete`` is set by :meth:`warm`'s
        # finally (on EVERY exit — success, partial failure, raise) so a foreground
        # sequence parked at the device door is always released. ``_warm_gate_armed``
        # gates the wait: False until :meth:`arm_warm_gate` (the app lifespan,
        # synchronously before the non-blocking warm task is created) so a solver that
        # never warms (tests / unconfigured / CI) never waits. The wait takes NO lock —
        # it only awaits an Event — and warm()/the refresh loop NEVER enter
        # :meth:`device_session`, so awaiting warm here cannot deadlock with the
        # foreground lease or the per-op ``_device_lock``.
        self._warm_gate_timeout_s = warm_gate_timeout_s
        self._warm_complete = asyncio.Event()
        self._warm_gate_armed = False

    def arm_warm_gate(self) -> None:
        """Arm the startup-warm gate (Bug 5 Fix #2; app lifespan, synchronous).

        Called by the app lifespan BEFORE the non-blocking warm task is created — so by
        the time the server accepts any request the gate is armed and a foreground
        android sequence (a comix solve+eval inside :meth:`device_session`) WAITS for
        :meth:`warm` to finish before claiming the shared device. Synchronous +
        race-free (runs in lifespan startup, before the lifespan yields). Idempotent;
        clearing the completion is :meth:`warm`'s job (its finally sets
        ``_warm_complete``)."""
        self._warm_gate_armed = True
        self._warm_complete.clear()

    async def _await_warm_gate(self) -> None:
        """Block a FOREGROUND sequence at the device door until startup warm() finishes.

        Bug 5 Fix #2: a comix /search that arrives DURING the startup warm storm would
        otherwise race the in-flight foreign solves — a queued warm/refresh solve can
        steal the single shared WebView mid-sequence, and comix's recovery re-nav burns
        the per-source budget (the collision spike). Gating the foreground sequence here
        lets warm() (others-first, comix-last per :meth:`warm`'s ordering) complete
        first, so the first post-boot search finds every source cleared and the page
        parked on comix. No-op unless armed (:meth:`arm_warm_gate`, the app lifespan) —
        a solver that never warms (tests / unconfigured sidecar / CI) never waits. FALLS
        THROUGH on timeout (warm slow/hung) rather than hanging the search forever;
        Lever B then defers any straggler warm solve. Deadlock-free: it awaits ONLY an
        Event (no lock) and warm()/the refresh loop NEVER enter :meth:`device_session`,
        so this can never wait on a holder of the lease or the per-op
        ``_device_lock``."""
        if not self._warm_gate_armed or self._warm_complete.is_set():
            return
        try:
            async with asyncio.timeout(self._warm_gate_timeout_s):
                await self._warm_complete.wait()
        except TimeoutError:
            _log.warning(
                "AndroidSolver startup-warm gate timed out after %.0fs — proceeding "
                "with the foreground sequence (warm slow/incomplete; Lever B defers "
                "any straggler warm solve)",
                self._warm_gate_timeout_s,
            )

    async def get_clearance(
        self,
        source_key: str,
        *,
        force_resolve: bool = False,
        solve_if_missing: bool = True,
        defer_if_foreground: bool = False,
    ) -> Clearance | None:
        """Return clearance for ``source_key`` (``None`` for non-android keys).

        A key absent from the android challenge-url map → ``None`` (BOT-01 — the
        gateway never touches the sidecar for it). Otherwise a non-force call reuses
        the held clearance when present; ``force_resolve=True`` discards it and runs a
        FRESH sidecar solve (D-35, the 403 self-heal). ``force_resolve`` is a declared
        keyword so context.py's ``_call_solver`` ``inspect.signature`` detection passes
        it through (D-41).

        ``defer_if_foreground`` (Bug 5 Lever B) is for BACKGROUND/eager callers only —
        the sole user is :meth:`warm`. When ``True`` and the solve has to actually run,
        it is threaded into the device op so the solve BAILS (raises
        :class:`_RefreshDeferred`) if a FOREGROUND lease (a comix sequence inside
        :meth:`device_session`) is claimed while it queues on the device lock — the
        eager warm never navigates the shared WebView away from a request that arrived
        DURING startup warm(). It defaults ``False`` so EVERY foreground caller (the
        request path, the D-35 force-resolve self-heal, the csrf-bootstrap peek) is
        byte-for-byte unchanged and NEVER defers itself.

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
        if source_key in self._warm_last_keys:
            # Bug 5 follow-on #3 (live-confirmed): a page-HOLDING source (comix) has NO
            # replayable host-scoped ``cf_clearance`` — its data path is the cleared
            # WebView page + the in-page signed env-module API, not a cookie replayed
            # over httpx (the sidecar logs ``no host-scoped clearance cookie found for
            # host comix.to`` on every warm eval). It MUST NOT be ``/solve``d
            # (``pm clear`` cold-wipes the warm page it needs → 5xx) and must NOT block
            # the request hot path on a per-request device eval. So serve held-or-None
            # and never mint here; the warm prime opportunistically holds a cookie IF a
            # real Turnstile clear deposits one, else the open CDN uses None.
            return await self._page_holder_clearance(source_key, force_resolve)
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
        clearance, expires_at = await self._coalesced_solve(
            source_key, defer_if_foreground=defer_if_foreground
        )
        self._held[source_key] = clearance
        self._record_expiry(source_key, expires_at)
        return clearance

    async def _page_holder_clearance(
        self, source_key: str, force_resolve: bool
    ) -> Clearance | None:
        """Serve a page-holder's (comix's) clearance: held-or-None, never ``/solve``.

        Bug 5 follow-on #3 (live-confirmed): comix is cleared by its warm hydrated eval
        page, NOT a replayable ``cf_clearance`` cookie — its search/recent/manifest run
        in-page via ``eval_in_webview`` (which need NO injected clearance) and its image
        CDN serves the bulk bytes WITHOUT a Cloudflare cookie. So a normal request
        serves whatever the warm prime opportunistically captured (``None`` in practice)
        with NO device hit — never blocking the hot path on a per-request eval, never a
        ``/solve``.

        ``force_resolve`` (the D-35 403 self-heal — fired only when an httpx leg hit a
        CF challenge 403) discards the stale capture and runs ONE opportunistic
        eval re-clear: if comix re-challenged and the interactive Turnstile clear
        deposited a host-scoped ``cf_clearance`` we hold + return it; otherwise we serve
        ``None`` (the eval re-cleared the warm page either way). It is LENIENT — a
        missing cookie is the norm for comix, not a failure (contrast a cf_clearance
        source, whose force-resolve ``/solve`` must yield a token)."""
        if not force_resolve:
            return self._held.get(source_key)
        self._held.pop(source_key, None)
        self._expires_at.pop(source_key, None)
        try:
            clearance, expires_at = await self._mint_clearance_via_eval(source_key)
        except Exception:  # noqa: BLE001 — a failed re-clear is best-effort (serve None)
            _log.warning(
                "AndroidSolver page-holder %r force-resolve eval re-clear failed; "
                "serving no clearance (comix clears via its warm eval page)",
                source_key,
                exc_info=True,
            )
            return None
        if clearance is not None:
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
        if defer_if_foreground:
            # Deferrable background solves (warm/refresh) must NOT share the
            # single-flight slot with request-path callers: ``_coalesce`` keys only by
            # ``source_key``, so a foreground caller (``defer_if_foreground=False``)
            # awaiting a shared deferrable task would receive its ``_RefreshDeferred``
            # — a control-flow signal only warm/refresh understand — violating the
            # get_clearance contract that foreground calls NEVER defer (CodeRabbit).
            # Run deferrable solves on their own task; the background herd for one key
            # is rare (warm solves a key once; the refresh loop one at a time) so the
            # lost coalescing is immaterial.
            return await self._solve(source_key, defer_if_foreground=True)
        return await self._coalesce(
            source_key,
            lambda: self._solve(source_key, defer_if_foreground=False),
        )

    async def _coalesce(
        self,
        source_key: str,
        factory: Callable[[], Coroutine[Any, Any, tuple[Clearance, float | None]]],
    ) -> tuple[Clearance, float | None]:
        """Per-source-key single-flight around ``factory`` (#296; shared by solve+eval).

        Registers ONE in-flight task per ``source_key`` so a same-key herd shares one
        device hit; distinct keys get distinct tasks (no global lock). Awaited under
        :func:`asyncio.shield` so one abandoned awaiter cannot tear the shared task down
        for the herd. Factored out of :meth:`_coalesced_solve` so the EVAL-path mint
        reuses the identical de-dup contract.
        """
        task = self._inflight.get(source_key)
        if task is None or task.done():
            task = asyncio.create_task(factory())
            self._inflight[source_key] = task

            def _pop(
                completed: asyncio.Task[tuple[Clearance, float | None]],
            ) -> None:
                # Clear the slot only when it still holds THIS task, so a later
                # task is never clobbered by an earlier one's callback.
                if self._inflight.get(source_key) is completed:
                    del self._inflight[source_key]

            task.add_done_callback(_pop)
        return await asyncio.shield(task)

    def _record_expiry(self, source_key: str, expires_at: float | None) -> None:
        """Track (or clear) the held clearance's epoch expiry for the refresh loop.

        Bug 5 Lever A: when ``refresh_max_age_s`` is configured, cap the tracked expiry
        at ``now + refresh_max_age_s`` whenever that is SOONER than the real cookie
        expiry (or whenever the cookie carries no usable expiry at all). This drives the
        EXISTING ``_expires_at``-based proactive-refresh loop to re-mint the clearance
        on the empirical re-challenge cadence rather than the meaningless ~1yr cookie
        ``expires`` (debug ``cf-clearance-cookie-expiry-is-not-lapse-time``), so comix
        is re-cleared off the request hot path. Disabled (``None``) ⇒ the historic
        cookie-expiry-only behavior, byte-for-byte unchanged.
        """
        effective = expires_at
        if self._refresh_max_age_s is not None:
            age_cap = time.time() + self._refresh_max_age_s
            effective = age_cap if effective is None else min(effective, age_cap)
        if effective is not None:
            self._expires_at[source_key] = effective
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

        Bug 5 follow-on #3 (Option A2): the page-HOLDING keys (``warm_last_keys`` —
        comix) are NOT routed through the ``cf_clearance`` ``/solve`` at all. ``_solve``
        runs ``force_stop_and_clear`` (``pm clear`` — a COLD storage wipe of the
        WebView: ``cf_clearance``, ``__cf_bm``, the cached ``env-*.js`` module) then a
        cold launch, on which comix's managed Cloudflare challenge presents NEITHER a
        tappable Turnstile OOPIF NOR a host-scoped ``cf_clearance`` → the solve burns
        the full deadline AND poisons the warm page comix's eval path depends on. comix
        clears ONLY via the EVAL path (a warm ``Page.navigate`` that RETAINS ``__cf_bm``
        + the env module → the interactive OOPIF clears in 5-9s). So the page-holders
        are EVAL-PRIMED LAST instead (:meth:`_prime_webview_page`): a no-op eval over
        the warm WebView that leaves the single redroid ended startup parked on a
        CLEARED + hydrated comix page (even the first post-boot search is fast). The
        clearance-only keys are solved FIRST, in ``challenge_urls`` order.

        Bug 5 Lever B: each eager clearance solve passes ``defer_if_foreground=True`` so
        a request that arrives DURING startup warm() (claiming a :meth:`device_session`
        foreground lease) is never robbed of the shared WebView by a queued warm
        solve — the warm solve BAILS (:class:`_RefreshDeferred`) instead. A deferred key
        is NOT a fail: it is left unwarmed and solves on-demand on first use (the
        request holding the lease) or via the next proactive refresh — NOT disabled.

        Bug 5 Fix #2 (startup-warm gate): the completion Event is set in the ``finally``
        on EVERY exit (success, partial failure, OR an outright raise) so a foreground
        android sequence parked at the device door (:meth:`_await_warm_gate`) is ALWAYS
        released — warm() never hangs a search. Because the gate is released only in
        this ``finally`` — AFTER both the clearance ``/solve`` loop AND the page-holder
        eval-prime loop — the gate waits on BOTH readiness sets (the first post-boot
        search finds every clearance source solved AND comix parked on its cleared
        page). Self-arms too, so a direct ``warm()`` call (no app lifespan) still gates.
        """
        self._warm_gate_armed = True
        failed: list[str] = []
        try:
            # (A2 part 1) /solve ONLY the cf_clearance sources (mangadot/kagane/
            # mangaball) — the page-holders (comix) are EXCLUDED here and eval-primed
            # below. ``challenge_urls`` order preserved.
            for key in self._challenge_urls:
                if key in self._warm_last_keys or key in self._on_demand_keys:
                    # Page-holder (eval-primed below) or on-demand source (never
                    # eager-warm — it solves only when a live challenge is detected,
                    # debug pooltimeout-recurrence): skip the eager /solve.
                    continue
                try:
                    await self.get_clearance(key, defer_if_foreground=True)
                except _RefreshDeferred:
                    # Bug 5 Lever B: a foreground lease was held while this eager warm
                    # queued on the device lock — skip it WITHOUT marking it failed (it
                    # is not a solve failure, so it must not be force-disabled). It
                    # solves on-demand on first use or via the next proactive refresh.
                    _log.info(
                        "AndroidSolver warm deferred for source %r — a foreground "
                        "lease held the device; will solve on-demand / next refresh "
                        "(NOT disabled)",
                        key,
                    )
                except Exception as exc:  # noqa: BLE001 — isolate per-key warm failures
                    _log.warning(
                        "AndroidSolver warm failed for source %r: %r "
                        "(boots disabled — D-33)",
                        key,
                        exc,
                        exc_info=True,
                    )
                    failed.append(key)
            # (A2 part 2) EVAL-PRIME the page-holders (comix) LAST via the eval path —
            # NO /solve, NO pm clear. Best-effort: a failed prime is logged and skipped,
            # NEVER force-disabled (the first real /search clears it via the same eval
            # path). Page-holders order preserved within the group.
            for key in self._challenge_urls:
                if key not in self._warm_last_keys or key in self._on_demand_keys:
                    continue
                await self._prime_webview_page(key)
        finally:
            # Bug 5 Fix #2: release the gate on EVERY exit so a parked foreground
            # sequence proceeds whether warm succeeded, partially failed, or raised.
            self._warm_complete.set()
        return failed

    async def _prime_webview_page(self, source_key: str) -> None:
        """EVAL-PRIME a page-holding source (comix) AND hold its clearance (Bug 5 #3).

        Runs the no-op prime eval (:data:`_WEBVIEW_PRIME_JS`) with
        ``extract_clearance=True`` via :meth:`_mint_clearance_via_eval`, so the
        sidecar's ``_drive_eval`` warm-navigates → clears-if-challenged (the interactive
        Turnstile tap, NO ``pm clear``) → hydrates the page → reads back the host-scoped
        ``cf_clearance`` comix's managed challenge auto-issued. This leaves the single
        redroid parked on a CLEARED + hydrated comix page at the end of startup (so even
        the first post-boot search is fast — no first-search re-clear) AND populates
        ``_held``/``_expires_at`` with comix's replayable clearance, so the very first
        D-40 httpx image leg already has a valid ``cf_clearance`` cookie + bound UA
        WITHOUT any ``/solve`` (follow-on #3: ``/solve``'s ``pm clear`` would cold-wipe
        the warm WebView comix depends on).

        Proxy parity is automatic: :meth:`_mint_clearance_via_eval` posts through the
        SAME ``/eval`` transport (forwarding ``self._proxy``) a real comix ``/search``
        eval uses, so the prime nav egresses the SAME residential sticky IP the
        eval-path clearance is bound to — no egress-verify mismatch. ``wait_for=None``
        so the prime relies only on the ``env-*.js`` hydration signal, not a DOM probe.

        Deadlock-free: the eval-mint takes only the per-op ``_device_lock`` (via
        :meth:`_device_op`, ``defer_if_foreground=False``) and NEVER awaits the warm
        gate (:meth:`_await_warm_gate` is entered only by :meth:`device_session`, which
        the prime does NOT use) — so a prime that is itself PART of ``warm()`` cannot
        block on the gate ``warm()`` must satisfy. During warm() the gate holds every
        foreground search at the device door (``_foreground_inflight == 0``), so the
        prime acquires the device lock uncontended.

        Best-effort + LENIENT: bounded by ``warm_gate_timeout_s`` (so a CF-flaky prime
        cannot run the full eval op-budget and hold the device past the startup
        readiness window). The eval FAILING (CF flaky → /eval non-200) is logged +
        swallowed (the source is NOT marked failed / never force-disabled). A SUCCESSFUL
        prime that extracts NO host-scoped cookie is the EXPECTED comix case (warm
        fast-path eval, no Turnstile) — the page is parked cleared+hydrated for the eval
        data path and ``_held`` is left empty (comix's image CDN needs no cookie).
        """
        try:
            clearance, expires_at = await self._mint_clearance_via_eval(
                source_key,
                # Cap the prime at the startup readiness window so a hung/flaky prime
                # gives up + frees the device before the warm gate falls through (it
                # never runs the full ~180s eval op-budget as a startup zombie).
                timeout=self._warm_gate_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort prime; never hang/disable
            _log.warning(
                "AndroidSolver eval-prime failed for page-holding source %r: %r — "
                "leaving it to clear on the first /search via the same eval path "
                "(NOT disabled)",
                source_key,
                exc,
                exc_info=True,
            )
            return
        if clearance is not None:
            self._held[source_key] = clearance
            self._record_expiry(source_key, expires_at)
            _log.info(
                "AndroidSolver eval-primed page-holder %r — warm WebView parked "
                "on its cleared, hydrated page AND opportunistically held a clearance",
                source_key,
            )
        else:
            _log.info(
                "AndroidSolver eval-primed page-holder %r — warm WebView parked "
                "on its cleared, hydrated page (no host-scoped cf_clearance to hold; "
                "comix clears via its warm eval page, not a replayable cookie)",
                source_key,
            )

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

    async def _post_sidecar(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: float,  # noqa: ASYNC109 — per-call op-budget, mirrors the eval/solve cap
    ) -> httpx.Response:
        """POST to the sidecar, RETRYING the ``503 solver busy`` backpressure (bounded).

        The single redroid is shared, so an op can hit the sidecar's non-blocking lock
        while ANOTHER caller's op is in flight → ``503 solver busy`` (by design, a
        retryable signal — the gateway's ``_device_op`` only serializes its own calls).
        Retry that 503 a bounded number of times with a linear backoff so a transient
        cross-caller collision resolves itself instead of failing the request as
        ``upstream 503``. A non-503 response (200, or a real ``504``/4xx) returns
        immediately for the caller's ``raise_for_status``; the LAST 503 is returned once
        the budget is spent (a genuinely wedged device still fails loud). The body/token
        is never logged.
        """
        resp = await self._ensure_client().post(
            url, json=json, headers=headers, timeout=timeout
        )
        attempt = 1
        while (
            resp.status_code == _SIDECAR_BUSY_STATUS
            and attempt < self._busy_retry_attempts
        ):
            await asyncio.sleep(self._busy_retry_backoff_s * attempt)
            attempt += 1
            resp = await self._ensure_client().post(
                url, json=json, headers=headers, timeout=timeout
            )
        return resp

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

        Bug 5 Fix #2 (startup-warm gate): BEFORE claiming the lease, the sequence WAITS
        for the startup :meth:`warm` to finish (:meth:`_await_warm_gate`, bounded), so a
        search that arrives during the warm storm does not race the in-flight foreign
        solves. Waiting BEFORE the increment keeps ``_foreground_inflight == 0`` during
        warm, so warm()'s own solves (defer_if_foreground=True) do NOT bail — warm
        completes fully, then the search proceeds against a fully-cleared device parked
        on the page-holder. The wait is a no-op until the app arms the gate, so every
        offline test enters the lease immediately exactly as before.
        """
        await self._await_warm_gate()
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
                resp = await self._post_sidecar(
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

        Mode-E follow-up: a SINGLE bounded retry of a TRANSIENT comix-origin 5xx
        in-page throw (``_is_origin_5xx_eval_throw``). comix's in-page env-module axios
        (``c.list`` / ``chapters``) occasionally hits comix's own flaky origin and gets
        a Cloudflare 521/520/522; the sidecar now surfaces that as a ``504 eval threw``
        with the axios ``status code 5xx`` in the body. Without a retry that one-off
        origin blip returns 0 releases for the WHOLE search (failing single-shot callers
        — e.g. ``external_links[comix]`` — even with the 60s negative-cache, which only
        self-heals the NEXT search). The retry is ONE extra full device eval (re-takes
        the lock, composes with ``_post_sidecar``'s busy-503 retry) and fires ONLY on
        the rare origin 5xx, which fails FAST (the axios rejects on the edge, never
        hanging to the eval timeout) → it fits the per-source 30s search budget. It does
        NOT retry a CLEAN empty (a ``200`` ``[]`` genuine no-match), a 4xx, a
        non-origin/CF/hydration throw, or a timeout/cancel — those re-raise on the first
        failure.
        """
        # Reverse-map the challenge_url → source key for comix attribution, the same
        # dict _solve indexes. Emit ONCE per OUTER eval_in_webview call (not per inner
        # 5xx retry attempt) via try/finally, so a comix /eval shows as one real-timed
        # row regardless of the bounded origin-5xx retry. OBSERVABILITY ONLY — the
        # return value and the retry semantics below are untouched.
        source_key = next(
            (k for k, v in self._challenge_urls.items() if v == challenge_url), None
        )
        start = time.perf_counter()
        try:
            attempts = self._eval_origin_5xx_retry_attempts
            for attempt in range(attempts + 1):
                try:
                    payload = await self._post_eval(
                        challenge_url, js, wait_for=wait_for, timeout=timeout
                    )
                except httpx.HTTPStatusError as exc:
                    if attempt < attempts and _is_origin_5xx_eval_throw(exc.response):
                        # Transient comix-origin 5xx — back off briefly and re-eval
                        # ONCE on the warm device (the busy-503 retry inside _post_eval
                        # still applies to each attempt). A persistent 5xx exhausts the
                        # bounded attempts and re-raises on the final pass → a
                        # per-source warning.
                        await asyncio.sleep(self._eval_origin_5xx_retry_backoff_s)
                        continue
                    raise
                _emit_eval(
                    source_key,
                    outcome="ok",
                    duration_ms=(time.perf_counter() - start) * 1000.0,
                    error=None,
                )
                return payload["value"]
            # Unreachable: the loop always returns a value or re-raises on the final
            # attempt (`attempt < attempts` is False on the last pass). Satisfies mypy.
            raise AssertionError("eval_in_webview retry loop exited without returning")
        except Exception as exc:
            _emit_eval(
                source_key,
                outcome="error",
                duration_ms=(time.perf_counter() - start) * 1000.0,
                error=type(exc).__name__,
            )
            raise

    async def _post_eval(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — per-call op-budget override
        extract_clearance: bool = False,
    ) -> dict[str, Any]:
        """POST the sidecar ``/eval`` and return the FULL response payload.

        The shared transport for both :meth:`eval_in_webview` (which returns
        ``payload["value"]``) and the page-holder clearance mint
        (:meth:`_mint_clearance_via_eval`, ``extract_clearance=True``). When
        ``extract_clearance`` is set the sidecar ALSO reads the host-scoped
        ``cf_clearance`` it auto-issued during the eval-path clear and returns it under
        a ``"clearance"`` block — so the gateway can mint+hold a page-holding source's
        (comix's) replayable clearance with NO destructive ``/solve`` (Bug 5 follow-on
        #3). The flag rides the body ONLY when set, so an ordinary eval's body is
        byte-for-byte unchanged. The returned token rides only the response, not a log.
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
        # Bug 5 follow-on #3: ask the sidecar to ALSO return the host-scoped
        # cf_clearance it auto-issued during the eval-path clear, so the gateway can
        # mint+hold comix's replayable clearance for the httpx image leg with NO /solve.
        # Present ONLY when requested (an ordinary eval is byte-for-byte unchanged).
        if extract_clearance:
            body["extract_clearance"] = True
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
            resp = await self._post_sidecar(
                f"{self._base_url}/eval",
                json=body,
                headers=headers,
                timeout=timeout if timeout is not None else self._timeout_s,
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
        return result

    async def _mint_clearance_via_eval(
        self,
        source_key: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 — per-call op-budget override
    ) -> tuple[Clearance | None, float | None]:
        """Eval-path re-clear a page-holder (comix); capture a ``cf_clearance`` if any.

        Bug 5 follow-on #3 — comix can NEVER be ``/solve``d (``/solve`` runs
        ``pm clear``, cold-wiping the warm WebView its managed challenge + cached
        ``env-*.js`` depend on → no clearance, burned deadline → 5xx). Instead run the
        no-op prime eval (:data:`_WEBVIEW_PRIME_JS`) with ``extract_clearance=True``:
        the sidecar warm-navigates → clears-if-challenged (interactive Turnstile tap, NO
        ``pm clear``) → hydrates → reads back the host-scoped ``cf_clearance`` IF the
        clear deposited one. LIVE REALITY (decisive): on a warm already-hydrated comix
        page the eval takes the fast path (no Turnstile, no cookie), so the sidecar
        returns NO host-scoped ``cf_clearance`` — comix's "cleared" state is the warm
        page + the in-page signed env-module API, not a replayable cookie. So this
        returns ``(None, None)`` (NOT a raise) when no cookie is present — the warm page
        is still cleared+hydrated for the eval data path, and comix's open image CDN
        serves bytes without a CF cookie. When a real Turnstile clear DID deposit
        a cookie it is returned as the usual ``Clearance`` (cookie + UA). Never logged.
        """
        challenge_url = self._challenge_urls[source_key]
        payload = await self._post_eval(
            challenge_url,
            _WEBVIEW_PRIME_JS,
            wait_for=None,
            timeout=timeout,
            extract_clearance=True,
        )
        return self._clearance_from_eval_payload(payload)

    @staticmethod
    def _clearance_from_eval_payload(
        payload: dict[str, Any],
    ) -> tuple[Clearance | None, float | None]:
        """Parse a ``/eval`` ``extract_clearance`` payload → ``(Clearance|None, exp)``.

        Returns ``(None, None)`` when the sidecar extracted no host-scoped
        ``cf_clearance`` (the common case for comix: a warm fast-path eval, or a clean
        clear that deposited no cookie) — NOT a failure for a page-holder, whose data
        path is the warm eval page, not a cookie. A present cookie is returned as a
        replayable ``Clearance`` (cookie + bound UA). The token is NEVER logged.
        """
        clearance_raw = payload.get("clearance")
        if not isinstance(clearance_raw, dict):
            return (None, None)
        token = clearance_raw.get("cf_clearance")
        if not isinstance(token, str) or not token:
            return (None, None)
        user_agent = clearance_raw.get("user_agent")
        expires_at = _parse_expiry(clearance_raw.get("cf_clearance_expires"))
        return (
            Clearance(
                cookies={"cf_clearance": token},
                user_agent=user_agent if isinstance(user_agent, str) else "",
            ),
            expires_at,
        )

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
            if key in self._on_demand_keys or key in self._warm_last_keys:
                # Bug 5 follow-on #3: a page-holder (comix) is NEVER proactively
                # refreshed via ``/solve`` (``pm clear`` wipes its page) — it has no
                # replayable cf_clearance to re-mint anyway (its data path is the warm
                # eval page). Its clearance, IF a Turnstile clear ever deposited one, is
                # re-captured reactively on a 403 self-heal (force-resolve) instead.
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
            if key not in self._on_demand_keys and key not in self._warm_last_keys
        ]
        if not upcoming:
            return self._refresh_max_sleep_s
        delay = min(upcoming) - time.time()
        return max(self._refresh_min_sleep_s, min(self._refresh_max_sleep_s, delay))
