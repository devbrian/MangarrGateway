"""Env-driven settings for the android-solver sidecar control API.

The sidecar reads ALL of its configuration from the process environment at
startup — nothing is baked into the image (SEC-01, threat T-10-10). The api key
and the redroid adb target arrive at runtime via the compose env (10-05); the
image carries no secret literal.

R1: this module imports ONLY the stdlib — never anything from
``src/manga_gateway``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# Env var names (the compose service in 10-05 supplies these).
_ENV_API_KEY = "SOLVER_API_KEY"
_ENV_ADB_TARGET = "SOLVER_ADB_TARGET"
_ENV_ALLOWED_HOSTS = "SOLVER_ALLOWED_HOSTS"
_ENV_TIMEOUT = "SOLVER_SOLVE_TIMEOUT_S"
_ENV_CANCEL_GRACE = "SOLVER_CANCEL_GRACE_S"
_ENV_BIND_HOST = "SOLVER_BIND_HOST"
_ENV_PORT = "SOLVER_PORT"
# Per-solve proxy knobs (Phase 11, D-07 + Runtime State). A proxied solve does a
# real cross-container CONNECT hop + egress-verify, so it is allowed a LONGER
# budget than the base no-proxy solve (whose timeout is unchanged, D-08).
_ENV_PROXY_TIMEOUT = "SOLVER_PROXY_SOLVE_TIMEOUT_S"
# The hop proxy the device routes WebView egress through. ``hop_host`` is the
# docker-network-reachable sidecar SERVICE name (NOT localhost — a loopback value
# resolves to the redroid container and bypasses the hop, Pitfall 1 / T-11-05).
_ENV_HOP_PORT = "SOLVER_HOP_PORT"
_ENV_HOP_HOST = "SOLVER_HOP_HOST"

_DEFAULT_ADB_TARGET = "redroid:5555"
# The ONLY challenge hosts the sidecar will ever drive a device against — the two
# strict-Turnstile sources proven by the debug spike. A request for any other
# host is rejected before any device action (SSRF guard, T-10-09).
_DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("mangadot.net", "kagane.to")
_DEFAULT_TIMEOUT_S = 120.0
# Bounded cleanup window (issue #207 / WR-02): when a solve exceeds the timeout
# its worker thread CANNOT be force-killed, so the service signals cooperative
# cancellation and waits at most this long for the worker to abort at its next
# adb/CDP checkpoint, reset the device, and exit — so the NEXT /solve starts on a
# clean device instead of queuing behind a still-running orphan. Held under the
# service lock, so it also caps how long the next caller can be delayed.
_DEFAULT_CANCEL_GRACE_S = 20.0
# Bind 0.0.0.0 INSIDE the container; the compose service publishes NO host port
# (10-05), so the control API is reachable only on the docker-internal network
# (the gateway is the only caller — T-10-12). Mirrors the gateway image's
# GATEWAY_HOST=0.0.0.0 + no-published-port stance.
_DEFAULT_BIND_HOST = "0.0.0.0"  # noqa: S104 — docker-internal only; no published port (T-10-12)
_DEFAULT_PORT = 8080
# Higher than _DEFAULT_TIMEOUT_S — a proxied solve adds CONNECT-hop + verify cost.
_DEFAULT_PROXY_TIMEOUT_S = 240.0
_DEFAULT_HOP_PORT = 18081
# The redroid-reachable sidecar service name (compose service, 10-05). NOT localhost.
_DEFAULT_HOP_HOST = "android-solver"


class ConfigError(RuntimeError):
    """A required setting is missing or malformed — the service refuses to start."""


@dataclass(frozen=True)
class SidecarConfig:
    """Immutable sidecar settings (the api key + adb target + SSRF allowlist)."""

    api_key: str
    adb_target: str = _DEFAULT_ADB_TARGET
    allowed_hosts: frozenset[str] = field(default=frozenset(_DEFAULT_ALLOWED_HOSTS))
    solve_timeout_s: float = _DEFAULT_TIMEOUT_S
    cancel_grace_s: float = _DEFAULT_CANCEL_GRACE_S
    bind_host: str = _DEFAULT_BIND_HOST
    port: int = _DEFAULT_PORT
    proxy_solve_timeout_s: float = _DEFAULT_PROXY_TIMEOUT_S
    hop_port: int = _DEFAULT_HOP_PORT
    hop_host: str = _DEFAULT_HOP_HOST

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SidecarConfig:
        """Build the config from ``env`` (defaults to ``os.environ``).

        Raises ``ConfigError`` when the api key is absent — the sidecar must
        never run unauthenticated (T-10-08).
        """
        source = env if env is not None else os.environ

        api_key = (source.get(_ENV_API_KEY) or "").strip()
        if not api_key:
            raise ConfigError(
                f"{_ENV_API_KEY} is required — the sidecar refuses to start "
                "without an api key (no unauthenticated /solve, T-10-08)."
            )

        adb_target = (source.get(_ENV_ADB_TARGET) or _DEFAULT_ADB_TARGET).strip()

        allowed_hosts = _parse_hosts(source.get(_ENV_ALLOWED_HOSTS))

        solve_timeout_s = _parse_positive_float(
            source.get(_ENV_TIMEOUT), _DEFAULT_TIMEOUT_S, _ENV_TIMEOUT
        )

        cancel_grace_s = _parse_positive_float(
            source.get(_ENV_CANCEL_GRACE), _DEFAULT_CANCEL_GRACE_S, _ENV_CANCEL_GRACE
        )

        bind_host = (source.get(_ENV_BIND_HOST) or _DEFAULT_BIND_HOST).strip()
        port = _parse_port(source.get(_ENV_PORT), _DEFAULT_PORT, _ENV_PORT)

        proxy_solve_timeout_s = _parse_positive_float(
            source.get(_ENV_PROXY_TIMEOUT), _DEFAULT_PROXY_TIMEOUT_S, _ENV_PROXY_TIMEOUT
        )
        hop_port = _parse_port(
            source.get(_ENV_HOP_PORT), _DEFAULT_HOP_PORT, _ENV_HOP_PORT
        )
        hop_host = (source.get(_ENV_HOP_HOST) or _DEFAULT_HOP_HOST).strip()

        return cls(
            api_key=api_key,
            adb_target=adb_target,
            allowed_hosts=allowed_hosts,
            solve_timeout_s=solve_timeout_s,
            cancel_grace_s=cancel_grace_s,
            bind_host=bind_host,
            port=port,
            proxy_solve_timeout_s=proxy_solve_timeout_s,
            hop_port=hop_port,
            hop_host=hop_host,
        )


def _parse_hosts(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated host allowlist; fall back to the default pair."""
    if not raw or not raw.strip():
        return frozenset(_DEFAULT_ALLOWED_HOSTS)
    hosts = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return frozenset(hosts) if hosts else frozenset(_DEFAULT_ALLOWED_HOSTS)


def _parse_positive_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {value}")
    return value


def _parse_port(
    raw: str | None, default: int = _DEFAULT_PORT, name: str = _ENV_PORT
) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not (1 <= port <= 65535):
        raise ConfigError(f"{name} must be in 1..65535, got {port}")
    return port
