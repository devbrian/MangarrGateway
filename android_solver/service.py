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
import json
import logging
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
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

# Cross-origin Cloudflare OOPIF readiness poll: the Turnstile iframe renders a
# few seconds after the page loads, so wait for it before locating the checkbox.
_CF_FRAME_URL_MARKER = "challenges.cloudflare.com"
_FRAME_POLL_TIMEOUT_S = 20.0
_FRAME_POLL_INTERVAL_S = 1.5

# Re-tap cadence: on a COLD WebView the challenges.cloudflare.com OOPIF frame URL
# appears BEFORE the checkbox inside it is interactive, so a single tap lands on
# a not-yet-ready widget and misses. We re-locate + re-tap every _RETAP_INTERVAL_S
# (polling for clearance in between) until the token is minted or the deadline
# fires. Re-locating returns coords ONLY while the cf widget is still present, so
# once the challenge passes we stop tapping and just poll out the clearance.
_RETAP_INTERVAL_S = 5.0


class SolveError(RuntimeError):
    """The solve pipeline could not mint a clearance (surfaces as 504)."""


@dataclass(frozen=True)
class SolveResult:
    """A successful solve: the minted token + the WebView UA it was minted under."""

    cf_clearance: str
    user_agent: str
    host: str


class SolvePipeline(Protocol):
    """The solve surface the control API drives (a FAKE is injected in tests)."""

    def solve(self, challenge_url: str, host: str) -> SolveResult: ...

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
    ) -> None:
        self._device = device
        self._timeout_s = timeout_s
        self._launch_settle_s = launch_settle_s
        self._poll_interval_s = poll_interval_s
        self._retap_interval_s = retap_interval_s
        self._frame_poll_timeout_s = frame_poll_timeout_s
        self._frame_poll_interval_s = frame_poll_interval_s
        # Lazy default factories live in cdp (same package) — reuse them so the
        # devtools websocket/http plumbing has a single implementation (R1).
        from android_solver.cdp import _default_http_get, _default_ws_factory

        self._ws_factory: WebSocketFactory = ws_factory or _default_ws_factory
        self._http_get: HttpGetter = http_get or _default_http_get

    def health(self) -> bool:
        """Cheap reachability probe: ``adb connect`` answers ⇒ redroid is up."""
        try:
            self._device.connect()
            return True
        except Exception:  # noqa: BLE001 — a health probe must never raise
            return False

    def solve(self, challenge_url: str, host: str) -> SolveResult:
        self._device.connect()
        self._device.force_stop_and_clear()
        self._device.launch_url(challenge_url)
        time.sleep(self._launch_settle_s)  # floor before the devtools socket is up

        pid = self._device.pidof()
        port = self._device.forward_devtools(pid)
        ws_url = self._discover_page_ws(port)

        # Bound the whole locate→tap→poll loop by the solve deadline (T-10-11).
        deadline = time.monotonic() + self._timeout_s
        ws = self._ws_factory(ws_url, timeout=_WS_TIMEOUT_S)
        try:
            cdp_call(ws, "Page.enable", command_id=10)
            cdp_call(ws, "DOM.enable", command_id=11)
            # Wait for the cross-origin Cloudflare OOPIF to render (a few seconds
            # after load) — the real readiness gate, not the fixed settle above.
            self._wait_for_cf_frame(ws)
            # Page ws, DOM/Page enable, frame readiness, and the viewport scales
            # are computed ONCE; only locate+tap+poll repeats inside the loop.
            x_scale, y_scale = self._compute_scales(ws)
            token = self._tap_until_cleared(
                ws, ws_url, host, x_scale, y_scale, deadline
            )
        finally:
            ws.close()
        if not token:
            raise SolveError(f"clearance not minted for {host} before deadline")

        user_agent = webview_user_agent(f"http://localhost:{port}/json/version") or ""
        return SolveResult(cf_clearance=token, user_agent=user_agent, host=host)

    def _tap_until_cleared(
        self,
        ws: WebSocketLike,
        ws_url: str,
        host: str,
        x_scale: float,
        y_scale: float,
        deadline: float,
    ) -> str | None:
        """Re-locate + re-tap the Turnstile checkbox until clearance is minted.

        On a COLD WebView the ``challenges.cloudflare.com`` OOPIF frame URL appears
        BEFORE the checkbox inside it is interactive, so a single tap can land on a
        not-yet-ready widget and miss. Each round re-runs ``locate_checkbox`` (which
        returns coords ONLY while the cf widget is still present) and re-taps, then
        polls for the clearance for up to ``_retap_interval_s`` before re-locating.

        Two key properties hold:
          * The re-tap naturally lands the moment the checkbox becomes interactive
            (cold-start race fixed), bounded by the solve ``deadline``.
          * Once the challenge passes (widget/iframe gone ⇒ no cf OOPIF frame), the
            re-locate returns ``None`` so we STOP tapping and just poll out the
            clearance — never re-triggering a fresh challenge by tapping a cleared
            page.
        """
        token: str | None = None
        while time.monotonic() < deadline:
            coords = locate_checkbox(
                ws,
                screencap=self._device.screencap(),
                x_scale=x_scale,
                y_scale=y_scale,
            )
            if coords is not None:
                self._device.input_tap(*coords)
            # Short poll window between taps, never overrunning the deadline.
            poll_until = min(deadline, time.monotonic() + self._retap_interval_s)
            while time.monotonic() < poll_until:
                token = extract_clearance(ws_url, host)
                if token:
                    return token
                time.sleep(self._poll_interval_s)
        return token

    def _wait_for_cf_frame(self, ws: WebSocketLike) -> None:
        """Poll Page.getFrameTree until the challenges.cloudflare.com OOPIF appears.

        The Turnstile widget renders in a cross-origin child frame a few seconds
        after the page loads. Returns once present; returns anyway on timeout so
        locate_checkbox can still try the secondary/screenshot paths.
        """
        deadline = time.monotonic() + self._frame_poll_timeout_s
        command_id = 20
        while time.monotonic() < deadline:
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

        host = (urlsplit(challenge_url).hostname or "").lower()
        if host not in self._config.allowed_hosts:
            # T-10-09 SSRF guard: reject BEFORE any device action — the gateway
            # only ever sends a source's own cloudflare_challenge_url.
            _log.warning("rejected non-allowlisted challenge host %r", host)
            return int(HTTPStatus.UNPROCESSABLE_ENTITY), {
                "error": "challenge host not allowlisted"
            }

        return self._run_solve(challenge_url, host)

    def _run_solve(self, challenge_url: str, host: str) -> tuple[int, dict[str, Any]]:
        with self._lock:  # serialize: one WebView solve at a time (T-10-11)
            future = self._executor.submit(self._pipeline.solve, challenge_url, host)
            try:
                result = future.result(timeout=self._config.solve_timeout_s)
            except FuturesTimeout:
                _log.warning("solve timed out for host %s", host)
                return int(HTTPStatus.GATEWAY_TIMEOUT), {"error": "solve timed out"}
            except Exception:  # noqa: BLE001 — any pipeline failure ⇒ 504
                _log.warning("solve failed for host %s", host)
                return int(HTTPStatus.GATEWAY_TIMEOUT), {"error": "solve failed"}

        # Redacted success event ONLY (T-10-10) — never the minted token value.
        _log.info("solved %s", host)
        return int(HTTPStatus.OK), {
            "cf_clearance": result.cf_clearance,
            "user_agent": result.user_agent,
            "host": result.host,
        }

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


# ── stdlib HTTP transport (thin shim over SolverService) ─────────────────────


class _SolverHTTPServer(ThreadingHTTPServer):
    """Threaded server holding the shared ``SolverService`` (solves are locked)."""

    daemon_threads = True

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
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        api_key = self.headers.get("X-Solver-Key")
        status, payload = self.server.service.solve(api_key=api_key, body=body)
        self._send_json(status, payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence the default access log (keeps request lines — and any header
        # echo — out of stdout); the service emits its own redacted events.
        return


def build_pipeline(config: SidecarConfig) -> SolvePipeline:
    """Construct the real device-backed pipeline from the config."""
    device = AdbDevice(config.adb_target)
    return AndroidSolvePipeline(device, timeout_s=config.solve_timeout_s)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    config = SidecarConfig.from_env()
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
        service.close()


if __name__ == "__main__":
    main()
