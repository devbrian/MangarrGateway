"""AndroidSolvePipeline per-solve proxied-egress lifecycle (Req 2/3/5/6).

Drives the REAL ``AndroidSolvePipeline._solve`` proxied path with fakes — no adb /
redroid / WebView / network. Asserts:

  * the device-wide proxy is set with ``(resolved_hop_ip, hop_port)`` BEFORE the
    tap and ALWAYS cleared in ``finally`` (Req 2/5), including the raise path;
  * the hop is repointed over the LOOPBACK CONTROL endpoint (127.0.0.1:hop_port+1)
    — NOT the cross-container proxy port (which silently closes a SET line);
  * the WebView egress is verified BEFORE the tap — observed==expected proceeds,
    observed!=expected raises with NO token, a transient/non-IP echo raises (Req 3);
  * the device proxy host is the RESOLVED hop IP (the device cannot resolve the
    docker name), never localhost (Pitfall 1 / T-11-05), with a literal fallback;
  * the proxied success result carries the verified ``egress_ip`` (Req 6).
"""

from __future__ import annotations

import threading
import time

import pytest
from android_solver.cdp import ClearanceCookie
from android_solver.device import AdbError
from android_solver.service import (
    AndroidSolvePipeline,
    SolveError,
    SolveResult,
)

from android_solver import service

_PROXY = {"server": "http://up.example:8080", "username": "u", "password": "p"}
_HOP_HOST = "android-solver"
_HOP_PORT = 18081
# The hop's CONTROL channel is a SEPARATE loopback listener (127.0.0.1:hop_port+1)
# — NOT the cross-container proxy port. The sidecar repoints over THIS endpoint;
# the device routes WebView egress through the PROXY port (_HOP_HOST:_HOP_PORT).
# Sending a SET to the proxy port silently closes (empty ack ⇒ ProxyHopError),
# which broke every live proxied solve until the repoint target was corrected.
_HOP_CONTROL_HOST = "127.0.0.1"
_HOP_CONTROL_PORT = _HOP_PORT + 1
# The DEVICE reaches the hop by IP, not by the docker service name: redroid's
# Android resolver is hardcoded to 8.8.8.8 and cannot resolve "android-solver".
# The sidecar resolves the name to this on-network IP for the device proxy.
_RESOLVED_HOP_IP = "172.27.0.3"
_EXPECTED_IP = "203.0.113.7"


class FakeClock:
    """Deterministic monotonic clock; ``sleep`` advances ``now``."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDevice:
    """Records proxy set/clear calls + taps over the AdbDevice surface used here."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.force_stops = 0
        self.proxy_set: list[tuple[str, int]] = []
        self.proxy_cleared = 0
        # Ordered event log so the test can assert set-before-tap-before-clear.
        self.events: list[str] = []

    def connect(self) -> None:
        return None

    def force_stop_and_clear(self) -> None:
        self.force_stops += 1

    def set_global_http_proxy(self, host: str, port: int) -> None:
        self.proxy_set.append((host, port))
        self.events.append("set_proxy")

    def clear_global_http_proxy(self) -> None:
        self.proxy_cleared += 1
        self.events.append("clear_proxy")

    def launch_url(self, url: str) -> None:
        return None

    def pidof(self) -> int:
        return 4321

    def forward_devtools(self, pid: int) -> int:
        return 9222

    def screencap(self) -> bytes:
        return b"PNG"

    def screen_size(self) -> tuple[int, int]:
        return (720, 1280)

    def input_tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        self.events.append("tap")


class FakeWs:
    def __init__(self) -> None:
        self.closed = False

    def send(self, payload: str) -> None:
        return None

    def recv(self) -> str:
        return "{}"

    def close(self) -> None:
        self.closed = True


def _build_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    device: FakeDevice,
    clock: FakeClock,
    egress_body: object,
    expected_ip: str = _EXPECTED_IP,
    repoint_calls: list[tuple[str, int, str | None]] | None = None,
) -> AndroidSolvePipeline:
    page_targets = b'[{"type":"page","webSocketDebuggerUrl":"ws://localhost:9222/p"}]'
    pipeline = AndroidSolvePipeline(
        device,  # type: ignore[arg-type]
        timeout_s=60.0,
        ws_factory=lambda url, *, timeout: FakeWs(),  # type: ignore[arg-type,return-value]
        http_get=lambda url, *, timeout: page_targets,
        launch_settle_s=0.0,
        poll_interval_s=1.0,
        hop_host=_HOP_HOST,
        hop_port=_HOP_PORT,
        egress_read_timeout_s=5.0,
    )
    monkeypatch.setattr(service, "time", clock)

    # Only the egress READ (command_id 41) returns the scripted ipify body; every
    # other CDP command (Page.enable, the _in_verification probe id 300, the
    # re-navigate, etc.) returns an empty dict so it does not look like a verifying
    # banner or a stray IP.
    def fake_cdp_call(ws, method, params=None, *, command_id):  # type: ignore[no-untyped-def]
        if method == "Runtime.evaluate" and command_id == service._EGRESS_READ_CMD_ID:
            return {"result": {"value": egress_body}}
        return {}

    monkeypatch.setattr(service, "cdp_call", fake_cdp_call)
    monkeypatch.setattr(service, "locate_checkbox", lambda ws, **k: (50, 100))

    # Return no token on the FIRST poll so a tap actually fires, then mint — this
    # lets the tests assert the set-proxy → tap → clear-proxy ordering.
    extract_state = {"calls": 0}

    def fake_extract(ws_url, host):  # type: ignore[no-untyped-def]
        extract_state["calls"] += 1
        if extract_state["calls"] < 2:
            return None
        return ClearanceCookie(value="TOKEN", expires=2000000000.0)

    monkeypatch.setattr(service, "extract_clearance", fake_extract)
    monkeypatch.setattr(service, "webview_user_agent", lambda url, **kwargs: "UA-wv")
    monkeypatch.setattr(service, "expected_egress", lambda *a, **k: expected_ip)

    calls = repoint_calls if repoint_calls is not None else []

    def fake_repoint(host, port, upstream, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((host, port, upstream))

    monkeypatch.setattr(service, "repoint", fake_repoint)

    # The sidecar resolves the docker hop name to an on-network IP for the device
    # (redroid cannot resolve docker names — Pitfall 1). Patch resolution to a
    # deterministic IP so the device-proxy host assertions are stable offline.
    monkeypatch.setattr(service.socket, "gethostbyname", lambda host: _RESOLVED_HOP_IP)

    # Collapse the pre-tap readiness/scale steps (covered by their own units).
    monkeypatch.setattr(
        pipeline,
        "_wait_for_cf_frame",
        lambda ws, ws_url, host, cancel=None, *, deadline=None: None,
    )
    monkeypatch.setattr(pipeline, "_compute_scales", lambda ws: (2.0, 2.586))
    return pipeline


# ── Req 2/3/6: matching egress proceeds to tap and returns egress_ip ──────────


def test_proxied_solve_verifies_egress_then_taps_and_returns_egress_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,  # WebView observed == self-probe expected
        repoint_calls=repoint_calls,
    )

    result: SolveResult = pipeline.solve(
        "https://mangadot.net/", "mangadot.net", proxy=_PROXY
    )

    assert result.cf_clearance == "TOKEN"
    assert result.egress_ip == _EXPECTED_IP  # Req 6: verified egress on the result
    # Proxy set with the RESOLVED hop IP:port — the device cannot resolve the
    # docker name (Pitfall 1); never localhost.
    assert device.proxy_set == [(_RESOLVED_HOP_IP, _HOP_PORT)]
    # Set BEFORE the tap; cleared AFTER (Req 2/5).
    assert device.events == ["set_proxy", "tap", "clear_proxy"]
    assert device.proxy_cleared == 1
    # Hop repointed to the authed upstream (SET), then back to DIRECT (IDLE) —
    # over the LOOPBACK CONTROL endpoint, NOT the cross-container proxy port.
    assert repoint_calls[0] == (
        _HOP_CONTROL_HOST,
        _HOP_CONTROL_PORT,
        "http://up.example:8080#u:p",
    )
    assert repoint_calls[-1] == (_HOP_CONTROL_HOST, _HOP_CONTROL_PORT, None)
    # The repoint (control) target MUST differ from the device proxy target —
    # conflating them sends SET to the pproxy port (empty ack ⇒ ProxyHopError).
    assert (repoint_calls[0][0], repoint_calls[0][1]) != device.proxy_set[0]


# ── Req 3 / T-11-01: a bypassed proxy (observed != expected) raises, no token ──


def test_egress_mismatch_raises_without_token_and_still_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body="198.51.100.99",  # WebView egress != expected → bypass
        repoint_calls=repoint_calls,
        expected_ip=_EXPECTED_IP,
    )

    with pytest.raises(SolveError):
        pipeline.solve("https://mangadot.net/", "mangadot.net", proxy=_PROXY)

    # No tap ever fired (raised before the tap) — NO wrong-IP token minted.
    assert device.taps == []
    # finally STILL cleared the proxy + idled the hop (Req 5).
    assert device.proxy_cleared == 1
    assert repoint_calls[-1] == (_HOP_CONTROL_HOST, _HOP_CONTROL_PORT, None)


# ── Req 3: a transient / non-IP echo is a FAILURE, never a pass ───────────────


def test_transient_non_ip_echo_raises_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body="<html>error: not an ip</html>",  # non-IP body
    )

    with pytest.raises(SolveError):
        pipeline.solve("https://mangadot.net/", "mangadot.net", proxy=_PROXY)

    assert device.taps == []  # echo-read failure ⇒ no tap, no token
    assert device.proxy_cleared == 1  # still cleared in finally


# ── Req 5: a raise mid-flight still clears the proxy in finally ───────────────


def test_proxy_cleared_even_when_solve_raises_midflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
    )

    # Make the post-verify tap loop blow up so the solve raises after the proxy
    # was set — the finally must STILL clear it.
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("tap loop exploded")

    monkeypatch.setattr(pipeline, "_tap_until_cleared", boom)

    with pytest.raises(RuntimeError):
        pipeline.solve("https://mangadot.net/", "mangadot.net", proxy=_PROXY)

    assert device.proxy_set == [(_RESOLVED_HOP_IP, _HOP_PORT)]
    assert device.proxy_cleared == 1  # cleared despite the mid-flight raise


# ── WR-01: a raise from set_global_http_proxy still idles the hop + clears ─────


def test_hop_idled_when_set_global_http_proxy_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WR-01: the hop repoint + the device-proxy set live INSIDE the try whose
    # finally idles the hop. If set_global_http_proxy raises (AdbError), the hop
    # must NOT be left holding the authenticated upstream — finally must idle it
    # back to DIRECT (repoint None) and run the device clear.
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
        repoint_calls=repoint_calls,
    )

    def boom(host: str, port: int) -> None:
        raise AdbError("settings put global http_proxy failed")

    monkeypatch.setattr(device, "set_global_http_proxy", boom)

    with pytest.raises(AdbError):
        pipeline.solve("https://mangadot.net/", "mangadot.net", proxy=_PROXY)

    # The hop WAS repointed to the authed upstream (SET) before the device-proxy
    # set blew up — and the finally STILL idled it back to DIRECT (IDLE/None), so
    # no credentialed hop state leaks past the failed solve.
    assert repoint_calls[0] == (
        _HOP_CONTROL_HOST,
        _HOP_CONTROL_PORT,
        "http://up.example:8080#u:p",
    )
    assert repoint_calls[-1] == (_HOP_CONTROL_HOST, _HOP_CONTROL_PORT, None)
    # The device clear ran in finally even though the set raised.
    assert device.proxy_cleared == 1
    assert device.taps == []  # never reached the tap


# ── Pitfall 1: device proxy host is the RESOLVED hop IP, never the docker name ─


def test_proxy_host_is_resolved_ip_never_docker_name_or_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
    )

    pipeline.solve("https://mangadot.net/", "mangadot.net", proxy=_PROXY)

    (host, port) = device.proxy_set[0]
    # The device gets the resolved IP — NOT the docker service name (which redroid
    # cannot resolve) and never localhost (redroid's own loopback).
    assert host == _RESOLVED_HOP_IP
    assert host != _HOP_HOST
    assert host not in ("localhost", "127.0.0.1")
    assert port == _HOP_PORT


def test_proxy_host_falls_back_to_literal_when_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the hop host cannot be resolved, fall back to the literal value and let
    # the egress-verify backstop catch a bad route (never mint a wrong-IP token).
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
    )

    def boom(host: str) -> str:
        raise OSError("name resolution failed")

    monkeypatch.setattr(service.socket, "gethostbyname", boom)

    pipeline.solve("https://mangadot.net/", "mangadot.net", proxy=_PROXY)

    assert device.proxy_set == [(_HOP_HOST, _HOP_PORT)]  # literal fallback


# ── D-08: a no-proxy solve sets NO device proxy and repoints NO hop ───────────


def test_no_proxy_solve_sets_no_device_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
        repoint_calls=repoint_calls,
    )

    result = pipeline.solve("https://mangadot.net/", "mangadot.net")

    assert result.cf_clearance == "TOKEN"
    assert result.egress_ip == ""  # additive-empty on the no-proxy path
    assert device.proxy_set == []  # no global http_proxy set
    assert device.proxy_cleared == 0  # nothing to clear
    assert repoint_calls == []  # hop never repointed
    assert device.taps == [(50, 100)]  # the solve still tapped + minted


def test_username_only_proxy_uri_omits_credentials_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _proxy_parts only appends #user:pass when BOTH are present; a server-only
    # proxy passes a bare upstream URI (no half-formed credential fragment).
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
        repoint_calls=repoint_calls,
    )

    pipeline.solve(
        "https://mangadot.net/",
        "mangadot.net",
        proxy={"server": "http://up.example:8080"},
    )

    assert repoint_calls[0] == (
        _HOP_CONTROL_HOST,
        _HOP_CONTROL_PORT,
        "http://up.example:8080",
    )


# ── Req 7: /eval proxy parity — set+verify+clear, no-proxy byte-unchanged ──────


def test_proxied_eval_verifies_egress_sets_and_clears_device_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Req 7 parity with the proxied solve: an eval with a proxy sets the device-wide
    # proxy (resolved hop IP:port) BEFORE the eval navigation, verifies the WebView
    # egress against the self-probe, runs, and ALWAYS clears the proxy + idles the hop
    # in finally — so the eval runs under the EXACT residential IP the clearance was
    # minted on. NO tap is involved on the eval path.
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,  # WebView observed == self-probe expected
        repoint_calls=repoint_calls,
    )

    pipeline.eval_in_webview(
        "https://mangadot.net/", "mangadot.net", "extract()", proxy=_PROXY
    )

    # Proxy set with the RESOLVED hop IP:port (Pitfall 1), set then cleared — and the
    # eval path never taps.
    assert device.proxy_set == [(_RESOLVED_HOP_IP, _HOP_PORT)]
    assert device.events == ["set_proxy", "clear_proxy"]
    assert device.proxy_cleared == 1
    assert device.taps == []
    # Hop repointed to the authed upstream then back to DIRECT over the LOOPBACK
    # control endpoint (never the cross-container proxy port).
    assert repoint_calls[0] == (
        _HOP_CONTROL_HOST,
        _HOP_CONTROL_PORT,
        "http://up.example:8080#u:p",
    )
    assert repoint_calls[-1] == (_HOP_CONTROL_HOST, _HOP_CONTROL_PORT, None)


def test_proxied_eval_egress_mismatch_raises_without_eval_and_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T-11-01 parity: a bypassed proxy (WebView egress != self-probe expected) raises
    # BEFORE the eval runs — a wrong-IP (re-challenged) session must NOT be eval'd —
    # and finally STILL clears the device proxy + idles the hop.
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body="198.51.100.99",  # WebView egress != expected → bypass
        repoint_calls=repoint_calls,
        expected_ip=_EXPECTED_IP,
    )

    with pytest.raises(SolveError):
        pipeline.eval_in_webview(
            "https://mangadot.net/", "mangadot.net", "extract()", proxy=_PROXY
        )

    assert device.proxy_cleared == 1  # cleared in finally despite the raise
    assert repoint_calls[-1] == (_HOP_CONTROL_HOST, _HOP_CONTROL_PORT, None)


def test_eval_proxy_cleared_even_when_eval_raises_midflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Req 5 parity: a raise AFTER the proxy was set (here the hydration wait blows up)
    # must STILL clear the device proxy in finally.
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
    )

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("hydration wait exploded")

    monkeypatch.setattr(pipeline, "_wait_for_hydration", boom)

    with pytest.raises(RuntimeError):
        pipeline.eval_in_webview(
            "https://mangadot.net/", "mangadot.net", "x()", proxy=_PROXY
        )

    assert device.proxy_set == [(_RESOLVED_HOP_IP, _HOP_PORT)]
    assert device.proxy_cleared == 1  # cleared despite the mid-flight raise


def test_no_proxy_eval_sets_no_device_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D-08: a no-proxy /eval sets NO device proxy, repoints NO hop, runs NO
    # egress-verify — byte-for-byte the pre-Phase-14 eval path.
    device = FakeDevice()
    repoint_calls: list[tuple[str, int, str | None]] = []
    pipeline = _build_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        egress_body=_EXPECTED_IP,
        repoint_calls=repoint_calls,
    )

    pipeline.eval_in_webview("https://mangadot.net/", "mangadot.net", "extract()")

    assert device.proxy_set == []  # no global http_proxy set
    assert device.proxy_cleared == 0  # nothing to clear
    assert repoint_calls == []  # hop never repointed
    assert device.events == []  # no proxy + no tap on the eval path


# ── 260710-ig1: shared-hop cross-lane serialization (the egress-clobber fix) ─────────


def test_proxied_eval_holds_hop_lock_across_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxied eval owns the process-wide ``_HOP_LOCK`` across its
    repoint→self-probe→drive section, so a concurrent lane cannot repoint the shared hop
    between this eval's self-probe and its WebView egress read. Assert the lock is HELD
    at the self-probe point (and released after)."""
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch, device=device, clock=FakeClock(), egress_body=_EXPECTED_IP
    )
    locked_at_probe: dict[str, bool] = {}

    def probe(*_a: object, **_k: object) -> str:
        locked_at_probe["v"] = service._HOP_LOCK.locked()
        return _EXPECTED_IP

    monkeypatch.setattr(service, "expected_egress", probe)
    pipeline.eval_in_webview(
        "https://mangadot.net/", "mangadot.net", "extract()", proxy=_PROXY
    )
    assert locked_at_probe["v"] is True  # self-probe ran inside the hop lock
    assert service._HOP_LOCK.locked() is False  # released in the finally


def test_hop_lock_serializes_concurrent_proxied_evals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared ``_HOP_LOCK`` serializes proxied ops ACROSS lanes: while lane A owns
    the hop section, lane B (its own device/pipeline) BLOCKS before its repoint +
    device-proxy set — so B can never repoint the hop mid-A (the cross-lane clobber that
    failed egress-verify). Deterministic via events (no reliance on scheduling)."""
    dev_a = FakeDevice()
    dev_b = FakeDevice()
    pipe_a = _build_pipeline(
        monkeypatch, device=dev_a, clock=FakeClock(), egress_body=_EXPECTED_IP
    )
    pipe_b = _build_pipeline(
        monkeypatch, device=dev_b, clock=FakeClock(), egress_body=_EXPECTED_IP
    )
    a_in_section = threading.Event()
    release_a = threading.Event()
    call_n = {"n": 0}

    # Patched AFTER both builds (each sets expected_egress to a lambda) so this wins.
    # Lane A (first self-probe) parks INSIDE the hop lock until released; B returns.
    def probe(*_a: object, **_k: object) -> str:
        call_n["n"] += 1
        if call_n["n"] == 1:
            a_in_section.set()
            release_a.wait(timeout=5.0)
        return _EXPECTED_IP

    monkeypatch.setattr(service, "expected_egress", probe)

    def run(pipe: AndroidSolvePipeline) -> None:
        pipe.eval_in_webview(
            "https://mangadot.net/", "mangadot.net", "extract()", proxy=_PROXY
        )

    ta = threading.Thread(target=run, args=(pipe_a,))
    ta.start()
    assert a_in_section.wait(timeout=5.0)  # A is parked inside the hop lock

    tb = threading.Thread(target=run, args=(pipe_b,))
    tb.start()
    time.sleep(0.2)  # give B ample time to reach — and BLOCK on — the hop lock
    # B is blocked ACQUIRING the lock: it has not repointed the hop nor set its device
    # proxy (both live inside the locked section, after the acquire).
    assert dev_b.proxy_set == []
    assert dev_b.events == []

    release_a.set()  # A finishes, releases the lock → B proceeds
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)
    assert not ta.is_alive() and not tb.is_alive()
    # Both lanes ultimately set + cleared their own device proxy (B only after A freed
    # the hop) — never overlapping.
    assert dev_a.proxy_set == [(_RESOLVED_HOP_IP, _HOP_PORT)]
    assert dev_b.proxy_set == [(_RESOLVED_HOP_IP, _HOP_PORT)]
    assert service._HOP_LOCK.locked() is False


def test_hop_lock_queued_cancel_unwinds_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxied op QUEUED on the hop lock (another lane holds it) with a SET cancel
    unwinds promptly (SolveCancelled) — it does NOT wait the active lane's full budget
    (CodeRabbit #350). Nothing on the device is touched (no repoint / proxy set)."""
    monkeypatch.setattr(service, "_HOP_LOCK_ACQUIRE_POLL_S", 0.01)  # fast poll for test
    device = FakeDevice()
    pipeline = _build_pipeline(
        monkeypatch, device=device, clock=FakeClock(), egress_body=_EXPECTED_IP
    )
    cancel = threading.Event()
    cancel.set()  # the request was cancelled while queued on the lock
    service._HOP_LOCK.acquire()  # simulate the ACTIVE lane owning the hop
    try:
        with pytest.raises(service.SolveCancelled):
            pipeline.eval_in_webview(
                "https://mangadot.net/", "mangadot.net", "x()", cancel, proxy=_PROXY
            )
    finally:
        service._HOP_LOCK.release()
    # Unwound at the cooperative acquire — never repointed the hop nor set the proxy.
    assert device.proxy_set == []
    assert device.events == []
