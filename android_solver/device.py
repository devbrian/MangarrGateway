"""Unrooted adb device driver for the android-solver sidecar.

Drives a redroid Android-in-Docker WebView over the ``adb`` CLI (via
``subprocess``) to mint a Cloudflare ``cf_clearance``. This mirrors the exact
command sequence proven end-to-end in debug ``mangadot-cf-linux-fingerprint``
(EXP27): connect → force-stop + ``pm clear`` (fresh cookie jar) → ``am start``
the challenge URL in the AOSP System-WebView → hardware ``input tap`` the
Turnstile checkbox → forward the devtools socket → extract ``cf_clearance``.

LANDMINE (10-CONTEXT <decisions>, threat T-10-02): this driver MUST run
UNROOTED. NEVER switch adbd into root mode — issuing that privileged restart
wedges redroid's adbd and the ONLY recovery is restarting the redroid
container. Every command below runs as the unprivileged adb shell user; there
is deliberately no privilege-escalation helper.

SSRF discipline: ``launch_url``'s ``url`` is a SIDECAR-supplied, upstream-
validated value (a source's ``cloudflare_challenge_url``, validated in 10-02) —
it is never reflected from an untrusted caller in this module.

DoS bound (threat T-10-03): every subprocess op is timeout-bounded; the
serialized per-solve cap lands in 10-02.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

# AOSP System-WebView shell — the bundled browser; no Play Store / Chrome sideload.
WEBVIEW_PACKAGE = "org.chromium.webview_shell"
WEBVIEW_ACTIVITY = f"{WEBVIEW_PACKAGE}/.WebViewBrowserActivity"

# adb-forwarded CDP devtools socket name for a given WebView pid.
_DEVTOOLS_SOCKET = "localabstract:webview_devtools_remote_{pid}"

_DEFAULT_TARGET = "localhost:5555"
_DEFAULT_LOCAL_PORT = 9222
_DEFAULT_TIMEOUT = 30.0


class AdbError(RuntimeError):
    """An adb command exited non-zero or could not be parsed."""


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one adb invocation (argv recorded for testability)."""

    argv: list[str]
    returncode: int
    stdout: bytes


class CommandRunner(Protocol):
    """Pluggable subprocess runner so tests can drive a FAKE adb (no real device)."""

    def __call__(self, argv: list[str], *, timeout: float) -> CommandResult: ...


def _parse_resolution(line: str) -> tuple[int, int] | None:
    """Parse a ``... size: WxH`` line into ``(width, height)`` ints, or None."""
    _, _, value = line.partition(":")
    width_str, sep, height_str = value.strip().partition("x")
    if not sep:
        return None
    try:
        return int(width_str), int(height_str)
    except ValueError:
        return None


def _subprocess_runner(argv: list[str], *, timeout: float) -> CommandResult:
    """Default runner: shell out to the real ``adb`` CLI, bounded by ``timeout``."""
    proc = subprocess.run(  # noqa: S603 — argv is built from constants, never a shell string
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        argv=list(argv), returncode=proc.returncode, stdout=proc.stdout
    )


class AdbDevice:
    """Unrooted adb driver targeting a redroid endpoint (default localhost:5555).

    The ``target`` is overridable so the sidecar can reach redroid by its
    docker-network host:port. Inject ``runner`` in tests to assert the exact argv
    emitted without touching a real device.
    """

    def __init__(
        self,
        target: str = _DEFAULT_TARGET,
        *,
        runner: CommandRunner | None = None,
        adb_path: str = "adb",
        local_port: int = _DEFAULT_LOCAL_PORT,
        default_timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._target = target
        self._adb = adb_path
        self._run: CommandRunner = runner or _subprocess_runner
        self._local_port = local_port
        self._timeout = default_timeout

    # -- low-level argv plumbing ------------------------------------------------

    def _exec(self, *args: str, timeout: float | None = None) -> CommandResult:
        argv = [self._adb, *args]
        result = self._run(
            argv, timeout=timeout if timeout is not None else self._timeout
        )
        if result.returncode != 0:
            raise AdbError(f"adb exited {result.returncode}: {' '.join(args[:3])}")
        return result

    def _device(self, *args: str, timeout: float | None = None) -> CommandResult:
        """Run a device-scoped adb command (``adb -s <target> ...``)."""
        return self._exec("-s", self._target, *args, timeout=timeout)

    # -- driver surface ---------------------------------------------------------

    def connect(self) -> None:
        """``adb connect <target>`` — attach to the redroid adbd endpoint."""
        self._exec("connect", self._target)

    def is_booted(self, *, timeout: float = 5.0) -> bool:
        """True once Android has finished booting (``getprop sys.boot_completed``).

        ``adb connect`` is exit-0-on-failure across several platform-tools versions
        and a TCP-reachable adbd does NOT imply Android is up enough to solve. This
        reads the canonical boot flag instead — ``sys.boot_completed == 1`` is set
        only after the system has finished booting and the WebView can run.
        """
        result = self._device("shell", "getprop", "sys.boot_completed", timeout=timeout)
        return result.stdout.decode("utf-8", "replace").strip() == "1"

    def force_stop_and_clear(self, package: str = WEBVIEW_PACKAGE) -> None:
        """Kill the WebView and wipe its data for a FRESH cookie jar."""
        self._device("shell", "am", "force-stop", package)
        self._device("shell", "pm", "clear", package)

    def launch_url(self, url: str, *, activity: str = WEBVIEW_ACTIVITY) -> None:
        """Open ``url`` in the System-WebView via an explicit VIEW intent."""
        self._device(
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            url,
            "-n",
            activity,
        )

    def pidof(self, package: str = WEBVIEW_PACKAGE) -> int:
        """Return ``package``'s pid (used to name its devtools socket)."""
        result = self._device("shell", "pidof", package)
        text = result.stdout.decode("utf-8", "replace").strip()
        first = text.split()[0] if text else ""
        if not first.isdigit():
            raise AdbError(f"pidof {package} returned no pid (process not running?)")
        return int(first)

    def forward_devtools(self, pid: int, *, local_port: int | None = None) -> int:
        """Forward ``tcp:<local_port>`` to the WebView devtools socket; return port."""
        port = local_port if local_port is not None else self._local_port
        self._device(
            "forward",
            f"tcp:{port}",
            _DEVTOOLS_SOCKET.format(pid=pid),
        )
        return port

    def remove_forward(self, local_port: int | None = None) -> None:
        """Tear down the ``tcp:<local_port>`` devtools forward (counterpart of
        :meth:`forward_devtools`).

        Runs ``adb -s <target> forward --remove tcp:<port>`` so the ESTABLISHED
        devtools forward created for a solve does not leak across solves (#275). The
        port defaults to the configured ``local_port`` — the same value
        :meth:`forward_devtools` forwards by default.
        """
        port = local_port if local_port is not None else self._local_port
        self._device("forward", "--remove", f"tcp:{port}")

    def input_tap(self, x: int, y: int) -> None:
        """Hardware touch at device pixels ``(x, y)`` — a REAL tap, not synthetic JS."""
        self._device("shell", "input", "tap", str(int(x)), str(int(y)))

    def set_global_http_proxy(self, host: str, port: int) -> None:
        """Route ALL device WebView egress through ``host:port`` (Req 2).

        ``host`` MUST be the docker-reachable sidecar service name (the caller
        passes ``config.hop_host`` = ``android-solver``), NEVER ``localhost`` /
        ``127.0.0.1`` — a loopback value resolves to the redroid container itself
        and silently bypasses the cross-container hop proxy (Pitfall 1, T-11-05).
        """
        self._device(
            "shell", "settings", "put", "global", "http_proxy", f"{host}:{int(port)}"
        )

    def clear_global_http_proxy(self) -> None:
        """Remove the device-wide proxy (``http_proxy :0``) — restore direct egress."""
        self._device("shell", "settings", "put", "global", "http_proxy", ":0")

    def screencap(self) -> bytes:
        """Grab the screen as PNG bytes (``adb exec-out screencap -p``)."""
        result = self._device("exec-out", "screencap", "-p")
        return result.stdout

    def screen_size(self) -> tuple[int, int]:
        """Return the PHYSICAL screen resolution ``(width, height)`` in pixels.

        Parses ``adb shell wm size``. Output carries a ``Physical size: WxH``
        line and, when a resolution override is active, an ``Override size: WxH``
        line — the override is what the WebView actually renders into, so it WINS
        when present. The physical pixels are the denominator-free target for
        ``input_tap`` (CDP CSS px are scaled up to these — see turnstile.py).
        """
        result = self._device("shell", "wm", "size")
        text = result.stdout.decode("utf-8", "replace")
        physical: tuple[int, int] | None = None
        override: tuple[int, int] | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("Override size:"):
                override = _parse_resolution(stripped)
            elif stripped.startswith("Physical size:"):
                physical = _parse_resolution(stripped)
        size = override or physical
        if size is None:
            raise AdbError(f"could not parse screen size from wm size: {text!r}")
        return size
