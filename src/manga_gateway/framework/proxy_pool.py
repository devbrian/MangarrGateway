"""Reusable residential proxy pool for source image-byte fetches (260620-4im).

The pool is a FRAMEWORK capability: a source opts in with one base-class flag
(``Source.image_fetch_via_proxy_pool = True``) and the framework routes ONLY that
source's ``fetch_image`` byte fetches through a rotating pool of operator-supplied
residential proxies — per-job sticky, retry-with-a-different-proxy on failure, and a
cooldown on the failed proxy. Search and the read-page Cloudflare solve stay DIRECT.

MangaFire is the first opt-in: its ``{prefix}.mfcdnN.xyz`` image-CDN zones IP-ban the
gateway's direct egress (verified 403 on all zones), and a clean residential proxy
returns ``200 image/jpeg``. The next image-CDN source enables it with a single flag.

Layering (locked): the proxy is the OUTER, per-job-sticky dimension; a source's own
per-page logic (e.g. MangaFire's ``mfcdnN`` zone-rewrite) runs INNER — one proxy is
held for the whole inner fetch and is rotated only when the whole inner fetch fails.

SECURITY (T-4im-01): a proxy password is a ``pydantic.SecretStr``;
``get_secret_value()`` is unpacked ONLY inside :meth:`PooledProxy.httpx_proxy`, ridden
on ``httpx.Proxy(auth=...)`` so it never enters a URL string. Logs / observability emit
``host:port`` identity only (:attr:`PooledProxy.identity`), NEVER credentials. Mirrors
``framework.proxy.build_proxy``.
"""

from __future__ import annotations

import logging
import random
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import SecretStr

from .transport import HttpxTransport, Transport

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config import Settings

_log = logging.getLogger("manga_gateway")

# Module-level default RNG so a pool built without an explicit ``rng`` still randomizes
# proxy selection (tests inject a deterministic ``random.Random(seed)`` or a stub).
_DEFAULT_RNG = random.Random()


@dataclass(frozen=True)
class PooledProxy:
    """One residential proxy endpoint (host:port [+ optional creds]).

    ``identity`` (``host:port``) is the ONLY value ever logged — it carries no
    credentials. ``password`` is a ``SecretStr`` so it auto-redacts in ``repr``/``str``;
    its plaintext is unpacked ONLY in :meth:`httpx_proxy`.
    """

    host: str
    port: int
    username: str | None = None
    password: SecretStr | None = None

    @property
    def identity(self) -> str:
        """Credential-free ``host:port`` identity — the only value ever logged."""
        return f"{self.host}:{self.port}"

    @property
    def selection_key(self) -> str:
        """Internal bookkeeping key — credential-distinct, NEVER logged.

        Cooldown / transport-cache / exclusion state is keyed by this, NOT by
        :attr:`identity`. Residential providers often expose ONE gateway ``host:port``
        with the rotating session encoded in the *username* (sticky-session proxies), so
        two pool entries can share ``host:port`` yet be distinct egress paths. Keying
        state off ``host:port`` alone would collide them — cooling down or caching the
        transport for the wrong credential. Including the username disambiguates them.
        ``identity`` stays ``host:port`` for safe logging; this value is never emitted.
        """
        return f"{self.host}:{self.port}:{self.username or ''}"

    def httpx_proxy(self) -> httpx.Proxy | str:
        """The httpx ``proxy=`` value for this endpoint (mirrors ``build_proxy``).

        With both a username and password set, the SOLE ``get_secret_value()`` unpack
        site rides the secret on ``httpx.Proxy(auth=...)`` so the password never enters
        the URL string. Otherwise the plain ``http://{host}:{port}`` URL is returned.
        """
        if self.username is not None and self.password is not None:
            secret = self.password.get_secret_value()  # SOLE unpack site (T-4im-01)
            return httpx.Proxy(
                url=f"http://{self.host}:{self.port}",
                auth=(self.username, secret),
            )
        return f"http://{self.host}:{self.port}"


class ProxyPool:
    """Selection + cooldown + per-proxy transport cache for image-byte fetches."""

    def __init__(
        self,
        proxies: list[PooledProxy],
        *,
        settings: Settings,
        cooldown_seconds: float,
        transport_factory: Callable[..., Transport] = HttpxTransport,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._proxies = list(proxies)
        self._settings = settings
        self._cooldown_seconds = cooldown_seconds
        self._transport_factory = transport_factory
        self._rng = rng if rng is not None else _DEFAULT_RNG
        self._clock = clock
        # selection_key -> monotonic cooldown deadline (lazily popped once expired).
        # Keyed by the credential-distinct ``selection_key`` (NOT ``identity``) so two
        # sticky-session entries sharing a ``host:port`` cool down independently.
        self._cooldowns: dict[str, float] = {}
        # selection_key -> ONE cached per-proxy transport (httpx binds the proxy at
        # client construction, so a distinct client per proxy/credential is required).
        self._transports: dict[str, Transport] = {}

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        settings: Settings,
        cooldown_seconds: float,
    ) -> ProxyPool | None:
        """Parse a ``host:port:user:pass`` file → a pool, or ``None`` when unusable.

        Reads the operator-supplied proxy file (sync; startup-only). Each non-blank,
        non-``#`` line is split on ``:`` into exactly 4 parts (``host:port:user:pass``;
        a 2-part ``host:port`` no-creds line is tolerated); a malformed line (wrong part
        count or non-integer port) is skipped. Returns ``None`` when the file is absent
        OR yields zero valid proxies — the disabled / regression-safe signal (the pool
        is then never wired, so the download path egresses exactly as today).
        """
        file_path = Path(path)
        if not file_path.is_file():
            return None
        proxies: list[PooledProxy] = []
        for raw_line in file_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) == 4:
                host, port_str, user, password = parts
            elif len(parts) == 2:
                host, port_str = parts
                user, password = "", ""
            else:
                continue
            try:
                port = int(port_str)
            except ValueError:
                continue
            proxies.append(
                PooledProxy(
                    host=host,
                    port=port,
                    username=user or None,
                    password=SecretStr(password) if password else None,
                )
            )
        if not proxies:
            return None
        return cls(proxies, settings=settings, cooldown_seconds=cooldown_seconds)

    def available_count(self) -> int:
        """Total proxies loaded into the pool (the startup-log count)."""
        return len(self._proxies)

    def _on_cooldown(self, key: str) -> bool:
        """True iff ``key`` (a selection_key) is on a LIVE cooldown; pops an expired."""
        deadline = self._cooldowns.get(key)
        if deadline is None:
            return False
        if self._clock() >= deadline:
            del self._cooldowns[key]
            return False
        return True

    def acquire(self, exclude: set[str] | None = None) -> PooledProxy | None:
        """A random proxy not on cooldown and not in ``exclude``, or ``None``.

        ``exclude`` holds ``selection_key`` values (the credential-distinct bookkeeping
        key — see :attr:`PooledProxy.selection_key`), matching ``_cooldowns``. Cooldown
        is NOT bypassed as a last resort — when every proxy is either on a live cooldown
        or excluded, this returns ``None`` and the orchestrator surfaces a clear
        terminal failure rather than re-using a known-bad proxy (T-4im-03).
        """
        excluded = exclude or set()
        available = [
            proxy
            for proxy in self._proxies
            if proxy.selection_key not in excluded
            and not self._on_cooldown(proxy.selection_key)
        ]
        if not available:
            return None
        return self._rng.choice(available)

    def mark_failed(self, proxy: PooledProxy) -> None:
        """Cool ``proxy`` down for ``cooldown_seconds`` from now (identity-only log)."""
        self._cooldowns[proxy.selection_key] = self._clock() + self._cooldown_seconds
        _log.info(
            "image proxy %s cooled down for %.0fs after a failed fetch",
            proxy.identity,  # host:port only — NEVER creds (T-4im-01)
            self._cooldown_seconds,
        )

    def transport_for(self, proxy: PooledProxy) -> Transport:
        """The cached per-proxy transport, lazily built once per ``selection_key``."""
        transport = self._transports.get(proxy.selection_key)
        if transport is None:
            transport = self._transport_factory(
                self._settings, proxy_override=proxy.httpx_proxy()
            )
            self._transports[proxy.selection_key] = transport
        return transport

    async def aclose(self) -> None:
        """Close every cached per-proxy transport exactly once (best-effort)."""
        for transport in self._transports.values():
            with suppress(Exception):
                await transport.aclose()
        self._transports.clear()
