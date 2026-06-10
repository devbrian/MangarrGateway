"""AdbDevice driven against a FAKE subprocess runner — no real adb is touched.

Asserts the exact argv the unrooted driver emits (the proven EXP27 sequence),
and the locked invariant that it NEVER issues ``adb root``.
"""

from __future__ import annotations

import pytest
from android_solver.device import (
    WEBVIEW_ACTIVITY,
    WEBVIEW_PACKAGE,
    AdbDevice,
    AdbError,
    CommandResult,
)


class FakeRunner:
    """Records every argv and replays canned results by leading subcommand."""

    def __init__(self, *, stdout_for: dict[str, bytes] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._stdout_for = stdout_for or {}
        self.returncode = 0

    def __call__(self, argv: list[str], *, timeout: float) -> CommandResult:
        self.calls.append(list(argv))
        # Canned stdout keyed off whichever subcommand token appears in this argv.
        stdout = next(
            (out for key, out in self._stdout_for.items() if key in argv),
            b"",
        )
        return CommandResult(argv=list(argv), returncode=self.returncode, stdout=stdout)

    def argv_containing(self, token: str) -> list[list[str]]:
        return [c for c in self.calls if token in c]


def test_launch_url_emits_view_intent_with_url_and_activity() -> None:
    runner = FakeRunner()
    dev = AdbDevice("redroid:5555", runner=runner)

    dev.launch_url("https://mangadot.net/")

    (call,) = runner.calls
    # The exact `am start ... -d <url> -n <activity>` argv must be emitted.
    assert call == [
        "adb",
        "-s",
        "redroid:5555",
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        "https://mangadot.net/",
        "-n",
        WEBVIEW_ACTIVITY,
    ]
    assert WEBVIEW_ACTIVITY.endswith(".WebViewBrowserActivity")


def test_force_stop_and_clear_wipes_webview_package() -> None:
    runner = FakeRunner()
    dev = AdbDevice(runner=runner)

    dev.force_stop_and_clear()

    force_stop, pm_clear = runner.calls
    assert force_stop[-3:] == ["am", "force-stop", WEBVIEW_PACKAGE]
    # `pm clear org.chromium.webview_shell` — fresh cookie jar.
    assert pm_clear[-3:] == ["pm", "clear", "org.chromium.webview_shell"]


def test_connect_targets_endpoint() -> None:
    runner = FakeRunner()
    dev = AdbDevice("localhost:5555", runner=runner)
    dev.connect()
    assert runner.calls == [["adb", "connect", "localhost:5555"]]


def test_pidof_parses_first_token() -> None:
    runner = FakeRunner(stdout_for={"pidof": b"4242\n"})
    dev = AdbDevice(runner=runner)
    assert dev.pidof() == 4242


def test_pidof_raises_when_process_absent() -> None:
    runner = FakeRunner(stdout_for={"pidof": b"\n"})
    dev = AdbDevice(runner=runner)
    with pytest.raises(AdbError):
        dev.pidof()


def test_forward_devtools_names_socket_by_pid_and_returns_port() -> None:
    runner = FakeRunner()
    dev = AdbDevice(runner=runner, local_port=9333)
    port = dev.forward_devtools(4242)
    assert port == 9333
    (call,) = runner.calls
    assert call[-3:] == [
        "forward",
        "tcp:9333",
        "localabstract:webview_devtools_remote_4242",
    ]


def test_input_tap_uses_device_pixels() -> None:
    runner = FakeRunner()
    dev = AdbDevice(runner=runner)
    dev.input_tap(73, 977)
    (call,) = runner.calls
    assert call[-4:] == ["input", "tap", "73", "977"]


def test_set_global_http_proxy_emits_host_port_argv() -> None:
    runner = FakeRunner()
    dev = AdbDevice(runner=runner)

    # Host MUST be the docker-reachable sidecar name, NOT localhost (Pitfall 1).
    dev.set_global_http_proxy("android-solver", 18081)

    (call,) = runner.calls
    assert call[-6:] == [
        "shell",
        "settings",
        "put",
        "global",
        "http_proxy",
        "android-solver:18081",
    ]
    assert call[-2:] == ["http_proxy", "android-solver:18081"]
    assert "localhost" not in call[-1] and "127.0.0.1" not in call[-1]


def test_clear_global_http_proxy_emits_colon_zero_argv() -> None:
    runner = FakeRunner()
    dev = AdbDevice(runner=runner)

    dev.clear_global_http_proxy()

    (call,) = runner.calls
    assert call[-6:] == ["shell", "settings", "put", "global", "http_proxy", ":0"]


def test_screencap_returns_png_bytes() -> None:
    runner = FakeRunner(stdout_for={"exec-out": b"\x89PNG\r\n\x1a\nDATA"})
    dev = AdbDevice(runner=runner)
    assert dev.screencap().startswith(b"\x89PNG")


def test_screen_size_parses_physical_resolution() -> None:
    runner = FakeRunner(stdout_for={"wm": b"Physical size: 720x1280\n"})
    dev = AdbDevice(runner=runner)
    assert dev.screen_size() == (720, 1280)


def test_screen_size_override_wins_over_physical() -> None:
    runner = FakeRunner(
        stdout_for={"wm": b"Physical size: 720x1280\nOverride size: 1080x1920\n"}
    )
    dev = AdbDevice(runner=runner)
    assert dev.screen_size() == (1080, 1920)


def test_screen_size_raises_when_unparseable() -> None:
    runner = FakeRunner(stdout_for={"wm": b"garbage output\n"})
    dev = AdbDevice(runner=runner)
    with pytest.raises(AdbError):
        dev.screen_size()


def test_is_booted_true_when_boot_completed_is_one() -> None:
    runner = FakeRunner(stdout_for={"getprop": b"1\n"})
    dev = AdbDevice(runner=runner)
    assert dev.is_booted() is True
    (call,) = runner.argv_containing("getprop")
    assert call[-2:] == ["getprop", "sys.boot_completed"]


def test_is_booted_false_before_boot_completes() -> None:
    # getprop returns empty (or 0) until the system finishes booting.
    runner = FakeRunner(stdout_for={"getprop": b"\n"})
    dev = AdbDevice(runner=runner)
    assert dev.is_booted() is False


def test_nonzero_exit_raises_adberror() -> None:
    runner = FakeRunner()
    runner.returncode = 1
    dev = AdbDevice(runner=runner)
    with pytest.raises(AdbError):
        dev.connect()


def test_driver_never_issues_adb_root() -> None:
    """Locked invariant: the unrooted driver must NEVER emit `adb root`."""
    runner = FakeRunner(stdout_for={"pidof": b"4242\n", "exec-out": b"\x89PNG"})
    dev = AdbDevice(runner=runner)

    dev.connect()
    dev.force_stop_and_clear()
    dev.launch_url("https://kagane.to/")
    dev.pidof()
    dev.forward_devtools(4242)
    dev.input_tap(10, 20)
    dev.screencap()

    assert not any("root" in token for call in runner.calls for token in call)
