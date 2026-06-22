"""AndroidSolvePipeline ``/eval`` cleared+hydrated-page guarantee (Phase 14, bug 3).

Drives the REAL ``AndroidSolvePipeline.eval_in_webview`` control flow with fakes — no
adb / redroid / WebView / network. Bug 3: ``/eval`` used to blindly ``Page.navigate``
the challenge_url, which reloads the page and re-triggers comix's MANAGED Cloudflare
challenge; ``/eval`` (unlike ``/solve``) never taps Turnstile, so every comix eval ran
against the "Just a moment..." interstitial and returned empty. The fix makes ``/eval``
self-sufficient:

  * FAST PATH — when the warm WebView is ALREADY on a cleared, hydrated comix page (the
    spike-021 path the prior ``/solve`` leaves alive), eval DIRECTLY with NO re-nav and
    NO tap (the reload is what re-triggers the challenge).
  * CLEAR-IF-CHALLENGED — when a re-nav is required (wrong page, or the proxied path's
    egress-verify navigated the page away) and comix re-challenges, clear the
    interstitial with the SAME ``/solve`` tap machinery BEFORE hydrating + evaluating.

These assert: the fast path issues no Page.navigate and no tap; the challenged path
navigates, taps to clear, then evals; proxy parity (set → egress-verify → re-establish
cleared page → eval → device proxy cleared in finally) holds; and the js / proxy creds /
cf_clearance token are never logged.
"""

from __future__ import annotations

import logging

import pytest
from android_solver.cdp import ClearanceCookie
from android_solver.service import AndroidSolvePipeline

from android_solver import service

_HOP_HOST = "android-solver"
_HOP_PORT = 18081
_RESOLVED_HOP_IP = "172.27.0.3"
_EXPECTED_IP = "203.0.113.7"
_PROXY = {"server": "http://up.example:8080", "username": "u", "password": "p"}


class FakeClock:
    """Deterministic monotonic clock; ``sleep`` advances ``now``."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDevice:
    """Records taps + proxy set/clear over the AdbDevice surface the eval path uses."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.proxy_set: list[tuple[str, int]] = []
        self.proxy_cleared = 0
        self.removed_forwards: list[int] = []
        self.events: list[str] = []

    def connect(self) -> None:
        return None

    def set_global_http_proxy(self, host: str, port: int) -> None:
        self.proxy_set.append((host, port))
        self.events.append("set_proxy")

    def clear_global_http_proxy(self) -> None:
        self.proxy_cleared += 1
        self.events.append("clear_proxy")

    def pidof(self) -> int:
        return 4321

    def forward_devtools(self, pid: int) -> int:
        return 9222

    def remove_forward(self, local_port: int | None = None) -> None:
        self.removed_forwards.append(local_port if local_port is not None else 9222)

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


class FakeCdp:
    """Configurable CDP fake: scripts the eval-path probes + records navigations.

    ``location_host`` answers the fast-path ``location.host`` probe; ``env_ready`` the
    ``env-*.js`` SPA-readiness probe; ``cf_present`` whether ``Page.getFrameTree`` shows
    the ``challenges.cloudflare.com`` OOPIF (a CF interstitial). ``egress_body`` answers
    the proxied egress read. Every ``Page.navigate`` URL is recorded so a test can
    assert the fast path issues NONE.
    """

    def __init__(
        self,
        *,
        location_host: object,
        env_ready: bool,
        cf_present: bool,
        egress_body: object = None,
        eval_value: object = "RESULT",
    ) -> None:
        self.location_host = location_host
        self.env_ready = env_ready
        self.cf_present = cf_present
        self.egress_body = egress_body
        self.eval_value = eval_value
        self.navigations: list[str] = []

    def __call__(self, ws, method, params=None, *, command_id):  # type: ignore[no-untyped-def]
        if method == "Page.navigate":
            self.navigations.append(params["url"])
            return {}
        if method == "Page.getFrameTree":
            tree: dict[str, object] = {
                "frame": {"url": "https://comix.to/", "id": "main"},
                "childFrames": [],
            }
            if self.cf_present:
                tree["childFrames"] = [
                    {
                        "frame": {
                            "url": "https://challenges.cloudflare.com/cdn-cgi/x",
                            "id": "cf",
                        },
                        "childFrames": [],
                    }
                ]
            return {"frameTree": tree}
        if method == "Runtime.evaluate":
            if command_id == service._EVAL_LOCATION_CMD_ID:
                return {"result": {"value": self.location_host}}
            if command_id == service._EVAL_HYDRATION_CMD_ID:
                return {"result": {"value": self.env_ready}}
            if command_id == service._EVAL_WAIT_FOR_CMD_ID:
                return {"result": {"value": True}}
            if command_id == service._EVAL_CMD_ID:
                return {"result": {"value": self.eval_value}}
            if command_id == service._EGRESS_READ_CMD_ID:
                return {"result": {"value": self.egress_body}}
            # _in_verification (300), _compute_scales (30): not a verifying banner.
            return {}
        # Page.enable / DOM.enable.
        return {}


def _eval_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    device: FakeDevice,
    clock: FakeClock,
    fake: FakeCdp,
    tap: bool = False,
    proxy: bool = False,
    token: str = "CF_TOKEN",
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
    monkeypatch.setattr(service, "cdp_call", fake)
    if tap:
        # The clear-if-challenged path reuses /solve's tap machinery. Return no token
        # on the FIRST poll so a tap actually fires, then mint — exactly the /solve
        # seam. _wait_for_cf_frame / _compute_scales are covered by their own units.
        monkeypatch.setattr(service, "locate_checkbox", lambda ws, **k: (50, 100))
        state = {"calls": 0}

        def fake_extract(ws_url, host):  # type: ignore[no-untyped-def]
            state["calls"] += 1
            if state["calls"] < 2:
                return None
            return ClearanceCookie(value=token, expires=2000000000.0)

        monkeypatch.setattr(service, "extract_clearance", fake_extract)
        monkeypatch.setattr(
            pipeline,
            "_wait_for_cf_frame",
            lambda ws, ws_url, host, cancel=None: None,
        )
        monkeypatch.setattr(pipeline, "_compute_scales", lambda ws: (2.0, 2.586))
    if proxy:
        monkeypatch.setattr(service, "expected_egress", lambda *a, **k: _EXPECTED_IP)
        monkeypatch.setattr(service, "repoint", lambda *a, **k: None)
        monkeypatch.setattr(
            service.socket, "gethostbyname", lambda host: _RESOLVED_HOP_IP
        )
    return pipeline


# ── FAST PATH: warm page already cleared+hydrated → no re-nav, no tap ──────────


def test_already_hydrated_eval_skips_nav_and_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The /solve before this eval left the WebView on the cleared, hydrated comix page
    # (location host matches, env-*.js present, NOT a CF interstitial). The eval runs
    # DIRECTLY — no Page.navigate (which would reload and re-trigger the challenge) and
    # no tap.
    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to",
        env_ready=True,
        cf_present=False,
        eval_value={"chapters": [1, 2, 3]},
    )
    pipeline = _eval_pipeline(monkeypatch, device=device, clock=FakeClock(), fake=fake)

    value = pipeline.eval_in_webview("https://comix.to/", "comix.to", "extract()")

    assert value == {"chapters": [1, 2, 3]}
    assert fake.navigations == []  # bug 3 fast path: NO needless re-navigation
    assert device.taps == []  # already cleared: NO tap


# ── Bug 5 follow-on #3: with_clearance → eval ALSO extracts the cf_clearance ─────


def test_eval_with_clearance_returns_value_and_minted_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``with_clearance=True``: after the clear+eval the pipeline reads the host-scoped
    cf_clearance from the WebView jar (the cookie comix's managed challenge auto-issued)
    and returns ``(value, SolveResult)`` — so the gateway can mint+hold comix's
    replayable clearance with NO destructive ``/solve``."""
    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to",
        env_ready=True,
        cf_present=False,
        eval_value={"chapters": [1, 2, 3]},
    )
    pipeline = _eval_pipeline(monkeypatch, device=device, clock=FakeClock(), fake=fake)
    monkeypatch.setattr(
        service,
        "extract_clearance",
        lambda ws_url, host: ClearanceCookie(value="MINTED-COMIX", expires=2.0e9),
    )
    monkeypatch.setattr(
        service, "webview_user_agent", lambda url, *, http_get: "Android WebView UA"
    )

    outcome = pipeline.eval_in_webview(
        "https://comix.to/", "comix.to", "extract()", with_clearance=True
    )

    value, clearance = outcome
    assert value == {"chapters": [1, 2, 3]}  # the value is unchanged
    assert fake.navigations == []  # fast path: no needless re-nav
    assert clearance is not None
    assert clearance.cf_clearance == "MINTED-COMIX"
    assert clearance.user_agent == "Android WebView UA"
    assert clearance.cf_clearance_expires == 2.0e9
    assert clearance.host == "comix.to"
    assert clearance.egress_ip == ""  # no-proxy path


def test_eval_with_clearance_returns_none_when_no_host_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean clear that deposited NO host-scoped cookie returns ``(value, None)`` —
    the gateway treats that as mint-not-ready and retries (never a bogus clearance)."""
    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to", env_ready=True, cf_present=False, eval_value="DATA"
    )
    pipeline = _eval_pipeline(monkeypatch, device=device, clock=FakeClock(), fake=fake)
    monkeypatch.setattr(service, "extract_clearance", lambda ws_url, host: None)

    outcome = pipeline.eval_in_webview(
        "https://comix.to/", "comix.to", "x()", with_clearance=True
    )

    value, clearance = outcome
    assert value == "DATA"
    assert clearance is None


def test_eval_without_with_clearance_returns_bare_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (``with_clearance=False``): the eval returns the bare value (no tuple),
    and ``extract_clearance`` is NEVER consulted — unchanged from before #3."""
    extract_calls = {"n": 0}

    def _never(ws_url, host):  # type: ignore[no-untyped-def]
        extract_calls["n"] += 1
        return None

    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to", env_ready=True, cf_present=False, eval_value="DATA"
    )
    pipeline = _eval_pipeline(monkeypatch, device=device, clock=FakeClock(), fake=fake)
    monkeypatch.setattr(service, "extract_clearance", _never)

    value = pipeline.eval_in_webview("https://comix.to/", "comix.to", "x()")

    assert value == "DATA"
    assert extract_calls["n"] == 0  # no clearance extract on an ordinary eval


# ── CLEAR-IF-CHALLENGED: re-nav lands on the interstitial → tap to clear → eval ─


def test_challenged_eval_navigates_clears_then_evals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The warm page is a CF interstitial (cf_present): the fast path correctly declines
    # it, the eval navigates the challenge_url, clears the interstitial with the SAME
    # /solve tap machinery, hydrates, then evals.
    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to",
        env_ready=True,
        cf_present=True,
        eval_value="DATA",
    )
    pipeline = _eval_pipeline(
        monkeypatch, device=device, clock=FakeClock(), fake=fake, tap=True
    )

    value = pipeline.eval_in_webview("https://comix.to/", "comix.to", "extract()")

    assert value == "DATA"
    assert fake.navigations == ["https://comix.to/"]  # re-nav happened
    assert device.taps == [(50, 100)]  # cleared via the solve tap machinery


def test_challenged_eval_raises_when_clear_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the interstitial never clears before the deadline, the eval must NOT run
    # against a re-challenged page — it raises (no empty/garbage result).
    device = FakeDevice()
    fake = FakeCdp(location_host="comix.to", env_ready=True, cf_present=True)
    pipeline = _eval_pipeline(
        monkeypatch, device=device, clock=FakeClock(), fake=fake, tap=True
    )
    # Clearance never mints → _tap_until_cleared returns None → clear raises.
    monkeypatch.setattr(service, "extract_clearance", lambda ws_url, host: None)

    with pytest.raises(service.SolveError):
        pipeline.eval_in_webview("https://comix.to/", "comix.to", "extract()")


def test_wrong_host_eval_navigates_then_evals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The warm WebView is on a DIFFERENT page (location host mismatch) and not
    # challenged — the fast path declines (wrong host), the eval navigates to the
    # challenge_url, finds no interstitial (nothing to clear), hydrates, then evals.
    device = FakeDevice()
    fake = FakeCdp(
        location_host="about:blank",
        env_ready=True,
        cf_present=False,
        eval_value="OK",
    )
    pipeline = _eval_pipeline(
        monkeypatch, device=device, clock=FakeClock(), fake=fake, tap=True
    )

    value = pipeline.eval_in_webview("https://comix.to/", "comix.to", "extract()")

    assert value == "OK"
    assert fake.navigations == ["https://comix.to/"]  # re-nav for the wrong page
    assert device.taps == []  # not challenged ⇒ no tap


# ── BUG 5 collision-recovery: a clean re-nav must NOT burn the frame-poll deadline ─


def test_clean_renav_short_circuits_frame_poll_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug 5 collision-recovery: when a foreign background solve steals the shared device
    # mid comix sequence, comix's recovery re-navigates — but its warm cf_clearance is
    # usually still valid, so the re-nav loads a CLEAN hydrated comix page and the CF
    # OOPIF NEVER appears. _clear_challenge_if_present must NOT block the full ~20s
    # frame-poll deadline waiting for a frame that won't come; it short-circuits the
    # instant the page is hydrated-and-CF-free (nothing to clear, no tap).
    device = FakeDevice()
    clock = FakeClock()
    fake = FakeCdp(location_host="comix.to", env_ready=True, cf_present=False)
    pipeline = _eval_pipeline(monkeypatch, device=device, clock=clock, fake=fake)
    ws = FakeWs()

    start = clock.now
    pipeline._clear_challenge_if_present(
        ws,  # type: ignore[arg-type]
        "ws://localhost:9222/p",
        "comix.to",
        None,
        clock.now + 60.0,
    )
    elapsed = clock.now - start

    assert device.taps == []  # clean page ⇒ nothing to clear, no tap
    # Did NOT grind the full ~20s frame-poll deadline (the old vestigial wait).
    assert elapsed < service._FRAME_POLL_TIMEOUT_S
    # Short-circuited on the first poll (no sleep) — collision recovery is now fast.
    assert elapsed <= pipeline._frame_poll_interval_s


def test_challenged_renav_still_detects_and_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The short-circuit must NOT skip a REAL challenge: a CF interstitial after the
    # re-nav is still detected and cleared with the /solve tap machinery.
    device = FakeDevice()
    clock = FakeClock()
    fake = FakeCdp(location_host="comix.to", env_ready=True, cf_present=True)
    pipeline = _eval_pipeline(
        monkeypatch, device=device, clock=clock, fake=fake, tap=True
    )
    ws = FakeWs()

    pipeline._clear_challenge_if_present(
        ws,  # type: ignore[arg-type]
        "ws://localhost:9222/p",
        "comix.to",
        None,
        clock.now + 60.0,
    )

    assert device.taps == [(50, 100)]  # the genuine challenge was cleared via the tap


# ── PROXY PARITY: egress-verify nav-away → re-establish cleared page → eval ────


def test_proxied_eval_clears_challenge_after_egress_navaway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Req 7 parity: the proxied eval sets the device proxy, egress-verifies (which
    # navigates the page AWAY to the ip echo), then re-navigates to the challenge_url
    # under the verified egress, clears the fresh challenge with the tap machinery,
    # evals, and clears the device proxy in finally.
    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to",
        env_ready=True,
        cf_present=True,
        egress_body=_EXPECTED_IP,  # WebView observed == self-probe expected
        eval_value="OK",
    )
    pipeline = _eval_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        fake=fake,
        tap=True,
        proxy=True,
    )

    value = pipeline.eval_in_webview(
        "https://comix.to/", "comix.to", "x()", proxy=_PROXY
    )

    assert value == "OK"
    # Device proxy set (resolved hop IP) then cleared in finally (Req 5).
    assert device.proxy_set == [(_RESOLVED_HOP_IP, _HOP_PORT)]
    assert device.proxy_cleared == 1
    # The clear/hydrate happened AFTER the egress-verify nav-away: the ip echo nav
    # then the challenge re-nav, with a tap to clear the re-challenged load.
    assert fake.navigations == [service._EGRESS_ECHO_URL, "https://comix.to/"]
    assert device.taps == [(50, 100)]


def test_proxied_eval_egress_mismatch_raises_before_nav_and_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bypassed proxy (WebView egress != self-probe expected) raises BEFORE any re-nav
    # or tap — a wrong-IP (re-challenged) session must never be eval'd — and finally
    # still clears the device proxy.
    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to",
        env_ready=True,
        cf_present=True,
        egress_body="198.51.100.99",  # != expected → bypass
        eval_value="OK",
    )
    pipeline = _eval_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        fake=fake,
        tap=True,
        proxy=True,
    )

    with pytest.raises(service.SolveError):
        pipeline.eval_in_webview("https://comix.to/", "comix.to", "x()", proxy=_PROXY)

    assert device.taps == []  # never tapped a wrong-IP session
    assert device.proxy_cleared == 1  # cleared in finally
    # Only the ip echo nav happened; never re-navigated to the challenge.
    assert fake.navigations == [service._EGRESS_ECHO_URL]


# ── SECURITY: js / proxy creds / cf_clearance token are NEVER logged ──────────


def test_eval_never_logs_js_proxy_or_clearance(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = FakeDevice()
    fake = FakeCdp(
        location_host="comix.to",
        env_ready=True,
        cf_present=True,
        egress_body=_EXPECTED_IP,
        eval_value="OK",
    )
    pipeline = _eval_pipeline(
        monkeypatch,
        device=device,
        clock=FakeClock(),
        fake=fake,
        tap=True,
        proxy=True,
        token="SECRET_CF_TOKEN_VALUE",
    )
    secret_proxy = {
        "server": "http://up.example:8080",
        "username": "u",
        "password": "s3cr3t_pr0xy_passw0rd",
    }

    with caplog.at_level(logging.DEBUG, logger="android_solver"):
        pipeline.eval_in_webview(
            "https://comix.to/",
            "comix.to",
            "SECRET_JS_EXPRESSION_TOKEN()",
            proxy=secret_proxy,
        )

    blob = " ".join(record.getMessage() for record in caplog.records)
    assert "SECRET_JS_EXPRESSION_TOKEN" not in blob  # T-14-04: js never logged
    assert "SECRET_CF_TOKEN_VALUE" not in blob  # clearance value never logged
    assert "s3cr3t_pr0xy_passw0rd" not in blob  # proxy creds never logged
