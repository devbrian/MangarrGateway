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
import socket
import subprocess
import sys
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock
from typing import Any, Protocol
from urllib.parse import urlsplit

from android_solver.cdp import (
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
_WS_TIMEOUT_S = 15.0
_HTTP_TIMEOUT_S = 10.0

# Pre-auth request hardening for the LAN-exposed control API (CR-01). A /solve
# body is tiny JSON (``{"challenge_url": "https://<host>/"}``), so any larger
# Content-Length is rejected 413 BEFORE the body is read — an unauthenticated
# caller can never force a multi-GB allocation. The handler also carries a socket
# read timeout so a slow-loris dribble cannot pin a worker thread indefinitely.
_MAX_BODY_BYTES = 64 * 1024
_HANDLER_READ_TIMEOUT_S = 15.0

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


class SolveError(RuntimeError):
    """The solve pipeline could not mint a clearance (surfaces as 504)."""


class SolveCancelled(RuntimeError):
    """The solve was cooperatively cancelled after the timeout (issue #207).

    Raised by the pipeline when it observes the cancel signal at one of its
    adb/CDP checkpoints. The pipeline resets the device on its way out so the next
    solve starts clean; the service treats it as a (clean) post-timeout exit.
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
                        f"proxied egress IP differed (expected {expected_egress_ip}, "
                        f"observed {observed}) — is the upstream a rotating/"
                        f"non-sticky proxy? egress-verify needs a sticky-session "
                        f"upstream that holds one exit IP across connections"
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
            # Wait for the cross-origin Cloudflare OOPIF to render (a few seconds
            # after load) — the real readiness gate, not the fixed settle above.
            self._wait_for_cf_frame(ws, cancel)
            # Page ws, DOM/Page enable, frame readiness, and the viewport scales
            # are computed ONCE; only locate+tap+poll repeats inside the loop.
            x_scale, y_scale = self._compute_scales(ws)
            token = self._tap_until_cleared(
                ws, ws_url, host, x_scale, y_scale, deadline, cancel
            )
        finally:
            ws.close()
        if not token:
            raise SolveError(f"clearance not minted for {host} before deadline")

        # IN-01: use the INJECTED getter (so tests/proxy injection apply), and
        # guard the fetch — a trivial /json/version hiccup must not discard an
        # already-minted, ~60s-expensive token (fall back to an empty UA).
        try:
            user_agent = (
                webview_user_agent(
                    f"http://localhost:{port}/json/version", http_get=self._http_get
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
            host=host,
            egress_ip=expected_egress_ip,
        )

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
    ) -> str | None:
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
        token: str | None = None
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
                token = extract_clearance(ws_url, host)
            except Exception:  # noqa: BLE001 — a transient cookie poll must not abort the solve
                _log.warning(
                    "clearance poll failed transiently; continuing", exc_info=True
                )
                token = None
            if token:
                return token

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
        return token

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

    def _wait_for_cf_frame(
        self, ws: WebSocketLike, cancel: Event | None = None
    ) -> None:
        """Poll Page.getFrameTree until the challenges.cloudflare.com OOPIF appears.

        The Turnstile widget renders in a cross-origin child frame a few seconds
        after the page loads. Returns once present; returns anyway on timeout so
        locate_checkbox can still try the secondary/screenshot paths.
        """
        deadline = time.monotonic() + self._frame_poll_timeout_s
        command_id = 20
        while time.monotonic() < deadline:
            _raise_if_cancelled(cancel)  # issue #207: don't poll past a cancel
            tree = cdp_call(ws, "Page.getFrameTree", command_id=command_id)
            command_id += 1
            if self._frame_tree_has_cf(tree.get("frameTree")):
                return
            time.sleep(self._frame_poll_interval_s)
        _log.warning("cloudflare OOPIF did not appear before frame-poll deadline")

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

    def solve(self, *, api_key: str | None, body: bytes) -> tuple[int, dict[str, Any]]:
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

        return self._run_solve(challenge_url, host, proxy)

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
        for cred in ("username", "password"):
            value = proxy.get(cred)
            if value is not None and not isinstance(value, str):
                return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                    "error": f"malformed proxy {cred}"
                }
        return None

    def _run_solve(
        self, challenge_url: str, host: str, proxy: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        cancel = Event()
        # D-07 / Pitfall 6: a proxied solve adds a cross-container CONNECT hop +
        # egress-verify, so it is bounded by the LONGER proxy_solve_timeout_s; a
        # no-proxy solve keeps the base solve_timeout_s (D-08, unchanged).
        timeout_s = (
            self._config.proxy_solve_timeout_s
            if proxy is not None
            else self._config.solve_timeout_s
        )
        with self._lock:  # serialize: one WebView solve at a time (T-10-11)
            future = self._executor.submit(
                self._pipeline.solve, challenge_url, host, cancel, proxy=proxy
            )
            try:
                result = future.result(timeout=timeout_s)
            except FuturesTimeout:
                _log.warning("solve timed out for host %s", host)
                # issue #207 / WR-02: the worker can't be force-killed. Signal
                # cooperative cancellation and drain it within a BOUNDED grace
                # window (still under the lock) so the orphan resets the device
                # and exits before the next /solve — no cascading 504s.
                self._cancel_and_drain(future, cancel, host)
                return int(HTTPStatus.GATEWAY_TIMEOUT), {"error": "solve timed out"}
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
            # Req 6: present on EVERY response — the verified egress on a proxied
            # solve, "" on the no-proxy path (additive-only; D-08 keys unchanged).
            "egress_ip": result.egress_ip,
        }

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
        if self.path != "/solve":
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
        # CR-01: cap the declared body size BEFORE reading a single byte.
        if length > _MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(
                int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE), {"error": "body too large"}
            )
            return
        body = self.rfile.read(length) if length > 0 else b""
        status, payload = self.server.service.solve(api_key=api_key, body=body)
        self._send_json(status, payload)

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


def _boot_defensive_clear(config: SidecarConfig) -> None:
    """Clear any residual device-wide ``global http_proxy`` at sidecar boot (Req 5).

    A prior crashed proxied solve could have left ``global http_proxy`` set on the
    redroid device. This best-effort boot-time clear restores direct egress so the
    first no-proxy solve is not silently routed through a dead hop. It never blocks
    startup — the device may not be booted yet (logged, swallowed).
    """
    device = AdbDevice(config.adb_target)
    try:
        device.connect()
        device.clear_global_http_proxy()
        _log.info("boot-time defensive global http_proxy clear ran")
    except Exception:  # noqa: BLE001 — best-effort; device may not be ready yet
        _log.warning(
            "boot-time defensive proxy clear skipped (device not ready)",
            exc_info=True,
        )


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
