"""Authenticated, SSRF-safe, serialized control API for the android-solver sidecar.

Wraps the Plan 10-01 pipeline pieces (``device`` driver + ``turnstile`` locator +
``cdp`` extractor) in the minimal HTTP control surface the gateway's
``AndroidSolver`` (Plan 10-03) calls:

  * ``GET  /healthz`` → 200 when the redroid adb target answers.
  * ``POST /solve``   → mint a Cloudflare clearance for an ALLOWLISTED host.

Security posture (SEC-01):
  * T-10-08 — every ``/solve`` requires the ``X-Solver-Key`` header to equal the
    configured api key (constant-time compare); 401 otherwise.
  * T-10-09 — the ``challenge_url`` host is validated against the allowlist BEFORE
    any device action; a non-allowlisted host is rejected 422 (no arbitrary-URL
    solve — the gateway only ever sends a source's own challenge URL).
  * T-10-11 — solves are SERIALIZED (one WebView at a time) under a process lock
    and bounded by an overall timeout (504 on expiry).
  * T-10-10 — the clearance token VALUE is never written to the logs; only a
    redacted ``solved <host>`` event is emitted.

The control API is bound docker-internal-only (config default 0.0.0.0 inside the
container, NO published host port — enforced by the 10-05 compose).

R1: this module imports ONLY the sibling sidecar modules + the stdlib — never
anything from ``src/manga_gateway``. The HTTP layer is stdlib ``http.server`` so
the sidecar image needs no web-framework dependency.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import select
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock
from typing import Any, Protocol
from urllib.parse import urlsplit

from android_solver.cdp import (
    ClearanceCookie,
    HttpGetter,
    WebSocketFactory,
    WebSocketLike,
    cdp_call,
    extract_clearance,
    webview_user_agent,
)
from android_solver.config import SidecarConfig
from android_solver.device import AdbDevice, AdbError
from android_solver.egress import expected_egress
from android_solver.proxy_hop import control_endpoint, repoint
from android_solver.turnstile import locate_checkbox

_log = logging.getLogger("android_solver.service")

# Default page-render settle + clearance-poll cadence for the real pipeline.
# The OOPIF widget appears a few seconds AFTER load, so a short fixed settle is
# only a floor — the real readiness gate is polling Page.getFrameTree for the
# challenges.cloudflare.com child frame (_FRAME_POLL_* below).
_LAUNCH_SETTLE_S = 2.0
_POLL_INTERVAL_S = 1.5
# Belt-and-braces attempt cap on the post-relaunch pid poll in
# ``_ensure_webview_alive`` — the eval ``deadline`` is the primary bound; this just
# guarantees the loop terminates if the clock is stubbed to 0.0 in tests.
_RELAUNCH_PIDOF_ATTEMPTS = 15
_WS_TIMEOUT_S = 15.0
_HTTP_TIMEOUT_S = 10.0

# Pre-auth request hardening for the LAN-exposed control API (CR-01). A /solve
# body is tiny JSON (``{"challenge_url": "https://<host>/"}``), so any larger
# Content-Length is rejected 413 BEFORE the body is read — an unauthenticated
# caller can never force a multi-GB allocation. The handler also carries a socket
# read timeout so a slow-loris dribble cannot pin a worker thread indefinitely.
_MAX_BODY_BYTES = 64 * 1024
_HANDLER_READ_TIMEOUT_S = 15.0

# #275: while a solve is in flight the service waits on the worker future in short
# polls so a caller-disconnect can be detected and the solve cancelled PROMPTLY
# instead of grinding the full solve deadline against the single device. 499 is the
# non-standard "Client Closed Request" status (nginx convention) returned when an
# abandoned /solve is cancelled mid-flight.
_DISCONNECT_POLL_S = 0.5
_CLIENT_CLOSED_REQUEST = 499


def _peer_disconnected(sock: socket.socket) -> bool:
    """True when the HTTP peer has closed its end of ``sock`` (#275).

    A ``select`` with a zero timeout asks "is there anything to read?"; a
    not-readable socket means the peer is still connected with nothing pending
    (``False``). A readable socket that ``MSG_PEEK``s an empty result is at EOF — the
    peer closed the connection (``True``). An ``OSError`` (the socket is already torn
    down) is likewise treated as disconnected. Used by the handler to wire an
    abandoned /solve to the pipeline's cooperative cancel.
    """
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


# Cross-origin Cloudflare OOPIF readiness poll: the Turnstile iframe renders a
# few seconds after the page loads, so wait for it before locating the checkbox.
_CF_FRAME_URL_MARKER = "challenges.cloudflare.com"
_FRAME_POLL_TIMEOUT_S = 20.0
_FRAME_POLL_INTERVAL_S = 1.5

# Re-tap cadence: on a COLD WebView the challenges.cloudflare.com OOPIF frame URL
# appears BEFORE the checkbox inside it is interactive, so a single tap lands on
# a not-yet-ready widget and misses. We re-locate + re-tap at most every
# _RETAP_INTERVAL_S (polling for clearance in between) until the token is minted
# or the deadline fires. Two guards keep a re-tap from DESTROYING a tap that DID
# register: (a) we never tap within _RETAP_INTERVAL_S of the previous tap, which
# gives a registered tap time to surface Turnstile's success text; and (b) once
# that success/verification window is detected (_in_verification) we stop tapping
# entirely and just poll out the clearance — a tap fired during the
# "Verification successful, waiting for <host> to respond" phase RESETS the
# widget and aborts the solve (the bug this loop exists to avoid).
_RETAP_INTERVAL_S = 5.0

# Main-challenge-page probe: Turnstile's post-tap success/verification banner
# ("Verification successful, waiting for <host> to respond") renders in a
# `#challenge-success-text` div in the TOP frame (NOT the OOPIF). While it is
# visible the checkbox must NOT be re-tapped. Runtime.evaluate runs in the main
# frame's default context (no Runtime.enable needed — same as _compute_scales).
_VERIFY_PROBE_CMD_ID = 300
_VERIFY_PROBE_JS = (
    "(function(){try{"
    "var sels=['#challenge-success-text','.challenge-success-text',"
    "'#challenge-success'];"
    "for(var i=0;i<sels.length;i++){var el=document.querySelector(sels[i]);"
    "if(el&&(el.offsetParent||el.getClientRects().length))return true;}"
    "var t=document.body?(document.body.innerText||''):'';"
    "return /verification successful|waiting for .* to respond/i.test(t);"
    "}catch(e){return false;}})()"
)

# Per-solve proxied-egress verification (Req 3 / D-03/D-04/D-05). After the device
# proxy is set and CDP is up, the WebView is navigated to a NEUTRAL IP echo and its
# observed egress is read in-browser, then compared to the sidecar self-probe's
# expected egress (learned through the SAME authenticated upstream). A mismatch — a
# silently-bypassed proxy — or any transient navigate/eval/parse failure raises
# (NO token, T-11-01). The echo target is a sidecar CONSTANT, never caller-supplied
# (no new SSRF surface, T-11-06).
#
# STICKY-SESSION REQUIREMENT (WR-02 — read before configuring a proxy upstream):
# the self-probe opens ONE CONNECT through the upstream and the WebView opens a
# SEPARATE connection through the SAME upstream; the two observed IPs are asserted
# BYTE-EQUAL. This is the T-11-01 security invariant — the minted cf_clearance must
# bind to the EXACT egress, so the comparison is deliberately exact-IP (NOT /24 or
# ASN). It therefore REQUIRES a STICKY upstream that holds one exit IP across both
# connections. A ROTATING residential endpoint (exit IP changes per TCP connection,
# the common default unless a "sticky session" plan is selected) will mismatch on
# nearly every solve and NO proxied solve will ever mint a token. Configure a
# sticky-session upstream; the mismatch SolveError names the rotating-proxy cause so
# the failure is not mistaken for a Cloudflare problem.
_EGRESS_ECHO_URL = "https://api.ipify.org"
_EGRESS_NAV_CMD_ID = 40
_EGRESS_READ_CMD_ID = 41
_CHALLENGE_RENAV_CMD_ID = 42
_EGRESS_READ_TIMEOUT_S = 15.0
_EGRESS_READ_JS = "document.body.innerText"

# Phase 14 bug 4 / Fix A: a NON-DESTRUCTIVE in-page egress probe for the proxied
# /eval path. The destructive ``_observe_webview_egress`` above navigates the main
# WebView AWAY to the IP echo, which defeats the bug-3 fast path (the cleared+
# hydrated comix page is gone, so every eval re-navigates → re-clears comix's
# managed Cloudflare challenge — the ~10-15s per-eval cost behind bug 4). This probe
# instead reads the egress with an IN-PAGE ``fetch`` of the SAME ``_EGRESS_ECHO_URL``
# (NO ``Page.navigate``), so the warm comix page survives and the fast path engages.
# The byte-equal egress assertion (T-11-01) still runs on EVERY proxied eval; only
# the transport changes (destructive nav-away → same-device-proxy XHR). The fetch
# runs in comix's origin, so a CSP/CORS block or a transient network error makes the
# IIFE return ``null`` → the caller treats it as "probe unavailable" and FALLS BACK
# to the destructive probe (an eval is NEVER run unverified). ``_EGRESS_IN_PAGE_CMD_ID``
# is a fresh id distinct from every ``_EGRESS_*`` / ``_EVAL_*`` id so its response
# never aliases another in-flight frame.
_EGRESS_IN_PAGE_CMD_ID = 43
_EGRESS_IN_PAGE_JS = (
    "(async () => {"
    "  try {"
    f"    const r = await fetch({_EGRESS_ECHO_URL!r}, {{cache: 'no-store'}});"
    "    return (await r.text()).trim();"
    "  } catch (e) { return null; }"
    "})()"
)

# ── /eval (Phase 14): run gateway-supplied JS in the warm cleared WebView ─────
# The /eval endpoint re-composes the existing navigate + Runtime.evaluate
# primitives so comix's in-page token-mint / chapter-list / manifest logic runs
# inside the only fingerprint that clears comix.to's Cloudflare. The ONE new CDP
# option over the three existing Runtime.evaluate sites is ``awaitPromise:true``
# (comix's extractors are async; spike 021 A2). ``_EVAL_CMD_ID`` is a fresh
# command id distinct from the solve path's ``_*_CMD_ID`` values above so the
# eval's Runtime.evaluate response never aliases an in-flight solve frame.
_EVAL_CMD_ID = 400
_EVAL_NAV_CMD_ID = 401  # Page.navigate to the challenge_url before the eval runs
_EVAL_HYDRATION_CMD_ID = 402  # env-*.js Resource-Timing SPA-readiness probe
_EVAL_WAIT_FOR_CMD_ID = 403  # the optional caller-supplied wait_for JS predicate
_EVAL_LOCATION_CMD_ID = 404  # location.host probe for the already-hydrated fast path
_EVAL_FRAME_TREE_CMD_ID = 405  # Page.getFrameTree CF-interstitial probe (bug 3)
# Bound on the in-page eval-throw summary surfaced by ``EvalError`` (Mode-E). The
# summary is the JS error's first description line only — never the ``js`` or the
# marshalled value (T-14-04); the cap keeps a hostile/verbose stack from bloating a
# log line.
_EVAL_EXC_SUMMARY_MAX = 300
# SPA-ready signal (spike 021 A1): comix's ``env-*.js`` module appears in the
# Resource-Timing buffer once the SPA has hydrated enough to run the in-page
# extractors. Polled (bounded by the eval deadline) before running the gateway js.
_EVAL_HYDRATION_JS = (
    "performance.getEntriesByType('resource')"
    ".some(function(e){return /env-.*\\.js/.test(e.name);})"
)
# Settle floor before the env-*.js readiness poll, mirroring the solve path's
# launch-settle before the devtools socket is driven.
_EVAL_HYDRATION_TIMEOUT_S = 20.0


class SolveError(RuntimeError):
    """The solve pipeline could not mint a clearance (surfaces as 504)."""


class SolveCancelled(RuntimeError):
    """The solve was cooperatively cancelled after the timeout (issue #207).

    Raised by the pipeline when it observes the cancel signal at one of its
    adb/CDP checkpoints. The pipeline resets the device on its way out so the next
    solve starts clean; the service treats it as a (clean) post-timeout exit.
    """


class EvalError(RuntimeError):
    """An in-page ``/eval`` JS expression THREW (its promise rejected).

    ``Runtime.evaluate(awaitPromise:true)`` returns a successful CDP response with
    an ``exceptionDetails`` block (and NO ``result.value``) when the evaluated
    promise rejects. Without this the sidecar marshalled the absent value as
    ``None`` and returned ``200 {"value": null}`` — which the gateway's tolerant
    extractors (``_result_items`` / ``_chapter_list``) silently coerce to ``[]``,
    so a transient in-page failure (a hydration race where ``env-*.js`` is not yet
    importable, a momentary CF re-challenge, or an ``api.list`` network blip)
    became a FAST, WARNING-LESS zero-result search that was then cached as a sticky
    negative (Mode-E, debug ``comix-warm-hydration-wait``). Raising instead surfaces
    the throw as a ``504`` the gateway propagates → the per-source fan-out emits a
    warning, the empty is NEVER cached (``SingleFlightCache`` never caches a raise),
    and the next search self-heals. A GENUINE empty (the promise RESOLVES with an
    empty envelope, no ``exceptionDetails``) is unaffected — it returns ``[]`` as
    before. The summary message is bounded + first-line-only and NEVER carries the
    evaluated ``js`` or the marshalled value (T-14-04).
    """


def _raise_if_cancelled(cancel: Event | None) -> None:
    """Cooperative cancellation checkpoint (issue #207).

    The single-worker ``ThreadPoolExecutor`` cannot be force-killed, so a
    timed-out solve is abandoned by ``future.result(timeout=...)`` while its
    thread keeps driving the device — starving the next solve behind the service
    lock. The pipeline therefore calls this between every blocking adb/CDP step;
    once the service ``set()``s the event the next checkpoint raises and unwinds
    the solve promptly.
    """
    if cancel is not None and cancel.is_set():
        raise SolveCancelled("solve cancelled after timeout")


def _proxy_parts(
    proxy: dict[str, Any],
) -> tuple[str, str, int, str | None, str | None]:
    """Decompose a validated /solve proxy into the hop URI + self-probe inputs.

    Returns ``(upstream_uri, up_host, up_port, user, pw)`` where ``upstream_uri`` is
    the ``http://HOST:PORT#USER:PASS`` form pproxy needs to inject
    ``Proxy-Authorization`` on the upstream CONNECT (the ``#user:pass`` fragment is
    appended only when BOTH are present). ``up_host``/``up_port``/``user``/``pw``
    feed the stdlib self-probe (``egress.expected_egress``) through the SAME
    upstream. The credential-bearing URI is NEVER logged (T-11-02).
    """
    server = str(proxy["server"])
    split = urlsplit(server)
    up_host = split.hostname or ""
    up_port = split.port or (443 if split.scheme == "https" else 80)
    user = proxy.get("username")
    pw = proxy.get("password")
    upstream = f"{server}#{user}:{pw}" if user and pw else server
    return upstream, up_host, up_port, user, pw


@dataclass(frozen=True)
class SolveResult:
    """A successful solve: the minted token + the WebView UA it was minted under.

    ``egress_ip`` carries the VERIFIED proxied egress on a proxied solve (Req 6);
    it defaults to ``""`` so a no-proxy solve keeps a stable, byte-for-byte shape
    (D-08). The egress IP is loggable; ``cf_clearance`` is NEVER logged (T-10-04).
    """

    cf_clearance: str
    user_agent: str
    host: str
    egress_ip: str = ""
    # The minted cookie's wall-clock epoch expiry (the Cloudflare Challenge-Passage
    # TTL), or ``None`` for a session/unknown lifetime. Additive-only (D-08): present
    # on every response so the gateway can re-mint ahead of the lapse. Non-sensitive.
    cf_clearance_expires: float | None = None


class SolvePipeline(Protocol):
    """The solve surface the control API drives (a FAKE is injected in tests)."""

    def solve(
        self,
        challenge_url: str,
        host: str,
        cancel: Event | None = None,
        *,
        proxy: dict[str, Any] | None = None,
    ) -> SolveResult: ...

    def eval_in_webview(
        self,
        challenge_url: str,
        host: str,
        js: str,
        cancel: Event | None = None,
        *,
        wait_for: str | None = None,
        deadline: float | None = None,
        proxy: dict[str, Any] | None = None,
        with_clearance: bool = False,
    ) -> Any: ...

    def health(self) -> bool: ...


# ── the real pipeline: device → locate → tap → extract ───────────────────────


class AndroidSolvePipeline:
    """Compose the 10-01 pieces into one end-to-end clearance mint.

    force_stop_and_clear → launch_url → locate_checkbox (dynamic CDP DOM) →
    input_tap → poll extract_clearance until the token appears or the deadline
    fires → capture the WebView UA. Only ever driven against a host the control
    API already allowlisted (SSRF validation happens upstream in ``SolverService``).
    """

    def __init__(
        self,
        device: AdbDevice,
        *,
        timeout_s: float,
        ws_factory: WebSocketFactory | None = None,
        http_get: HttpGetter | None = None,
        launch_settle_s: float = _LAUNCH_SETTLE_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
        retap_interval_s: float = _RETAP_INTERVAL_S,
        frame_poll_timeout_s: float = _FRAME_POLL_TIMEOUT_S,
        frame_poll_interval_s: float = _FRAME_POLL_INTERVAL_S,
        hop_host: str = "",
        hop_port: int = 0,
        egress_read_timeout_s: float = _EGRESS_READ_TIMEOUT_S,
    ) -> None:
        self._device = device
        self._timeout_s = timeout_s
        self._launch_settle_s = launch_settle_s
        self._poll_interval_s = poll_interval_s
        self._retap_interval_s = retap_interval_s
        self._frame_poll_timeout_s = frame_poll_timeout_s
        self._frame_poll_interval_s = frame_poll_interval_s
        # The docker-reachable hop the device routes WebView egress through. Set
        # from config (hop_host = "android-solver", NOT localhost — Pitfall 1).
        self._hop_host = hop_host
        self._hop_port = hop_port
        # The hop's CONTROL channel is a SEPARATE loopback listener
        # (127.0.0.1:<hop_port + 1>) — the sidecar (same container as the hop
        # child) repoints over it. This is NOT the cross-container proxy port:
        # sending a SET line to the proxy port silently closes (empty ack ⇒
        # ProxyHopError, blocking every proxied solve). See proxy_hop.serve().
        self._hop_control_host, self._hop_control_port = control_endpoint(hop_port)
        self._egress_read_timeout_s = egress_read_timeout_s
        # Lazy default factories live in cdp (same package) — reuse them so the
        # devtools websocket/http plumbing has a single implementation (R1).
        from android_solver.cdp import _default_http_get, _default_ws_factory

        self._ws_factory: WebSocketFactory = ws_factory or _default_ws_factory
        self._http_get: HttpGetter = http_get or _default_http_get

    def health(self) -> bool:
        """Readiness probe: redroid is reachable AND Android has finished booting.

        WR-04: ``adb connect`` is exit-0-on-failure across several platform-tools
        versions, so trusting its exit code alone reports healthy when no solve can
        possibly succeed (defeating the compose ``service_healthy`` gate). Verify
        the device actually booted (``sys.boot_completed == 1``) instead.
        """
        try:
            self._device.connect()
            return self._device.is_booted()
        except Exception:  # noqa: BLE001 — a health probe must never raise
            return False

    def solve(
        self,
        challenge_url: str,
        host: str,
        cancel: Event | None = None,
        *,
        proxy: dict[str, Any] | None = None,
    ) -> SolveResult:
        """Mint a clearance, cooperatively cancellable via ``cancel`` (issue #207).

        On cancellation the in-flight pipeline unwinds at its next adb/CDP
        checkpoint and the device is reset (``force_stop_and_clear``) on the way
        out so the NEXT solve starts on a clean WebView, not a half-driven
        challenge page.

        When ``proxy`` is supplied the WebView egress is routed through the
        per-solve authenticated hop and verified before the tap (Phase 11); when
        ``None`` today's direct-egress flow runs byte-for-byte unchanged (D-08).
        """
        try:
            return self._solve(challenge_url, host, cancel, proxy=proxy)
        except SolveCancelled:
            # AC3 (#207): a cancelled solve must leave redroid clean for the next
            # caller — reset the WebView so no wedged challenge state carries over.
            self._reset_device_quietly()
            raise

    def _reset_device_quietly(self) -> None:
        """Best-effort device reset on the cancellation path (never raises)."""
        try:
            self._device.force_stop_and_clear()
        except Exception:  # noqa: BLE001 — cleanup must not mask the cancellation
            _log.warning("device reset after cancellation failed", exc_info=True)

    def eval_in_webview(
        self,
        challenge_url: str,
        host: str,
        js: str,
        cancel: Event | None = None,
        *,
        wait_for: str | None = None,
        deadline: float | None = None,
        proxy: dict[str, Any] | None = None,
        with_clearance: bool = False,
    ) -> Any:
        """Run ``js`` in the warm cleared WebView and return its marshalled value.

        Bug 5 follow-on #3 (comix clearance via the EVAL path, never ``/solve``): when
        ``with_clearance`` is set, the eval ALSO extracts the host-scoped
        ``cf_clearance`` (+ the WebView UA + the cookie's epoch expiry) from the WebView
        cookie jar AFTER the page is cleared+hydrated, and returns
        ``(value, SolveResult | None)`` instead of the bare value. This lets the gateway
        MINT + HOLD a page-holding source's (comix's) replayable ``cf_clearance`` for
        the httpx image leg WITHOUT a ``/solve`` (whose ``pm clear`` would cold-wipe the
        warm WebView comix's managed challenge + cached ``env-*.js`` depend on). The
        eval path's warm ``Page.navigate`` + interactive-OOPIF clear leaves comix's
        managed challenge auto-issuing the cookie (it is then read by
        :func:`extract_clearance` from the same jar ``_tap_until_cleared`` reads). The
        token is NEVER logged (T-14-04). ``with_clearance`` defaults ``False`` so every
        existing comix extractor eval returns the bare value byte-for-byte unchanged.

        Re-uses the already-Turnstile-cleared WebView from the prior ``/solve`` — it
        deliberately does NOT ``force_stop_and_clear`` (that would wipe the cookie
        jar and re-trigger the challenge). Re-forwards devtools (torn down in a
        ``finally`` on EVERY exit, #275), then guarantees the WebView is on a
        CF-cleared, HYDRATED comix page BEFORE ``Runtime.evaluate``:

          * FAST PATH (spike 021, the common case — a comix ``/solve`` runs right
            before each comix eval and leaves the page cleared): when the warm WebView
            is ALREADY on a hydrated, non-challenged comix page for ``host``, eval
            DIRECTLY with NO ``Page.navigate`` and NO tap. A re-navigation reloads the
            page and re-triggers comix's MANAGED Cloudflare challenge (bug 3: the
            cf_clearance COOKIE alone does NOT pass a fresh top-level load), so the
            needless re-nav is skipped.
          * CLEAR-IF-CHALLENGED (fallback — wrong page/host, or the proxied path's
            egress-verify navigated the page away): ``Page.navigate`` the
            ``challenge_url``, and if comix re-challenges, clear the interstitial with
            the SAME Turnstile tap machinery ``/solve`` uses (``_wait_for_cf_frame`` +
            ``_compute_scales`` + ``_tap_until_cleared``) before proceeding.

        Then polls for SPA hydration (``env-*.js`` in Resource-Timing + an optional
        ``wait_for`` JS boolean predicate, returning anyway on the deadline) →
        ``Runtime.evaluate(awaitPromise:true, returnByValue:true)`` → marshal
        ``result.result.value``. ``awaitPromise:true`` is the ONE new CDP option over
        the three existing eval sites (comix's extractors are async; spike 021 A2).
        The clearance minted/observed during a clear-if-challenged is NEVER logged or
        returned (T-14-04) — the eval returns only the JS value.

        When ``proxy`` is supplied the eval navigation egresses through the SAME
        per-solve authenticated hop + egress-verify the proxied ``/solve`` uses, so
        the eval runs under the EXACT residential IP the proxied clearance was minted
        on (Req 7 parity — a different egress IP makes the cleared cookie invalid and
        CF re-challenges the eval nav). When ``None`` today's direct-egress eval runs
        byte-for-byte unchanged (D-08).

        A4 (research §6): the marshalled value (e.g. a 4,716-row chapter list) is
        NOT subject to the inbound ``_MAX_BODY_BYTES`` cap — that cap is inbound-only.
        The CDP ``ws.recv`` reassembles fragmented frames and the stdlib JSON
        response writer streams the full Content-Length body, so a large result
        round-trips intact with no transport truncation (offline-proven in the
        service tests); no in-eval pagination is required.

        ``wait_for`` is a JS boolean EXPRESSION string (NOT a CSS selector) so Plan
        03 can pass readiness predicates. The ``js`` and the marshalled value are
        NEVER logged (T-14-04).
        """
        if deadline is None:
            deadline = time.monotonic() + self._timeout_s
        # Re-use the warm cleared WebView — connect only (NO force_stop_and_clear).
        self._device.connect()
        _raise_if_cancelled(cancel)

        if proxy is None:
            # D-08: no-proxy evals run the direct-egress flow byte-for-byte unchanged
            # — no hop repoint, no global http_proxy, no egress-verify.
            return self._drive_eval(
                challenge_url,
                host,
                js,
                cancel,
                wait_for=wait_for,
                deadline=deadline,
                expected_egress_ip="",
                with_clearance=with_clearance,
            )

        # Proxied eval (Req 7 parity with ``_solve``): set the device-wide proxy and
        # ALWAYS clear it in finally — even on a mid-flight raise (mirroring
        # ``_solve``'s discipline). The hop is repointed to the per-solve
        # authenticated upstream BEFORE the eval navigation so the warm WebView picks
        # the proxy up on its next load. The credential-bearing upstream URI is never
        # logged (T-14-04 / T-11-02).
        upstream, up_host, up_port, user, pw = _proxy_parts(proxy)
        try:
            # WR-01 parity: the hop repoint AND the device-proxy set both live INSIDE
            # the try so ``finally: self._clear_proxy_quietly()`` ALWAYS idles the hop
            # + clears the device, even if either step raises an AdbError mid-flight.
            repoint(self._hop_control_host, self._hop_control_port, upstream)
            self._device.set_global_http_proxy(self._device_hop_host(), self._hop_port)
            _raise_if_cancelled(cancel)
            # Learn the egress this SAME upstream should present (stdlib self-probe,
            # D-05); the WebView's observed egress is asserted against it pre-eval.
            expected = expected_egress(up_host, up_port, user, pw)
            return self._drive_eval(
                challenge_url,
                host,
                js,
                cancel,
                wait_for=wait_for,
                deadline=deadline,
                expected_egress_ip=expected,
                with_clearance=with_clearance,
            )
        finally:
            self._clear_proxy_quietly()

    @staticmethod
    def _raise_if_eval_threw(result: dict[str, Any]) -> None:
        """Raise :class:`EvalError` if a ``Runtime.evaluate`` response shows a throw.

        ``Runtime.evaluate(awaitPromise:true)`` reports a rejected in-page promise via
        an ``exceptionDetails`` block on an otherwise-200 response (Mode-E root cause).
        The summary is built from the JS error's first description line (or its class /
        the top-level ``text``), bounded to ``_EVAL_EXC_SUMMARY_MAX`` chars — CDP does
        NOT echo the evaluated ``expression`` in ``exceptionDetails``, and the gateway's
        own extractor throws are token-free, so the summary carries neither the ``js``
        nor the marshalled value (T-14-04). A response with no ``exceptionDetails`` (the
        common case, INCLUDING a promise that resolved with an empty envelope) is a
        no-op.
        """
        details = result.get("exceptionDetails")
        if not details:
            return
        exception = details.get("exception") if isinstance(details, dict) else None
        summary = ""
        if isinstance(exception, dict):
            description = exception.get("description")
            if isinstance(description, str) and description:
                summary = description.splitlines()[0]
            elif isinstance(exception.get("className"), str):
                summary = str(exception["className"])
        if not summary and isinstance(details, dict):
            text = details.get("text")
            if isinstance(text, str):
                summary = text
        summary = (summary or "in-page promise rejected")[:_EVAL_EXC_SUMMARY_MAX]
        raise EvalError(f"in-page eval threw: {summary}")

    def _ensure_webview_alive(
        self, challenge_url: str, cancel: Event | None, deadline: float
    ) -> int:
        """Return a live ``webview_shell`` pid, relaunching the process if it died.

        Happy path: ``pidof()`` succeeds on the first call and we return immediately
        with NO ``launch_url`` and NO sleep — the warm comix page the prior eval/solve
        left alive is untouched and the caller's downstream fast path can engage.

        Recovery path: ``pidof()`` raises ``AdbError`` (toybox ``pidof`` exits 1 when
        the process is absent) → relaunch ``webview_shell`` on ``challenge_url`` via
        the same unrooted ``am start`` mechanism ``/solve`` uses, settle, then poll
        ``pidof`` until a pid appears. We deliberately do NOT ``force_stop_and_clear``
        here: that would cold-wipe the warm in-page comix env the eval needs (MEMORY
        "comix clears via eval path, never /solve"). A fresh relaunch lands on the CF
        "Just a moment…" interstitial, so the caller's ``_is_hydrated_comix_page`` fast
        path naturally returns False and the existing ``Page.navigate`` +
        ``_clear_challenge_if_present`` + ``_wait_for_hydration`` else-branch clears and
        hydrates it — this method only guarantees a live process + valid pid before
        ``forward_devtools(pid)``.

        Bounded by BOTH the eval ``deadline`` and ``_RELAUNCH_PIDOF_ATTEMPTS``; honors
        ``_raise_if_cancelled(cancel)`` and the ``deadline`` BEFORE the relaunch/settle
        and between poll attempts, and clamps every sleep to the remaining budget so a
        cancelled or expired eval never drives the device (T-10-02). adbd is NEVER
        rooted.
        """
        try:
            return self._device.pidof()
        except AdbError as initial_err:
            last_err: AdbError = initial_err
            _log.warning("webview_shell not running for eval; relaunching to self-heal")
        # Respect cancellation + the eval deadline BEFORE the (costly) relaunch and
        # its settle sleep — a request cancelled or already past deadline must not
        # keep the single eval worker driving the device.
        _raise_if_cancelled(cancel)
        if time.monotonic() >= deadline:
            raise AdbError(
                "webview_shell not running and eval deadline expired before relaunch"
            ) from last_err
        self._device.launch_url(challenge_url)
        _raise_if_cancelled(cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdbError(
                "webview_shell relaunch completed after the eval deadline"
            ) from last_err
        # Floor before the process registers a pid, never overshooting the deadline.
        time.sleep(min(self._launch_settle_s, remaining))
        for _ in range(_RELAUNCH_PIDOF_ATTEMPTS):
            _raise_if_cancelled(cancel)
            if time.monotonic() >= deadline:
                break
            try:
                return self._device.pidof()
            except AdbError as err:
                last_err = err
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(self._poll_interval_s, remaining))
        raise AdbError(
            "webview_shell did not register a pid after relaunch before the eval "
            "deadline"
        ) from last_err

    def _drive_eval(
        self,
        challenge_url: str,
        host: str,
        js: str,
        cancel: Event | None,
        *,
        wait_for: str | None,
        deadline: float,
        expected_egress_ip: str,
        with_clearance: bool = False,
    ) -> Any:
        """Forward devtools → (egress-verify) → ensure cleared+hydrated → eval.

        When ``expected_egress_ip`` is non-empty (proxied path) the WebView's observed
        egress is read in-browser and asserted to equal it BEFORE the eval runs (Req 3
        / T-11-01 parity with ``_drive_solve``); a mismatch or transient echo failure
        raises with NO eval result — a silently-bypassed proxy must NOT eval against a
        wrong-IP (re-challenged) session. When it is ``""`` (no-proxy path) the
        egress-verify is skipped (D-08). The device is already connected by the caller.

        Bug 3: comix re-challenges a fresh top-level load, so the eval does NOT blindly
        re-navigate. If the warm WebView is already on a cleared, hydrated comix page
        (the spike-021 fast path the prior /solve leaves alive) the eval runs DIRECTLY
        with no re-nav and no tap. Otherwise — wrong page/host, or the egress-verify
        navigated the page AWAY to the ip echo — it navigates to ``challenge_url`` and,
        if comix re-challenges, clears the interstitial with ``/solve``'s tap machinery
        BEFORE waiting for hydration. The clear/hydrate therefore always happens AFTER
        any egress-verify nav-away, so the eval never runs against a re-challenged page.
        """
        pid = self._ensure_webview_alive(challenge_url, cancel, deadline)
        _raise_if_cancelled(cancel)
        port = self._device.forward_devtools(pid)
        # #275: from the moment the adb/CDP forward exists, tear it down in a finally
        # on EVERY exit — success, failure, timeout, AND cancellation.
        try:
            _raise_if_cancelled(cancel)
            ws_url = self._discover_page_ws(port)
            _raise_if_cancelled(cancel)
            ws = self._ws_factory(ws_url, timeout=_WS_TIMEOUT_S)
            try:
                cdp_call(ws, "Page.enable", command_id=10)
                cdp_call(ws, "DOM.enable", command_id=11)
                navigated_away = False
                if expected_egress_ip:
                    # Req 3 / T-11-01 parity: prove the WebView egresses through the
                    # verified proxy IP BEFORE running the eval. Fix A (bug 4 cause #1):
                    # verify NON-DESTRUCTIVELY first — an IN-PAGE fetch of the IP echo
                    # (NO Page.navigate), so the cleared+hydrated comix page survives
                    # and the bug-3 fast path below can engage (NO per-eval re-clear).
                    # Only when the in-page probe is unavailable (CSP/CORS/transient ⇒
                    # ``None``) do we FALL BACK to the destructive nav-away probe — an
                    # eval is NEVER run unverified, and the worst case is today's
                    # behaviour (re-nav + clear). The byte-equal assertion runs on EVERY
                    # eval whichever transport observed the egress.
                    observed = self._observe_webview_egress_in_page(ws, cancel)
                    if observed is None:
                        # Probe unavailable — the destructive verify navigates the page
                        # AWAY to the ip echo, so the fast path can no longer engage and
                        # the eval must re-establish the cleared+hydrated comix page.
                        observed = self._observe_webview_egress(ws, cancel)
                        navigated_away = True
                    if observed != expected_egress_ip:
                        # WR-02 parity: byte-equal egress assertion REQUIRES a STICKY
                        # upstream that holds one exit IP across the self-probe CONNECT
                        # and the WebView's separate connection. A ROTATING residential
                        # proxy mismatches on nearly every call — name the cause so it
                        # is not mistaken for a Cloudflare failure.
                        raise self._egress_mismatch_error(expected_egress_ip, observed)
                    _log.info(
                        "verified proxied WebView egress %s for eval%s",
                        observed,
                        "" if navigated_away else " (in-page)",
                    )
                # Bug 3: a fresh Page.navigate reloads the page and re-triggers comix's
                # MANAGED Cloudflare challenge (the cf_clearance COOKIE alone does NOT
                # pass a top-level reload), and /eval — unlike /solve — never taps to
                # clear it, so a blind re-nav left every comix eval running against the
                # "Just a moment..." interstitial (empty result). FAST PATH: when the
                # warm WebView is ALREADY on the cleared, hydrated comix page the prior
                # /solve left alive, eval directly with NO re-nav and NO tap.
                if not navigated_away and self._is_hydrated_comix_page(ws, host):
                    self._wait_for_hydration(ws, wait_for, cancel, deadline)
                else:
                    # Re-navigation required (wrong page/host, or the egress-verify
                    # navigated the page away). comix re-challenges the fresh load,
                    # so clear it with /solve's tap machinery before hydrating.
                    cdp_call(
                        ws,
                        "Page.navigate",
                        {"url": challenge_url},
                        command_id=_EVAL_NAV_CMD_ID,
                    )
                    self._clear_challenge_if_present(ws, ws_url, host, cancel, deadline)
                    self._wait_for_hydration(ws, wait_for, cancel, deadline)
                _raise_if_cancelled(cancel)
                # awaitPromise:true is the ONLY new option over the existing eval
                # sites — comix's extractors wrap an async ``import()`` IIFE.
                result = cdp_call(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": js,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    command_id=_EVAL_CMD_ID,
                )
                # Mode-E (debug comix-warm-hydration-wait): a REJECTED in-page promise
                # comes back as a successful CDP response carrying ``exceptionDetails``
                # and NO ``result.value``. Detect it and RAISE — otherwise the absent
                # value marshals to ``None`` and the gateway silently coerces it to an
                # empty (warning-less, sticky-cached) zero-result search. A throw is a
                # transient failure, not a genuine empty.
                self._raise_if_eval_threw(result)
                value = result.get("result", {}).get("value")
                if not with_clearance:
                    return value
                # Bug 5 follow-on #3: the page is now cleared+hydrated, so comix's
                # managed challenge has auto-issued the host-scoped cf_clearance into
                # this WebView's cookie jar — read it back (+ the bound UA) and return
                # it alongside the value so the gateway can MINT + HOLD comix's
                # replayable clearance with NO /solve. The forward + ws are still alive
                # here (their teardown is the finally blocks below). Token NEVER logged.
                clearance = self._extract_clearance_after_eval(
                    ws_url, host, port, expected_egress_ip
                )
                return (value, clearance)
            finally:
                ws.close()
        finally:
            self._remove_forward_quietly(port)

    def _extract_clearance_after_eval(
        self,
        ws_url: str,
        host: str,
        port: int,
        expected_egress_ip: str,
    ) -> SolveResult | None:
        """Read the host-scoped ``cf_clearance`` from the WebView jar after clear+eval.

        Bug 5 follow-on #3: the eval path's warm ``Page.navigate`` + interactive-OOPIF
        clear leaves comix's managed challenge auto-issuing a host-scoped
        ``cf_clearance`` (the SAME cookie ``_tap_until_cleared`` reads on the solve
        path), so a page-holding source can mint a replayable clearance for the httpx
        image leg WITHOUT the destructive ``/solve`` (``pm clear``). Returns ``None``
        (NOT a raise) when no
        host-scoped cookie is present yet or a transient CDP hiccup hits — the gateway
        treats a missing clearance as "mint not ready" and retries on the next call. The
        UA is fetched exactly as ``_drive_solve`` does (a hiccup ⇒ empty UA, never a
        discard). The token value is NEVER logged (T-14-04)."""
        try:
            minted = extract_clearance(ws_url, host)
        except Exception:  # noqa: BLE001 — a post-eval cookie read must not abort the eval
            _log.warning(
                "post-eval clearance extract failed transiently for host %s; "
                "returning no clearance",
                host,
                exc_info=True,
            )
            return None
        if minted is None:
            return None
        try:
            user_agent = (
                webview_user_agent(
                    f"http://localhost:{port}/json/version",
                    http_get=self._http_get,
                )
                or ""
            )
        except Exception:  # noqa: BLE001 — a UA hiccup must not discard a minted token
            _log.warning(
                "webview UA fetch failed after eval clearance extract; using empty UA",
                exc_info=True,
            )
            user_agent = ""
        return SolveResult(
            cf_clearance=minted.value,
            user_agent=user_agent,
            host=host,
            egress_ip=expected_egress_ip,
            cf_clearance_expires=minted.expires,
        )

    def _is_hydrated_comix_page(self, ws: WebSocketLike, host: str) -> bool:
        """True when the warm WebView is ALREADY on a cleared, hydrated ``host`` page.

        The spike-021 fast path: the prior ``/solve`` leaves the WebView alive on the
        cleared comix page (``env-*.js`` in Resource-Timing, NO CF interstitial). When
        ALL three hold — current ``location.host`` == ``host`` AND ``env-*.js`` present
        AND NOT a CF interstitial — the eval runs with NO ``Page.navigate`` and NO tap,
        avoiding the reload that re-triggers comix's managed challenge (bug 3). Any
        probe failure ⇒ ``False`` (take the safe re-nav + clear-if-challenged path).
        """
        try:
            result = cdp_call(
                ws,
                "Runtime.evaluate",
                {"expression": "location.host", "returnByValue": True},
                command_id=_EVAL_LOCATION_CMD_ID,
            )
        except Exception:  # noqa: BLE001 — a probe error ⇒ take the safe re-nav path
            return False
        current = result.get("result", {}).get("value")
        if not isinstance(current, str) or current.lower() != host.lower():
            return False
        if self._cf_frame_present(ws):
            return False
        return self._eval_bool(ws, _EVAL_HYDRATION_JS, _EVAL_HYDRATION_CMD_ID)

    def _cf_frame_present(self, ws: WebSocketLike) -> bool:
        """True when the page is a CF interstitial — the ``challenges.cloudflare.com``
        OOPIF is in the frame tree (the SAME ``_CF_FRAME_URL_MARKER`` /
        ``_frame_tree_has_cf`` the solve path's ``_wait_for_cf_frame`` uses, not a new
        detector). Any CDP error is treated as 'not a CF interstitial' (fail-open to
        the safe re-nav path; never a token-bearing decision).
        """
        try:
            tree = cdp_call(ws, "Page.getFrameTree", command_id=_EVAL_FRAME_TREE_CMD_ID)
        except Exception:  # noqa: BLE001 — a probe error must not abort the eval
            return False
        return self._frame_tree_has_cf(tree.get("frameTree"))

    def _await_cf_challenge_or_clean(
        self,
        ws: WebSocketLike,
        cancel: Event | None,
        *,
        is_clean: Callable[[], bool],
    ) -> bool:
        """Poll a freshly-navigated page until it resolves, returning WHICH state:

          * ``True``  — a Cloudflare challenge interstitial appeared (the
            ``challenges.cloudflare.com`` OOPIF is in the frame tree) and must be
            tapped to clear.
          * ``False`` — the page came up CLEAN (``is_clean()`` reports the target is
            already in its terminal good state), so there is NOTHING to clear.

        ONE clean-vs-challenge resolver shared by BOTH the eval path and the solve
        path (DRY — no per-path copy of the poll-to-deadline loop to lurk and rot).
        The clean signal differs per path, so it is injected as ``is_clean``:

          * eval path (``_clear_challenge_if_present``): clean = a hydrated,
            non-challenged ``host`` comix page (``_is_hydrated_comix_page``).
          * solve path (``_wait_for_cf_frame``): clean = a host-scoped ``cf_clearance``
            is ALREADY present (``_host_already_cleared``) — host-agnostic, so it serves
            every solve host (mangadot/kagane/mangaball/comix), not just comix.

        Bug 5 / collision + solve-path parity: when the navigated page is already
        cleared (a warm ``cf_clearance`` still passed the load, or CF's managed
        challenge auto-issued clearance with no interactive widget), the CF OOPIF
        NEVER appears. The old per-path waits blocked the FULL ``_frame_poll_timeout_s``
        (~20s) on every such clean load (the "cloudflare OOPIF did not appear before
        frame-poll deadline" warning), blowing the per-source / startup-warm budget.
        This poll short-circuits the instant ``is_clean()`` holds — only spending the
        long deadline when a challenge genuinely renders. The CF frame is checked FIRST
        each iteration so a real interstitial is never mistaken for a clean page (the
        genuine-challenge path is unchanged).
        """
        deadline = time.monotonic() + self._frame_poll_timeout_s
        while time.monotonic() < deadline:
            _raise_if_cancelled(cancel)  # issue #207: don't poll past a cancel
            if self._cf_frame_present(ws):
                return True
            if is_clean():
                return False
            time.sleep(self._frame_poll_interval_s)
        # Neither a challenge frame nor a confirmed-clean page within the deadline —
        # fall back to a final CF-frame read (preserving the old behaviour: clear a
        # challenge if one is present, else there is nothing to clear).
        _log.warning("cloudflare OOPIF did not appear before frame-poll deadline")
        return self._cf_frame_present(ws)

    def _clear_challenge_if_present(
        self,
        ws: WebSocketLike,
        ws_url: str,
        host: str,
        cancel: Event | None,
        deadline: float,
    ) -> None:
        """Clear the CF interstitial after a fresh eval nav, if comix re-challenged.

        comix re-challenges a fresh top-level load (the cf_clearance COOKIE alone does
        NOT pass a reload — bug 3), and on the proxied path the egress-verify navigates
        the page away, so a re-nav may be freshly challenged. Poll for the page to
        resolve to ONE of two terminal states (``_await_cf_challenge_or_clean``): a CF
        interstitial — drive the SAME tap machinery ``/solve`` uses (``_compute_scales``
        + ``_tap_until_cleared`` — NOT a forked implementation) to clear it — or a CLEAN
        hydrated comix page (the warm clearance still passed the nav), in which case
        there is nothing to clear.

        Bug 5 collision-recovery: a clean re-nav short-circuits the instant the page is
        hydrated-and-CF-free, so it no longer blocks the full ~20s frame-poll deadline
        waiting for a CF OOPIF that will never appear. The clearance minted/observed
        here is NEVER logged or returned (T-14-04) — clearing is the only goal; the eval
        returns just the JS value.
        """
        if not self._await_cf_challenge_or_clean(
            ws, cancel, is_clean=lambda: self._is_hydrated_comix_page(ws, host)
        ):
            return
        x_scale, y_scale = self._compute_scales(ws)
        minted = self._tap_until_cleared(
            ws, ws_url, host, x_scale, y_scale, deadline, cancel
        )
        if minted is None:
            raise SolveError(
                f"could not clear CF challenge for {host} before the eval deadline"
            )

    def _wait_for_hydration(
        self,
        ws: WebSocketLike,
        wait_for: str | None,
        cancel: Event | None,
        deadline: float,
    ) -> None:
        """Poll until the comix SPA has hydrated, bounded by ``deadline``.

        Readiness = ``env-*.js`` present in the Resource-Timing buffer (the SPA-ready
        signal, spike 021 A1) AND, when ``wait_for`` is supplied, that JS boolean
        predicate is truthy. Returns anyway on the deadline (does NOT hard-fail) —
        comix routes by hid and the slug is cosmetic, so a cosmetic-readiness miss
        must not abort an otherwise-runnable eval (spike 021). The wait is also
        bounded by ``_EVAL_HYDRATION_TIMEOUT_S`` so it never consumes the whole eval
        budget waiting on a signal that will not appear.
        """
        hydration_deadline = min(deadline, time.monotonic() + _EVAL_HYDRATION_TIMEOUT_S)
        while time.monotonic() < hydration_deadline:
            _raise_if_cancelled(cancel)
            ready = self._eval_bool(ws, _EVAL_HYDRATION_JS, _EVAL_HYDRATION_CMD_ID)
            if ready and (
                wait_for is None or self._eval_bool(ws, wait_for, _EVAL_WAIT_FOR_CMD_ID)
            ):
                return
            time.sleep(self._poll_interval_s)
        _log.warning(
            "comix SPA hydration signal not seen before eval deadline; "
            "running the eval anyway"
        )

    def _eval_bool(self, ws: WebSocketLike, expression: str, command_id: int) -> bool:
        """Evaluate a JS boolean predicate; any CDP/eval error is treated as False.

        Fail-open like ``_in_verification`` — a transient CDP hiccup on a readiness
        probe must never abort the eval; it just means "not ready yet, keep polling".
        """
        try:
            result = cdp_call(
                ws,
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                command_id=command_id,
            )
        except Exception:  # noqa: BLE001 — a readiness probe must never abort the eval
            return False
        return bool(result.get("result", {}).get("value"))

    def _solve(
        self,
        challenge_url: str,
        host: str,
        cancel: Event | None,
        proxy: dict[str, Any] | None = None,
    ) -> SolveResult:
        self._device.connect()
        _raise_if_cancelled(cancel)
        self._device.force_stop_and_clear()
        _raise_if_cancelled(cancel)

        if proxy is None:
            # D-08: no-proxy solves run the direct-egress flow byte-for-byte
            # unchanged — no hop repoint, no global http_proxy, no egress-verify.
            return self._drive_solve(challenge_url, host, cancel, expected_egress_ip="")

        # Proxied solve (Req 2/3/5): set the device-wide proxy under the serialized
        # service Lock and ALWAYS clear it in finally — even on a mid-flight raise
        # (Req 5, mirroring the #207 cancel-drain discipline). The hop is repointed
        # to the per-solve authenticated upstream BEFORE the WebView restarts so it
        # picks the proxy up on launch (D-02). The credential-bearing upstream URI
        # is never logged (T-10-04 / T-11-02).
        upstream, up_host, up_port, user, pw = _proxy_parts(proxy)
        try:
            # WR-01: the hop repoint AND the device-proxy set both live INSIDE the
            # try so the ``finally: self._clear_proxy_quietly()`` ALWAYS idles the
            # hop + clears the device — even if ``set_global_http_proxy`` (or the
            # repoint itself) raises an AdbError mid-flight. Hoisting either above
            # the try would leak credentialed hop state on that raise: the hop
            # would stay repointed at the authenticated upstream (its
            # ``pproxy.Server`` holds ``user:pass``) until the next proxied solve.
            repoint(self._hop_control_host, self._hop_control_port, upstream)
            # The DEVICE (redroid Android) must reach the hop by IP, not by the
            # docker service name: redroid's Android resolver is hardcoded to
            # 8.8.8.8 and does NOT consult docker's embedded DNS, so ``settings put
            # global http_proxy android-solver:<port>`` yields
            # ERR_PROXY_CONNECTION_FAILED ("unknown host"). The sidecar DOES resolve
            # its own docker name, so resolve it here to the on-network IP the
            # WebView can actually connect to (Pitfall 1).
            self._device.set_global_http_proxy(self._device_hop_host(), self._hop_port)
            _raise_if_cancelled(cancel)
            # Learn the egress this SAME upstream should present (stdlib self-probe,
            # D-05); the WebView's observed egress is asserted against it pre-tap.
            expected = expected_egress(up_host, up_port, user, pw)
            return self._drive_solve(
                challenge_url, host, cancel, expected_egress_ip=expected
            )
        finally:
            self._clear_proxy_quietly()

    def _drive_solve(
        self,
        challenge_url: str,
        host: str,
        cancel: Event | None,
        *,
        expected_egress_ip: str,
    ) -> SolveResult:
        """Launch → CDP → (egress-verify when proxied) → tap → extract.

        When ``expected_egress_ip`` is non-empty (proxied path) the WebView's
        observed egress is read in-browser and asserted to equal it BEFORE the tap
        (Req 3); a mismatch or transient echo failure raises with NO token. When it
        is ``""`` (no-proxy path) the egress-verify is skipped and this flow is
        byte-for-byte today's direct-egress solve (D-08).
        """
        self._device.launch_url(challenge_url)
        _raise_if_cancelled(cancel)
        time.sleep(self._launch_settle_s)  # floor before the devtools socket is up
        _raise_if_cancelled(cancel)

        pid = self._device.pidof()
        _raise_if_cancelled(cancel)
        port = self._device.forward_devtools(pid)
        # #275 req 3: from the moment the adb/CDP forward exists, tear it down in a
        # finally on EVERY exit — success, failure, timeout, AND cancellation — so no
        # ESTABLISHED devtools forward leaks across solves.
        try:
            _raise_if_cancelled(cancel)
            ws_url = self._discover_page_ws(port)
            _raise_if_cancelled(cancel)

            # Bound the whole locate→tap→poll loop by the solve deadline (T-10-11).
            deadline = time.monotonic() + self._timeout_s
            ws = self._ws_factory(ws_url, timeout=_WS_TIMEOUT_S)
            try:
                cdp_call(ws, "Page.enable", command_id=10)
                cdp_call(ws, "DOM.enable", command_id=11)
                if expected_egress_ip:
                    # Req 3 / T-11-01: prove the WebView egresses through the verified
                    # proxy IP BEFORE tapping — a silently-bypassed proxy must NOT mint
                    # a wrong-IP clearance. Mismatch/transient echo failure ⇒ no token.
                    observed = self._observe_webview_egress(ws, cancel)
                    if observed != expected_egress_ip:
                        # WR-02: the egress-verify gate asserts the self-probe IP and
                        # the WebView IP are byte-equal (T-11-01 — the minted
                        # cf_clearance MUST bind to the EXACT egress). This REQUIRES a
                        # STICKY upstream that holds one exit IP across both the
                        # self-probe CONNECT and the WebView's separate connection. A
                        # ROTATING residential proxy (exit IP changes per TCP
                        # connection) will mismatch on nearly every solve, so the
                        # operator MUST configure a sticky-session upstream. The error
                        # below names the rotating-proxy cause explicitly so a config
                        # mismatch is not mistaken for a Cloudflare failure.
                        raise SolveError(
                            f"proxied egress IP differed "
                            f"(expected {expected_egress_ip}, observed {observed}) — "
                            f"is the upstream a rotating/non-sticky proxy? "
                            f"egress-verify needs a sticky-session upstream that holds "
                            f"one exit IP across connections"
                        )
                    _log.info("verified proxied WebView egress %s", observed)
                    # The echo read navigated away; return to the challenge under the
                    # now-verified egress before locating the Turnstile widget.
                    cdp_call(
                        ws,
                        "Page.navigate",
                        {"url": challenge_url},
                        command_id=_CHALLENGE_RENAV_CMD_ID,
                    )
                # Wait for the page to resolve — the cross-origin Cloudflare OOPIF
                # renders (a few seconds after load) OR the host is already cleared (a
                # warm/auto-issued cf_clearance, no widget). Short-circuits on either,
                # never burning the full frame-poll deadline on an already-cleared load
                # (bug 5 solve-path parity). The real readiness gate, not the settle.
                self._wait_for_cf_frame(ws, ws_url, host, cancel)
                # Page ws, DOM/Page enable, frame readiness, and the viewport scales
                # are computed ONCE; only locate+tap+poll repeats inside the loop.
                x_scale, y_scale = self._compute_scales(ws)
                minted = self._tap_until_cleared(
                    ws, ws_url, host, x_scale, y_scale, deadline, cancel
                )
            finally:
                ws.close()
            if minted is None:
                raise SolveError(f"clearance not minted for {host} before deadline")
            token, expires = minted.value, minted.expires

            # IN-01: use the INJECTED getter (so tests/proxy injection apply), and
            # guard the fetch — a trivial /json/version hiccup must not discard an
            # already-minted, ~60s-expensive token (fall back to an empty UA). The
            # forward is still alive here (its teardown is the outer finally below).
            try:
                user_agent = (
                    webview_user_agent(
                        f"http://localhost:{port}/json/version",
                        http_get=self._http_get,
                    )
                    or ""
                )
            except Exception:  # noqa: BLE001 — a UA hiccup must not discard a minted token
                _log.warning(
                    "webview UA fetch failed after token mint; using empty UA",
                    exc_info=True,
                )
                user_agent = ""
            return SolveResult(
                cf_clearance=token,
                user_agent=user_agent,
                cf_clearance_expires=expires,
                host=host,
                egress_ip=expected_egress_ip,
            )
        finally:
            self._remove_forward_quietly(port)

    def _remove_forward_quietly(self, port: int) -> None:
        """Best-effort devtools-forward teardown (#275): logs, never masks the solve
        outcome — same discipline as ``_clear_proxy_quietly`` / ``_reset_device``."""
        try:
            self._device.remove_forward(port)
        except Exception:  # noqa: BLE001 — teardown must not mask the solve outcome
            _log.warning("remove_forward failed in teardown", exc_info=True)

    def _observe_webview_egress(self, ws: WebSocketLike, cancel: Event | None) -> str:
        """Navigate the WebView to the IP echo and return its observed egress IP.

        Uses the existing CDP channel (``Page.navigate`` → ``Runtime.evaluate``
        ``document.body.innerText`` returnByValue), normalizing the body with
        ``ipaddress.ip_address``. A failed navigate, a transient eval error, or a
        non-IP body is NEVER a pass — it raises :class:`SolveError` (T-11-01). The
        observed IP is loggable; no token is involved here.
        """
        try:
            cdp_call(
                ws,
                "Page.navigate",
                {"url": _EGRESS_ECHO_URL},
                command_id=_EGRESS_NAV_CMD_ID,
            )
        except Exception as exc:  # noqa: BLE001 — any nav failure ⇒ not a pass
            raise SolveError(f"egress echo navigation failed: {exc}") from exc

        deadline = time.monotonic() + self._egress_read_timeout_s
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            _raise_if_cancelled(cancel)
            time.sleep(self._poll_interval_s)  # settle for the echo page to load
            try:
                result = cdp_call(
                    ws,
                    "Runtime.evaluate",
                    {"expression": _EGRESS_READ_JS, "returnByValue": True},
                    command_id=_EGRESS_READ_CMD_ID,
                )
                value = result.get("result", {}).get("value")
                return str(ipaddress.ip_address(str(value).strip()))
            except SolveCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 — retry until the load settles
                last_err = exc
                continue
        raise SolveError(f"could not read a valid WebView egress IP: {last_err}")

    def _observe_webview_egress_in_page(
        self, ws: WebSocketLike, cancel: Event | None
    ) -> str | None:
        """Read the WebView egress via an IN-PAGE ``fetch`` (no nav-away); Fix A.

        The non-destructive counterpart to :meth:`_observe_webview_egress`: it runs
        ``_EGRESS_IN_PAGE_JS`` (an async IIFE that ``fetch``es the SAME
        ``_EGRESS_ECHO_URL`` with ``cache:'no-store'``, ``awaitPromise:true``) in the
        CURRENT page instead of navigating away, so the cleared+hydrated comix page
        survives and the bug-3 fast path can engage. Returns the normalized egress IP
        on success, or ``None`` when the probe is unavailable — the IIFE returned
        ``null`` (CSP/CORS/network block in comix's origin), the body is not an IP, or
        the CDP call itself errored — so the caller can FALL BACK to the destructive
        probe rather than run an eval unverified. The observed IP is loggable; no token
        is involved here.
        """
        _raise_if_cancelled(cancel)
        try:
            result = cdp_call(
                ws,
                "Runtime.evaluate",
                {
                    "expression": _EGRESS_IN_PAGE_JS,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                command_id=_EGRESS_IN_PAGE_CMD_ID,
            )
        except SolveCancelled:
            raise
        except Exception:  # noqa: BLE001 — a probe error ⇒ unavailable, fall back
            return None
        value = result.get("result", {}).get("value")
        if value is None:
            return None
        try:
            return str(ipaddress.ip_address(str(value).strip()))
        except ValueError:
            return None

    @staticmethod
    def _egress_mismatch_error(expected: str, observed: str | None) -> SolveError:
        """The SAME rotating/non-sticky-proxy ``SolveError`` both egress paths raise.

        Shared by the in-page and destructive-fallback verify in ``_drive_eval`` so a
        mismatch surfaces an identical, byte-equal T-11-01 failure whichever transport
        observed the egress (the message names the rotating-proxy cause so a config
        mismatch is not mistaken for a Cloudflare failure).
        """
        return SolveError(
            f"proxied egress IP differed "
            f"(expected {expected}, observed {observed}) — "
            f"is the upstream a rotating/non-sticky proxy? "
            f"egress-verify needs a sticky-session upstream that holds "
            f"one exit IP across connections"
        )

    def _device_hop_host(self) -> str:
        """The hop host the DEVICE routes ``global http_proxy`` through, as an IP.

        redroid's Android resolver is hardcoded to public DNS (8.8.8.8) and does
        NOT consult docker's embedded DNS, so a docker SERVICE NAME (the default
        ``hop_host = "android-solver"``) is an "unknown host" to the WebView. The
        sidecar, however, DOES resolve its own docker name, so we resolve
        ``hop_host`` to the on-network IP the device can reach (verified live:
        ``ping android-solver`` fails inside Android while ``ping <ip>`` succeeds).
        If ``hop_host`` is already an IP literal this is a no-op; if resolution
        fails we fall back to the literal name and let the egress-verify backstop
        surface the bad route (never minting a wrong-IP token).
        """
        host = self._hop_host
        try:
            return socket.gethostbyname(host)
        except OSError:
            _log.warning("could not resolve hop host %r to an IP; using as-is", host)
            return host

    def _clear_proxy_quietly(self) -> None:
        """ALWAYS-run proxy teardown (Req 5): clear global http_proxy + hop IDLE.

        Runs in the proxied solve's ``finally`` even when the solve raised
        mid-flight, so the device is never left with a residual ``global
        http_proxy`` and the hop returns to DIRECT. Neither step is allowed to mask
        the original error — both are best-effort and only log on failure.
        """
        try:
            self._device.clear_global_http_proxy()
        except Exception:  # noqa: BLE001 — teardown must not mask the solve outcome
            _log.warning("clear_global_http_proxy failed in teardown", exc_info=True)
        try:
            repoint(self._hop_control_host, self._hop_control_port, None)
        except Exception:  # noqa: BLE001 — teardown must not mask the solve outcome
            _log.warning("hop idle-repoint failed during teardown", exc_info=True)

    def _tap_until_cleared(
        self,
        ws: WebSocketLike,
        ws_url: str,
        host: str,
        x_scale: float,
        y_scale: float,
        deadline: float,
        cancel: Event | None = None,
    ) -> ClearanceCookie | None:
        """Tap the Turnstile checkbox once it is interactive, then let it verify.

        On a COLD WebView the ``challenges.cloudflare.com`` OOPIF frame URL appears
        BEFORE the checkbox inside it is interactive, so the first tap can land on a
        not-yet-ready widget and miss. We therefore re-locate + re-tap — but a naive
        5s re-tap loop fires a SECOND tap during Turnstile's post-tap
        "Verification successful, waiting for <host> to respond" window, which
        RESETS the widget and aborts the solve (the observed failure: two taps
        ~7s apart, then the widget vanishes with no clearance).

        Each poll cycle therefore:
          1. checks for the clearance first (a registered tap may have minted it);
          2. if the success/verification banner is showing (``_in_verification``),
             does NOTHING but wait — never tapping during verification;
          3. otherwise (re-)taps the located checkbox, but never within
             ``_retap_interval_s`` of the previous tap so a registered tap has time
             to surface its success banner before another tap is even considered.

        Together these mean: the tap naturally lands the moment the checkbox is
        interactive (cold-start race handled, bounded by ``deadline``), and once a
        tap registers it is NEVER disrupted by a follow-up tap.
        """
        minted: ClearanceCookie | None = None
        last_tap: float | None = None
        while time.monotonic() < deadline:
            # issue #207: abort the (longest) loop promptly on a post-timeout
            # cancel so the device frees without waiting out the full deadline.
            _raise_if_cancelled(cancel)
            # WR-03: a transient extract failure in ONE poll cycle (CDP hiccup, a
            # refused 2nd concurrent socket, a ws timeout) must NOT abort an
            # in-progress ~60s solve — treat it as "no token yet" and continue,
            # mirroring the deliberately fail-open _in_verification probe below.
            try:
                minted = extract_clearance(ws_url, host)
            except Exception:  # noqa: BLE001 — a transient cookie poll must not abort the solve
                _log.warning(
                    "clearance poll failed transiently; continuing", exc_info=True
                )
                minted = None
            if minted:
                return minted

            if self._in_verification(ws):
                # Post-tap verification underway — tapping now resets the widget.
                time.sleep(self._poll_interval_s)
                continue

            now = time.monotonic()
            if last_tap is None or (now - last_tap) >= self._retap_interval_s:
                coords = locate_checkbox(
                    ws,
                    screencap=self._device.screencap(),
                    x_scale=x_scale,
                    y_scale=y_scale,
                )
                if coords is not None:
                    self._device.input_tap(*coords)
                    last_tap = time.monotonic()
            time.sleep(self._poll_interval_s)
        return minted

    def _in_verification(self, ws: WebSocketLike) -> bool:
        """True while Turnstile's post-tap success/verification banner is showing.

        Reads the TOP frame for the ``#challenge-success-text`` banner Cloudflare
        renders during "Verification successful, waiting for <host> to respond".
        While that is up the checkbox must NOT be re-tapped. Any CDP/eval failure
        is treated as "not verifying" so the loop falls back to its tap behaviour
        (fail-open: better to risk a tap than to stall forever on a probe error).
        """
        try:
            result = cdp_call(
                ws,
                "Runtime.evaluate",
                {"expression": _VERIFY_PROBE_JS, "returnByValue": True},
                command_id=_VERIFY_PROBE_CMD_ID,
            )
        except Exception:  # noqa: BLE001 — a readiness probe must never abort the solve
            return False
        return bool(result.get("result", {}).get("value"))

    def _host_already_cleared(self, ws_url: str, host: str) -> bool:
        """Host-agnostic clean check for the solve path: True when a host-scoped
        ``cf_clearance`` is ALREADY present.

        The solve serves EVERY allowlisted host, so unlike the comix-specific
        ``_is_hydrated_comix_page`` this asks the host-agnostic question the solve
        actually cares about: "is there anything left to clear?". A present
        host-scoped ``cf_clearance`` means the load auto-passed CF's managed challenge
        (no interactive Turnstile rendered) or a warm clearance still held — either
        way the solve is effectively done and ``_tap_until_cleared`` will read the SAME
        cookie on its first poll. This mirrors how ``_drive_solve`` confirms success
        today (``extract_clearance``). Any extract error ⇒ ``False`` (keep polling /
        fall through to the long deadline + tap path; never a token-bearing decision).
        """
        try:
            return extract_clearance(ws_url, host) is not None
        except Exception:  # noqa: BLE001 — a clean-check probe must not abort the solve
            return False

    def _wait_for_cf_frame(
        self,
        ws: WebSocketLike,
        ws_url: str,
        host: str,
        cancel: Event | None = None,
    ) -> None:
        """Wait until the page resolves to ONE of two states before tapping.

        The Turnstile widget renders in a cross-origin ``challenges.cloudflare.com``
        child frame a few seconds after the page loads. The solve polls until EITHER
        that OOPIF appears (a genuine challenge — return so ``_tap_until_cleared`` can
        clear it, the unchanged cold-solve behaviour) OR ``host`` is already cleared (a
        host-scoped ``cf_clearance`` is present — the load auto-passed CF's managed
        challenge with no widget, or a warm clearance still held — so there is nothing
        to tap and ``_tap_until_cleared`` reads the existing cookie immediately).

        Bug 5 / solve-path parity with the eval path: a clean already-cleared load used
        to burn the FULL ``_frame_poll_timeout_s`` (~20s) here waiting for an OOPIF that
        would NEVER appear ("cloudflare OOPIF did not appear before frame-poll
        deadline"), which on a gateway-restart ``warm()`` re-solve of an already-cleared
        host blew the startup-warm gate. This now short-circuits via the SHARED
        ``_await_cf_challenge_or_clean`` resolver the instant either state is reached;
        the genuine-challenge path is detected FIRST and is byte-for-byte unchanged.
        Returns either way so ``_tap_until_cleared`` (which can still try the
        secondary/screenshot locate paths) always runs next.
        """
        self._await_cf_challenge_or_clean(
            ws, cancel, is_clean=lambda: self._host_already_cleared(ws_url, host)
        )

    @staticmethod
    def _frame_tree_has_cf(frame_tree: Any) -> bool:
        if not isinstance(frame_tree, dict):
            return False
        frame = frame_tree.get("frame", {})
        url = frame.get("url", "") if isinstance(frame, dict) else ""
        if isinstance(url, str) and _CF_FRAME_URL_MARKER in url:
            return True
        return any(
            AndroidSolvePipeline._frame_tree_has_cf(child)
            for child in frame_tree.get("childFrames", []) or []
        )

    def _compute_scales(self, ws: WebSocketLike) -> tuple[float, float]:
        """CSS→physical per-axis scales: screen px / live viewport px.

        x_scale = screen_width / window.innerWidth, y_scale likewise for height.
        The vertical factor differs (webview_shell's URL bar shrinks innerHeight),
        so the two are computed independently. Falls back to (1.0, 1.0) on any
        missing/zero value (never logs a token).
        """
        try:
            screen_w, screen_h = self._device.screen_size()
            result = cdp_call(
                ws,
                "Runtime.evaluate",
                {
                    "expression": "[window.innerWidth, window.innerHeight]",
                    "returnByValue": True,
                },
                command_id=30,
            )
            value = result.get("result", {}).get("value")
            view_w = float(value[0])
            view_h = float(value[1])
        except (AdbError, KeyError, IndexError, TypeError, ValueError) as exc:
            _log.warning(
                "could not compute viewport scales (%s); using 1.0",
                type(exc).__name__,
            )
            return (1.0, 1.0)
        if view_w <= 0 or view_h <= 0:
            _log.warning(
                "non-positive viewport (%r, %r); using scale 1.0", view_w, view_h
            )
            return (1.0, 1.0)
        return (screen_w / view_w, screen_h / view_h)

    def _discover_page_ws(self, port: int) -> str:
        payload = self._http_get(
            f"http://localhost:{port}/json", timeout=_HTTP_TIMEOUT_S
        )
        targets = json.loads(payload)
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return str(target["webSocketDebuggerUrl"])
        for target in targets:  # fall back to any target exposing a ws url
            if target.get("webSocketDebuggerUrl"):
                return str(target["webSocketDebuggerUrl"])
        raise SolveError("no CDP page target exposes a websocket debugger url")


# ── the control service: auth + allowlist + serialized/timeout-bounded solve ──


class SolverService:
    """Stateless-per-request control logic, independent of the HTTP transport.

    Tested directly (no socket needed): ``solve()`` and ``healthz()`` return a
    ``(status_code, json_body)`` pair the HTTP handler just serializes.
    """

    def __init__(
        self,
        config: SidecarConfig,
        pipeline: SolvePipeline,
        *,
        executor: Executor | None = None,
    ) -> None:
        self._config = config
        self._pipeline = pipeline
        # One WebView solve at a time (T-10-11): the lock serializes callers and
        # the single-worker executor bounds each solve by wall-clock timeout.
        self._lock = Lock()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="android-solve"
        )

    def _authenticate(self, provided_key: str | None) -> bool:
        if not provided_key:
            return False
        return hmac.compare_digest(provided_key, self._config.api_key)

    def authenticate(self, provided_key: str | None) -> bool:
        """Constant-time ``X-Solver-Key`` check for the transport-layer pre-body
        gate (CR-01): the HTTP handler authenticates BEFORE reading the body so an
        unauthenticated caller can never force a large/slow read."""
        return self._authenticate(provided_key)

    def healthz(self) -> tuple[int, dict[str, Any]]:
        ok = self._pipeline.health()
        status = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
        return int(status), {"status": "ok" if ok else "unavailable"}

    def solve(
        self,
        *,
        api_key: str | None,
        body: bytes,
        disconnected: Callable[[], bool] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not self._authenticate(api_key):
            # T-10-08: no device action without a valid key.
            return int(HTTPStatus.UNAUTHORIZED), {
                "error": "invalid or missing X-Solver-Key"
            }

        try:
            payload = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return int(HTTPStatus.BAD_REQUEST), {"error": "malformed JSON body"}
        if not isinstance(payload, dict):
            return int(HTTPStatus.BAD_REQUEST), {"error": "body must be a JSON object"}

        challenge_url = payload.get("challenge_url")
        if not isinstance(challenge_url, str) or not challenge_url:
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "challenge_url is required"
            }

        split = urlsplit(challenge_url)
        host = (split.hostname or "").lower()
        if split.scheme not in ("http", "https"):
            # WR-01 SSRF guard: pin the scheme BEFORE the host check so a
            # ``file://``/``content://``/``javascript:``/``intent://`` URL with an
            # allowlisted host can never reach ``am start -d`` (the gateway only
            # ever sends an ``https://<host>/`` challenge nav).
            _log.warning("rejected non-http(s) challenge scheme %r", split.scheme)
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "challenge url scheme not allowed"
            }
        if host not in self._config.allowed_hosts:
            # T-10-09 SSRF guard: reject BEFORE any device action — the gateway
            # only ever sends a source's own cloudflare_challenge_url.
            _log.warning("rejected non-allowlisted challenge host %r", host)
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "challenge host not allowlisted"
            }

        # Req 1 / T-11-06: validate the optional per-solve proxy SHAPE BEFORE any
        # device action — a malformed proxy is a pre-action 422 (no hop repoint, no
        # global http_proxy set). The ipify egress target is a sidecar constant, so
        # the only caller-supplied URL here is the upstream proxy server (validated
        # for scheme + hostname, never blindly fetched). The existing challenge_url
        # host-allowlist above is untouched.
        proxy_err = self._validate_proxy(payload.get("proxy"))
        if proxy_err is not None:
            return proxy_err
        proxy = payload.get("proxy")

        return self._run_solve(challenge_url, host, proxy, disconnected)

    def eval(
        self,
        *,
        api_key: str | None,
        body: bytes,
        disconnected: Callable[[], bool] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Run gateway-supplied JS in the warm cleared WebView (EVAL-01 / SEC-01).

        Replicates ``/solve``'s rails verbatim — ``X-Solver-Key`` auth BEFORE any
        body read or device action (T-14-01), the ``urlsplit`` scheme-pinned-then-
        host-allowlisted SSRF guard BEFORE any device action (T-14-02) — then adds a
        ``js`` parse. ``js`` is sidecar-trusted (gateway-authored, sent over the
        authenticated control channel with no published host port, T-14-03) but
        STILL requires the key + allowlist. The ``js`` and the eval result are NEVER
        logged (T-14-04).
        """
        if not self._authenticate(api_key):
            # T-14-01: no device action without a valid key.
            return int(HTTPStatus.UNAUTHORIZED), {
                "error": "invalid or missing X-Solver-Key"
            }

        try:
            payload = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return int(HTTPStatus.BAD_REQUEST), {"error": "malformed JSON body"}
        if not isinstance(payload, dict):
            return int(HTTPStatus.BAD_REQUEST), {"error": "body must be a JSON object"}

        challenge_url = payload.get("challenge_url")
        if not isinstance(challenge_url, str) or not challenge_url:
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "challenge_url is required"
            }

        split = urlsplit(challenge_url)
        host = (split.hostname or "").lower()
        if split.scheme not in ("http", "https"):
            # T-14-02 SSRF guard: pin the scheme BEFORE the host check (mirrors
            # /solve) so a non-http(s) URL with an allowlisted host never navigates.
            _log.warning("rejected non-http(s) eval challenge scheme %r", split.scheme)
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "challenge url scheme not allowed"
            }
        if host not in self._config.allowed_hosts:
            # T-14-02 SSRF guard: reject BEFORE any device action.
            _log.warning("rejected non-allowlisted eval challenge host %r", host)
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "challenge host not allowlisted"
            }

        # The gateway-authored JS to run in-page. Sidecar-trusted, but the request
        # still had to pass the key + allowlist above (T-14-03). NEVER logged.
        js = payload.get("js")
        if not isinstance(js, str) or not js:
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {"error": "js is required"}
        # Optional SPA-readiness predicate: a JS boolean EXPRESSION string (NOT a
        # CSS selector) the eval polls until truthy before running ``js``.
        wait_for = payload.get("wait_for")
        if wait_for is not None and not isinstance(wait_for, str):
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "wait_for must be a string"
            }
        # Bug 5 follow-on #3: when set, ALSO extract the host-scoped cf_clearance from
        # the WebView jar after the clear+eval and return it under a ``clearance`` key,
        # so the gateway can mint+hold a page-holding source's (comix's) replayable
        # clearance without a destructive ``/solve``. Optional bool; default False keeps
        # every existing comix extractor eval's response shape byte-for-byte unchanged.
        with_clearance = payload.get("extract_clearance", False)
        if not isinstance(with_clearance, bool):
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "extract_clearance must be a boolean"
            }

        # Req 7 parity with /solve: validate the optional per-eval proxy SHAPE BEFORE
        # any device action — a malformed proxy is a pre-action 422 (no hop repoint,
        # no global http_proxy set). Same validator as /solve (T-11-06: the only
        # caller-supplied URL is the upstream proxy server, validated for scheme +
        # hostname, never blindly fetched). The credentials are never logged.
        proxy_err = self._validate_proxy(payload.get("proxy"))
        if proxy_err is not None:
            return proxy_err
        proxy = payload.get("proxy")

        return self._run_eval(
            challenge_url,
            host,
            js,
            wait_for,
            proxy,
            disconnected,
            with_clearance=with_clearance,
        )

    @staticmethod
    def _validate_proxy(
        proxy: Any,
    ) -> tuple[int, dict[str, Any]] | None:
        """Return a 422 tuple if ``proxy`` is malformed, else ``None`` (valid/absent).

        A present proxy MUST be a dict with a str ``server`` whose ``urlsplit``
        yields an http/https scheme AND a hostname; ``username``/``password`` are
        optional and, if present, must be str. The credentials are never logged.
        """
        if proxy is None:
            return None
        if not isinstance(proxy, dict):
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {"error": "malformed proxy"}
        server = proxy.get("server")
        if not isinstance(server, str) or not server:
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "malformed proxy server"
            }
        split = urlsplit(server)
        if split.scheme not in ("http", "https") or not split.hostname:
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "malformed proxy server"
            }
        # WR-04: force the lazy port parse HERE, while still pre-device. An
        # out-of-range/non-numeric port (e.g. ``http://host:99999``) makes
        # ``urlsplit(...).port`` raise ``ValueError: Port out of range 0-65535``;
        # without this guard that ValueError surfaces later inside ``_proxy_parts``
        # (in the worker thread), is swallowed by ``_run_solve``'s broad except, and
        # becomes a misleading 504 AFTER the device-action stage. Catch it here so a
        # bad port is the clean pre-action 422 the contract promises.
        try:
            _ = split.port
        except ValueError:
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "malformed proxy server"
            }
        username = proxy.get("username")
        password = proxy.get("password")
        for cred, value in (("username", username), ("password", password)):
            if value is not None and not isinstance(value, str):
                return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                    "error": f"malformed proxy {cred}"
                }
        # CR-2: reject a half-specified credential pair PRE-device. _proxy_parts only
        # appends the `#user:pass` fragment when BOTH are present, so a lone
        # username/password would silently fall back to an UNAUTHENTICATED upstream
        # and fail later as a 504 — AFTER the device + hop were already mutated. An
        # XOR presence check returns the 422 up front (same pre-flight discipline as
        # the WR-04 port check).
        if bool(username) != bool(password):
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "malformed proxy credentials"
            }
        return None

    def _run_solve(
        self,
        challenge_url: str,
        host: str,
        proxy: dict[str, Any] | None = None,
        disconnected: Callable[[], bool] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        cancel = Event()
        # D-07 / Pitfall 6: a proxied solve adds a cross-container CONNECT hop +
        # egress-verify BEFORE the tap loop, so the OUTER future wait below is bounded
        # by the LONGER proxy_solve_timeout_s to absorb that added overhead.
        # NOTE (CR-3 / IN-01): this extends ONLY the outer future wait. The inner
        # locate→tap→poll deadline in _drive_solve stays at the base solve_timeout_s
        # BY DESIGN — the extra budget covers the hop + egress-verify overhead, NOT
        # more tap time (the live proxied gate passed well within the base tap
        # budget). A no-proxy solve keeps the base solve_timeout_s end-to-end
        # (D-08, unchanged).
        timeout_s = (
            self._config.proxy_solve_timeout_s
            if proxy is not None
            else self._config.solve_timeout_s
        )
        # #275 req 2 (T-nup-01): NON-blocking acquire. A /solve arriving while one is
        # already in flight is rejected 503 immediately rather than queuing behind the
        # single device — an abandoned/bursty caller can never pile a backlog the one
        # redroid cannot work off. The single WebView solve stays serialized (T-10-11).
        if not self._lock.acquire(blocking=False):
            _log.info("solver busy; rejecting concurrent /solve for host %s", host)
            return int(HTTPStatus.SERVICE_UNAVAILABLE), {"error": "solver busy"}
        try:
            future = self._executor.submit(
                self._pipeline.solve, challenge_url, host, cancel, proxy=proxy
            )
            # #275 req 1: wait on the worker in short polls (bounded overall by
            # timeout_s) so an abandoned request is detected and the in-flight solve
            # cancelled PROMPTLY, instead of grinding the full deadline against the
            # single device while a CLOSE_WAIT pile-up builds.
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _log.warning("solve timed out for host %s", host)
                    # issue #207 / WR-02: the worker can't be force-killed. Signal
                    # cooperative cancellation and drain it within a BOUNDED grace
                    # window (still under the lock) so the orphan resets the device
                    # and exits before the next /solve — no cascading 504s.
                    self._cancel_and_drain(future, cancel, host)
                    return int(HTTPStatus.GATEWAY_TIMEOUT), {"error": "solve timed out"}
                try:
                    result = future.result(timeout=min(_DISCONNECT_POLL_S, remaining))
                except FuturesTimeout:
                    # Still solving — if the caller has since disconnected, cancel the
                    # orphan now (don't pin the device for the rest of the deadline).
                    if disconnected is not None and disconnected():
                        _log.info(
                            "caller disconnected mid-solve for host %s; cancelling",
                            host,
                        )
                        self._cancel_and_drain(future, cancel, host)
                        return _CLIENT_CLOSED_REQUEST, {"error": "client disconnected"}
                    continue
                except Exception:  # noqa: BLE001 — any pipeline failure ⇒ 504
                    # IN-02: keep the traceback so field solve failures are
                    # diagnosable (still never logs the token — the value isn't here).
                    _log.warning("solve failed for host %s", host, exc_info=True)
                    return int(HTTPStatus.GATEWAY_TIMEOUT), {"error": "solve failed"}
                # Redacted success event ONLY (T-10-10) — never the minted token value.
                # The verified egress_ip IS loggable (Req 6 / T-10-04).
                _log.info("solved %s (egress %s)", host, result.egress_ip or "direct")
                return int(HTTPStatus.OK), {
                    "cf_clearance": result.cf_clearance,
                    "user_agent": result.user_agent,
                    "host": result.host,
                    # Req 6: present on EVERY response — the verified egress on a
                    # proxied solve, "" on the no-proxy path (additive; D-08 unchanged).
                    "egress_ip": result.egress_ip,
                    # Additive-only: the minted cookie's epoch expiry (or null) so the
                    # gateway re-mints ahead of the lapse. Non-sensitive (D-08).
                    "cf_clearance_expires": result.cf_clearance_expires,
                }
        finally:
            self._lock.release()

    def _run_eval(
        self,
        challenge_url: str,
        host: str,
        js: str,
        wait_for: str | None = None,
        proxy: dict[str, Any] | None = None,
        disconnected: Callable[[], bool] | None = None,
        *,
        with_clearance: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        """Serialize + timeout-bound one in-WebView eval (mirrors ``_run_solve``).

        Shares the SAME ``self._lock`` + single-worker ``self._executor`` as
        ``_run_solve`` (never a second executor), so an eval and a solve can never
        run concurrently against the one redroid (PERF-01 / R1). NON-blocking lock
        acquire → 503 busy; short-poll the worker future bounded by the solve
        timeout, cancelling-and-draining on timeout / caller disconnect. On success
        returns ``(200, {"value": <marshalled eval result>})``. The ``js`` and the
        result value are NEVER logged (T-14-04) — the success event carries only the
        host.

        When ``proxy`` is supplied the eval adds the cross-container CONNECT hop +
        egress-verify before the eval nav (Req 7 parity with ``_run_solve``), so the
        outer future wait is bounded by the LONGER ``proxy_solve_timeout_s`` to absorb
        that overhead. A no-proxy eval keeps the base ``solve_timeout_s`` (D-08).
        """
        cancel = Event()
        timeout_s = (
            self._config.proxy_solve_timeout_s
            if proxy is not None
            else self._config.solve_timeout_s
        )
        # T-14-05 / PERF-01: NON-blocking acquire — an eval (or solve) arriving while
        # one is already in flight is rejected 503 immediately rather than queuing
        # behind the single device.
        if not self._lock.acquire(blocking=False):
            _log.info("solver busy; rejecting concurrent /eval for host %s", host)
            return int(HTTPStatus.SERVICE_UNAVAILABLE), {"error": "solver busy"}
        try:
            # Bug 5 follow-on #3: thread ``with_clearance`` ONLY when requested so an
            # ordinary eval calls the pipeline (and any test fake) with the
            # byte-for-byte unchanged kwargs; clearance evals return ``(value, clr)``.
            eval_kwargs: dict[str, Any] = {"wait_for": wait_for, "proxy": proxy}
            if with_clearance:
                eval_kwargs["with_clearance"] = True
            future = self._executor.submit(
                self._pipeline.eval_in_webview,
                challenge_url,
                host,
                js,
                cancel,
                **eval_kwargs,
            )
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _log.warning("eval timed out for host %s", host)
                    self._cancel_and_drain(future, cancel, host)
                    return int(HTTPStatus.GATEWAY_TIMEOUT), {"error": "eval timed out"}
                try:
                    outcome = future.result(timeout=min(_DISCONNECT_POLL_S, remaining))
                except FuturesTimeout:
                    if disconnected is not None and disconnected():
                        _log.info(
                            "caller disconnected mid-eval for host %s; cancelling",
                            host,
                        )
                        self._cancel_and_drain(future, cancel, host)
                        return _CLIENT_CLOSED_REQUEST, {"error": "client disconnected"}
                    continue
                except EvalError as exc:
                    # Mode-E follow-up: the in-page promise REJECTED — most often a
                    # comix-origin 5xx (Cloudflare "origin down/unreachable" 521/520/522
                    # surfaced by the env-module axios as "Request failed with status
                    # code 5xx"). Surface the bounded, token-free summary in the body
                    # under ``detail`` so the gateway can distinguish a TRANSIENT origin
                    # 5xx (worth one bounded retry) from a 4xx / non-origin throw (not).
                    # The summary is built by ``_raise_if_eval_threw`` from the JS
                    # error's first description line only — it carries NEITHER the
                    # evaluated ``js`` NOR the marshalled value NOR any token (T-14-04).
                    # Keep the redacted warning + trace for field diagnosis.
                    _log.warning("eval failed for host %s", host, exc_info=True)
                    return int(HTTPStatus.GATEWAY_TIMEOUT), {
                        "error": "eval threw",
                        "detail": str(exc),
                    }
                except Exception:  # noqa: BLE001 — any other pipeline failure ⇒ 504
                    # Never logs the js or the result (the value isn't in the
                    # traceback message) — keep the trace for field diagnosis.
                    _log.warning("eval failed for host %s", host, exc_info=True)
                    return int(HTTPStatus.GATEWAY_TIMEOUT), {"error": "eval failed"}
                # Redacted success event ONLY (T-14-04) — never the js or the result.
                _log.info("evaluated in webview for host %s", host)
                if not with_clearance:
                    return int(HTTPStatus.OK), {"value": outcome}
                # ``with_clearance``: the pipeline returned ``(value, SolveResult |
                # None)``. Carry the cf_clearance back under a ``clearance`` key (null
                # when none was minted) so the gateway can hold it for the httpx image
                # leg. The token rides only the response body — NEVER a log (T-14-04).
                value, clearance = outcome
                clearance_body: dict[str, Any] | None = None
                if clearance is not None:
                    clearance_body = {
                        "cf_clearance": clearance.cf_clearance,
                        "user_agent": clearance.user_agent,
                        "cf_clearance_expires": clearance.cf_clearance_expires,
                        "egress_ip": clearance.egress_ip,
                    }
                return int(HTTPStatus.OK), {"value": value, "clearance": clearance_body}
        finally:
            self._lock.release()

    def _cancel_and_drain(self, future: Any, cancel: Event, host: str) -> None:
        """Signal cancellation and wait (bounded) for the orphan to unwind (#207).

        Called under the service lock on a solve timeout. ``cancel.set()`` tells
        the worker to abort at its next adb/CDP checkpoint, reset the device, and
        exit; we then wait at most ``cancel_grace_s`` for it to do so. A worker
        wedged in a single blocking syscall may outlive the grace — we log and
        move on rather than hold the lock forever, but the common case frees the
        device (and the next caller) within the window.
        """
        cancel.set()
        try:
            future.result(timeout=self._config.cancel_grace_s)
        except FuturesTimeout:
            _log.warning(
                "solve for host %s did not abort within %.0fs grace; worker may "
                "still be running",
                host,
                self._config.cancel_grace_s,
            )
        except SolveCancelled:
            _log.info("solve for host %s cancelled cleanly after timeout", host)
        except Exception:  # noqa: BLE001 — drain must not raise out of cleanup
            _log.warning("solve for host %s errored after cancel", host, exc_info=True)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


# ── stdlib HTTP transport (thin shim over SolverService) ─────────────────────


class _SolverHTTPServer(ThreadingHTTPServer):
    """Threaded server holding the shared ``SolverService`` (solves are locked)."""

    daemon_threads = True
    # Listen backlog. stdlib's default (5) drops connections under a burst: a
    # 15-way simultaneous /solve burst reset 1 connection at the TCP-accept layer
    # before it reached the app (Phase 10 stress test — the app itself served
    # 41/41). Solves still SERIALIZE behind SolverService's lock; this only widens
    # the accept queue so bursty callers queue instead of getting RST.
    request_queue_size = 128

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        service: SolverService,
    ) -> None:
        super().__init__(server_address, handler)
        self.service = service


class _Handler(BaseHTTPRequestHandler):
    server: _SolverHTTPServer  # narrowed for attribute access
    # Socket read timeout (CR-01): kills slow/stalled connections so a slow-loris
    # body dribble cannot pin a daemon worker thread indefinitely.
    timeout = _HANDLER_READ_TIMEOUT_S

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path == "/healthz":
            status, payload = self.server.service.healthz()
            self._send_json(status, payload)
            return
        self._send_json(int(HTTPStatus.NOT_FOUND), {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        # Phase 14: /eval shares /solve's pre-auth + body-cap rails verbatim — only
        # the service method dispatched at the end differs.
        if self.path not in ("/solve", "/eval"):
            self._send_json(int(HTTPStatus.NOT_FOUND), {"error": "not found"})
            return
        # CR-01: authenticate BEFORE reading the body. An unauthenticated caller
        # must never be able to force a large/slow read on the LAN-exposed port.
        api_key = self.headers.get("X-Solver-Key")
        if not self.server.service.authenticate(api_key):
            self.close_connection = True  # don't drain a possibly-large body
            self._send_json(
                int(HTTPStatus.UNAUTHORIZED),
                {"error": "invalid or missing X-Solver-Key"},
            )
            return
        # IN-03: a non-numeric Content-Length is a clean 400, not a 500 crash.
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self.close_connection = True
            self._send_json(
                int(HTTPStatus.BAD_REQUEST), {"error": "invalid Content-Length"}
            )
            return
        # CR-01 / T-14-05: cap the declared INBOUND body size BEFORE reading a single
        # byte. (The cap is inbound-only; a large chapter-list eval RESULT travels on
        # the outbound side and is NOT capped — research A4.)
        if length > _MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(
                int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE), {"error": "body too large"}
            )
            return
        body = self.rfile.read(length) if length > 0 else b""
        # #275 req 1: let the service detect a mid-call caller disconnect (peek the
        # socket for EOF) so an abandoned /solve or /eval cancels its device work.
        if self.path == "/solve":
            status, payload = self.server.service.solve(
                api_key=api_key,
                body=body,
                disconnected=lambda: _peer_disconnected(self.connection),
            )
        else:  # /eval
            status, payload = self.server.service.eval(
                api_key=api_key,
                body=body,
                disconnected=lambda: _peer_disconnected(self.connection),
            )
        # #275 cleanup: the client may already be gone (CLOSE_WAIT / BrokenPipe). A
        # write to a dead peer must not crash the daemon worker thread — swallow it
        # (OSError covers BrokenPipeError/ConnectionError, its subclasses).
        try:
            self._send_json(status, payload)
        except OSError:
            _log.info("client gone before %s response could be sent", self.path)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence the default access log (keeps request lines — and any header
        # echo — out of stdout); the service emits its own redacted events.
        return


def build_pipeline(config: SidecarConfig) -> SolvePipeline:
    """Construct the real device-backed pipeline from the config.

    Threads the hop ``host``/``port`` into the pipeline so the proxied solve can
    route the device-wide ``global http_proxy`` through the cross-container hop
    (``android-solver:<hop_port>`` — never localhost, Pitfall 1).
    """
    device = AdbDevice(config.adb_target)
    return AndroidSolvePipeline(
        device,
        timeout_s=config.solve_timeout_s,
        hop_host=config.hop_host,
        hop_port=config.hop_port,
    )


def _spawn_proxy_hop(config: SidecarConfig) -> subprocess.Popen[bytes]:
    """Start the persistent pproxy CONNECT hop as a sidecar child subprocess.

    Runs ``python -m android_solver.proxy_hop --port <hop_port>`` (Runtime State).
    The child is terminated on sidecar shutdown by :func:`_terminate_proxy_hop`
    so it is never orphaned (T-11-04).
    """
    _log.info("starting proxy hop child on port %d", config.hop_port)
    return subprocess.Popen(  # noqa: S603 — argv is built from config ints/constants
        [
            sys.executable,
            "-m",
            "android_solver.proxy_hop",
            "--port",
            str(config.hop_port),
        ]
    )


def _terminate_proxy_hop(hop: subprocess.Popen[bytes] | None) -> None:
    """Terminate the hop child on shutdown (bounded), killing it if it lingers."""
    if hop is None:
        return
    hop.terminate()
    try:
        hop.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        _log.warning("proxy hop did not exit on terminate; killing")
        hop.kill()


_BOOT_CLEAR_RETRIES = 5
_BOOT_CLEAR_RETRY_SLEEP_S = 2.0


def _boot_defensive_clear(
    config: SidecarConfig,
    *,
    retries: int = _BOOT_CLEAR_RETRIES,
    sleep_s: float = _BOOT_CLEAR_RETRY_SLEEP_S,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Clear any residual device-wide ``global http_proxy`` at sidecar boot (Req 5).

    A prior crashed proxied solve could have left ``global http_proxy`` set on the
    redroid device, and that setting SURVIVES a sidecar restart. This boot-time clear
    restores direct egress so the first no-proxy solve (D-08, unchanged) is not
    silently routed through a now-idle hop.

    CR-1: redroid is ``depends_on: service_healthy``, so the device is normally
    booted before the sidecar starts — but a TRANSIENT adb hiccup at boot must not
    PERMANENTLY skip the clear (a single failed attempt would strand a stale proxy
    until the next proxied solve repoints it). Retry a bounded number of times before
    giving up, so the fix lives entirely at boot and the per-solve no-proxy path
    stays byte-for-byte unchanged (D-08 — no extra adb call per direct solve).
    Returns True once the clear ran; False if every attempt failed (logged,
    swallowed — it never blocks startup indefinitely).
    """
    device = AdbDevice(config.adb_target)
    for attempt in range(1, retries + 1):
        try:
            device.connect()
            device.clear_global_http_proxy()
            _log.info("boot-time defensive global http_proxy clear ran")
            return True
        except Exception:  # noqa: BLE001 — best-effort; device may not be ready yet
            if attempt == retries:
                _log.warning(
                    "boot-time defensive proxy clear gave up after %d attempt(s) "
                    "(device not ready)",
                    retries,
                    exc_info=True,
                )
                return False
            sleep(sleep_s)
    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    config = SidecarConfig.from_env()
    # Boot the persistent hop child + a defensive proxy clear BEFORE serving so the
    # first solve has a live hop and a clean device (Runtime State / Req 5).
    hop = _spawn_proxy_hop(config)
    _boot_defensive_clear(config)
    service = SolverService(config, build_pipeline(config))
    server = _SolverHTTPServer(
        (config.bind_host, config.port), _Handler, service=service
    )
    _log.info(
        "android-solver control API listening on %s:%s (docker-internal)",
        config.bind_host,
        config.port,
    )
    try:
        server.serve_forever()
    finally:
        # Never orphan the hop child (T-11-04); always shut the executor down.
        service.close()
        _terminate_proxy_hop(hop)


if __name__ == "__main__":
    main()
