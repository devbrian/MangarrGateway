"""Env-driven settings for the android-solver sidecar control API.

The sidecar reads ALL of its configuration from the process environment at
startup — nothing is baked into the image (SEC-01, threat T-10-10). The api key
and the redroid adb target arrive at runtime via the compose env (10-05); the
image carries no secret literal.

R1: this module imports ONLY the stdlib — never anything from
``src/manga_gateway``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# Env var names (the compose service in 10-05 supplies these).
_ENV_API_KEY = "SOLVER_API_KEY"
_ENV_ADB_TARGET = "SOLVER_ADB_TARGET"
# LANE-03: the FULL multi-target registry (comma list). One android-solver
# addresses N adb targets; absent ⇒ the registry collapses to the single
# SOLVER_ADB_TARGET (byte-for-byte today).
_ENV_ADB_TARGETS = "SOLVER_ADB_TARGETS"
_ENV_ALLOWED_HOSTS = "SOLVER_ALLOWED_HOSTS"
# SEC-01: optional per-target challenge-host allowlist scoping. A JSON object
# mapping an adb target -> a host (or comma-list / JSON list of hosts) so a
# dedicated lane can be scoped to ONLY its own source's host. Any target named
# here MUST be a member of SOLVER_ADB_TARGETS (fail loud at from_env).
_ENV_ALLOWED_HOSTS_BY_TARGET = "SOLVER_ALLOWED_HOSTS_BY_TARGET"
_ENV_TIMEOUT = "SOLVER_SOLVE_TIMEOUT_S"
# Debug ``kagane-search-timeout`` (defensive fix part 1): the INNER locate→tap→poll
# deadline that bounds ``_drive_solve`` (and the frame-readiness wait), separate from
# the base ``solve_timeout_s`` (which still caps the EVAL path + the outer worker
# wait). During a Cloudflare block-page window kagane.to serves a "you have been
# blocked" page instead of a solvable Turnstile, so the checkbox locator finds nothing
# and the loop used to poll to the full ~120s ``solve_timeout_s`` — blowing the
# gateway's 30s search fan-out budget. Max observed SUCCESSFUL solve is 11.2s and the
# fastest in-window FAILURE is 21s (a clean gap), so a ~13s inner deadline aborts ZERO
# good solves while turning each block-window failure from ~120s into ~13s.
_ENV_TAP_POLL_DEADLINE = "SOLVER_TAP_POLL_DEADLINE_S"
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
# The ONLY challenge hosts the sidecar will ever drive a device against — the
# strict-Turnstile sources proven by the debug spike. A request for any other
# host is rejected before any device action (SSRF guard, T-10-09). mangaball.net
# joined 2026-06-15 (#243) after its managed-challenge escalation proved unclearable
# by desktop Chromium from our Linux fingerprint (CI + deploy both timed out).
_DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("mangadot.net", "kagane.to", "mangaball.net")
_DEFAULT_TIMEOUT_S = 120.0
# Debug ``kagane-search-timeout`` part 1: 13s = just above the 11.2s max successful
# solve and below the 21s fastest in-window failure (the empirical gap), so it aborts
# zero good solves while capping a block-window failure at ~13s. MUST stay below
# ``proxy_solve_timeout_s`` (the outer proxied budget) — enforced in ``from_env``.
_DEFAULT_TAP_POLL_DEADLINE_S = 13.0
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
    # The DEFAULT target — used when a /solve|/eval request omits ``target``
    # (byte-for-byte today's single-device behavior, LANE-03).
    adb_target: str = _DEFAULT_ADB_TARGET
    # The FULL registry the sidecar drives (LANE-03). Always contains
    # ``adb_target``; a single-target deploy collapses to ``(adb_target,)``.
    adb_targets: tuple[str, ...] = (_DEFAULT_ADB_TARGET,)
    allowed_hosts: frozenset[str] = field(default=frozenset(_DEFAULT_ALLOWED_HOSTS))
    # SEC-01: per-target allowlist overrides. Empty ⇒ every target uses the
    # global ``allowed_hosts`` (see :meth:`allowed_hosts_for`).
    allowed_hosts_by_target: Mapping[str, frozenset[str]] = field(default_factory=dict)
    solve_timeout_s: float = _DEFAULT_TIMEOUT_S
    # Inner locate→tap→poll deadline (debug ``kagane-search-timeout`` part 1). Bounds
    # ``_drive_solve``'s frame-readiness wait + tap loop INDEPENDENTLY of the base
    # ``solve_timeout_s`` (which still caps the eval path + the outer worker wait).
    tap_poll_deadline_s: float = _DEFAULT_TAP_POLL_DEADLINE_S
    cancel_grace_s: float = _DEFAULT_CANCEL_GRACE_S
    bind_host: str = _DEFAULT_BIND_HOST
    port: int = _DEFAULT_PORT
    proxy_solve_timeout_s: float = _DEFAULT_PROXY_TIMEOUT_S
    hop_port: int = _DEFAULT_HOP_PORT
    hop_host: str = _DEFAULT_HOP_HOST

    def allowed_hosts_for(self, target: str) -> frozenset[str]:
        """Return the challenge-host allowlist for ``target`` (SEC-01).

        A target with a per-target override (``SOLVER_ALLOWED_HOSTS_BY_TARGET``)
        is scoped to only those hosts; every other target falls back to the
        global ``allowed_hosts``. This keeps a dedicated lane from driving
        another source's host while leaving the default deploy untouched.
        """
        return self.allowed_hosts_by_target.get(target, self.allowed_hosts)

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
        adb_targets = _parse_targets(source.get(_ENV_ADB_TARGETS), adb_target)

        allowed_hosts = _parse_hosts(source.get(_ENV_ALLOWED_HOSTS))
        allowed_hosts_by_target = _parse_allowed_hosts_by_target(
            source.get(_ENV_ALLOWED_HOSTS_BY_TARGET), adb_targets
        )

        solve_timeout_s = _parse_positive_float(
            source.get(_ENV_TIMEOUT), _DEFAULT_TIMEOUT_S, _ENV_TIMEOUT
        )

        tap_poll_deadline_s = _parse_positive_float(
            source.get(_ENV_TAP_POLL_DEADLINE),
            _DEFAULT_TAP_POLL_DEADLINE_S,
            _ENV_TAP_POLL_DEADLINE,
        )

        cancel_grace_s = _parse_positive_float(
            source.get(_ENV_CANCEL_GRACE), _DEFAULT_CANCEL_GRACE_S, _ENV_CANCEL_GRACE
        )

        bind_host = (source.get(_ENV_BIND_HOST) or _DEFAULT_BIND_HOST).strip()
        port = _parse_port(source.get(_ENV_PORT), _DEFAULT_PORT, _ENV_PORT)

        proxy_solve_timeout_s = _parse_positive_float(
            source.get(_ENV_PROXY_TIMEOUT), _DEFAULT_PROXY_TIMEOUT_S, _ENV_PROXY_TIMEOUT
        )
        # Debug ``kagane-search-timeout`` part 1: the proxied solve adds a CONNECT-hop +
        # egress-verify BEFORE the tap loop, so its OUTER budget MUST stay above the
        # inner tap-poll deadline — otherwise a misconfig could abort a proxied solve
        # at the outer wait before the inner loop even runs. Fail loud at startup.
        if proxy_solve_timeout_s <= tap_poll_deadline_s:
            raise ConfigError(
                f"{_ENV_PROXY_TIMEOUT} ({proxy_solve_timeout_s}) must be greater than "
                f"{_ENV_TAP_POLL_DEADLINE} ({tap_poll_deadline_s}) — the proxied outer "
                "budget has to exceed the inner locate→tap→poll deadline"
            )
        hop_port = _parse_port(
            source.get(_ENV_HOP_PORT), _DEFAULT_HOP_PORT, _ENV_HOP_PORT
        )
        hop_host = (source.get(_ENV_HOP_HOST) or _DEFAULT_HOP_HOST).strip()

        return cls(
            api_key=api_key,
            adb_target=adb_target,
            adb_targets=adb_targets,
            allowed_hosts=allowed_hosts,
            allowed_hosts_by_target=allowed_hosts_by_target,
            solve_timeout_s=solve_timeout_s,
            tap_poll_deadline_s=tap_poll_deadline_s,
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


def _parse_targets(raw: str | None, default_target: str) -> tuple[str, ...]:
    """Parse the comma-separated multi-target registry (LANE-03).

    Reuses the comma-split/strip idiom from :func:`_parse_hosts`. When unset or
    all-blank, the registry collapses to ``(default_target,)`` so a single-target
    deploy is byte-for-byte today. The DEFAULT target is always a member: it is
    prepended when the explicit list omits it, with order otherwise preserved.
    """
    targets: list[str] = []
    if raw and raw.strip():
        for part in raw.split(","):
            value = part.strip()
            if value and value not in targets:
                targets.append(value)
    if not targets:
        return (default_target,)
    if default_target not in targets:
        targets.insert(0, default_target)
    return tuple(targets)


def _parse_allowed_hosts_by_target(
    raw: str | None, adb_targets: tuple[str, ...]
) -> dict[str, frozenset[str]]:
    """Parse the optional per-target allowlist override map (SEC-01).

    ``raw`` is a JSON object mapping an adb target -> a host string (comma list)
    OR a JSON list of hosts. Raises ``ConfigError`` on malformed JSON, a
    non-object payload, an unparseable value, or a target NOT present in
    ``adb_targets`` (fail loud — never scope an allowlist to a target the sidecar
    does not drive).
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"{_ENV_ALLOWED_HOSTS_BY_TARGET} must be a JSON object, got {raw!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"{_ENV_ALLOWED_HOSTS_BY_TARGET} must be a JSON object mapping "
            f"target -> hosts, got {type(parsed).__name__}"
        )

    result: dict[str, frozenset[str]] = {}
    for target, value in parsed.items():
        if target not in adb_targets:
            raise ConfigError(
                f"{_ENV_ALLOWED_HOSTS_BY_TARGET} names target {target!r} which is "
                f"not in {_ENV_ADB_TARGETS} ({list(adb_targets)})"
            )
        if isinstance(value, str):
            hosts = {part.strip().lower() for part in value.split(",") if part.strip()}
        elif isinstance(value, list):
            hosts = {str(part).strip().lower() for part in value if str(part).strip()}
        else:
            raise ConfigError(
                f"{_ENV_ALLOWED_HOSTS_BY_TARGET}[{target!r}] must be a host string "
                f"or a list of hosts, got {type(value).__name__}"
            )
        if not hosts:
            raise ConfigError(
                f"{_ENV_ALLOWED_HOSTS_BY_TARGET}[{target!r}] resolved to an empty "
                "allowlist"
            )
        result[target] = frozenset(hosts)
    return result


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
