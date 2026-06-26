"""Per-source pinned residential proxy for the search + CF-solve legs (Phase 16).

``SourcePinnedProxies`` is the R1 singleton that holds ONE residential
:class:`~manga_gateway.framework.proxy_pool.PooledProxy` pinned per ``source_key``
and delegates selection / rotation / cooldown to the shared
:class:`~manga_gateway.framework.proxy_pool.ProxyPool` it is constructed with. It is
the SEARCH/SOLVE analog of the per-JOB ``_sticky_proxy`` orchestration inside
``SourceContext.fetch_image_via_pool`` (the image leg) — but keyed per-SOURCE on a
single shared singleton rather than per-job, so the pin OUTLIVES the per-request
``SourceContext`` rebuild (D-04: the pinned IP must be the SAME IP the source's
``cf_clearance`` was minted on — it pairs with the AndroidSolver ``_held[source_key]``
clearance store; debug session ``mangadot-turnstile-cf``).

PROXY-04/PROXY-05/PROXY-06: a source opts into search+solve proxying with one
declarative flag (``Source.solve_search_via_proxy_pool``, Plan 01) and the framework
(Plan 02) pins one proxy per source via this singleton, rotates to a DIFFERENT proxy
(excluding tried ones, +cooldown on the old one) when the origin reputation-blocks the
pinned IP, and surfaces a terminal signal when the pool is exhausted.

SECURITY (T-4im-01, D-05, R1): this module unpacks NO secret — the one new
secret-unpack site is :meth:`PooledProxy.as_solve_dict`, not here. Rotation logs ride
``ProxyPool.mark_failed`` which emits ``PooledProxy.identity`` (``host:port``) ONLY,
NEVER credentials. The singleton holds NO transports (the ``ProxyPool`` owns those), so
it needs no ``aclose``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .proxy_pool import PooledProxy, ProxyPool


class SourcePinnedProxies:
    """``source_key -> pinned PooledProxy`` over a shared :class:`ProxyPool` (R1).

    Constructed ONCE in the app lifespan (Plan 02) and stowed on ``app.state``; ``deps``
    accessors only READ it (PLAT-02). A ``None`` pool (pool-disabled / regression-safe
    deploy) makes every method return ``None`` and never raise — the search/solve legs
    then egress exactly as today.
    """

    def __init__(self, pool: ProxyPool | None) -> None:
        self._pool = pool
        # source_key -> the ONE proxy currently pinned for that source. A source is
        # absent from the map until its first successful ``get_or_acquire``.
        self._pins: dict[str, PooledProxy] = {}

    def get_or_acquire(self, source_key: str) -> PooledProxy | None:
        """The pinned proxy for ``source_key`` — acquire+pin on first call (PROXY-04).

        Returns the existing pin if one is set; otherwise acquires a fresh proxy from
        the shared pool, stores it as the pin, and returns it. Returns ``None`` (and
        pins nothing) when there is no pool OR the pool is exhausted (all cooled/
        excluded).
        """
        existing = self._pins.get(source_key)
        if existing is not None:
            return existing
        if self._pool is None:
            return None
        acquired = self._pool.acquire()
        if acquired is None:
            return None
        self._pins[source_key] = acquired
        return acquired

    def current(self, source_key: str) -> PooledProxy | None:
        """Peek at the pinned proxy for ``source_key`` WITHOUT acquiring (PROXY-04).

        Returns ``None`` when nothing is pinned (or the pool is disabled).
        """
        return self._pins.get(source_key)

    def rotate(self, source_key: str, *, exclude: set[str]) -> PooledProxy | None:
        """Cool the current pin down + pin a DIFFERENT proxy for ``source_key``.

        Mirrors the per-job rotate-on-failure of ``fetch_image_via_pool`` (PROXY-05):
        the old pin (if any) is handed to ``ProxyPool.mark_failed`` (cooldown +
        identity-only log), dropped from the map, and a NEW proxy is acquired excluding
        the tried ``selection_key`` values in ``exclude``. The new proxy becomes the pin
        and is returned. On exhaustion (``acquire`` returns ``None``) or a ``None``
        pool, leaves the source UNPINNED and returns ``None`` — the terminal signal it
        surfaces (e.g. ``SourceError("source_unavailable", ...)``), never re-using a
        known-bad IP.
        """
        if self._pool is None:
            return None
        old = self._pins.pop(source_key, None)
        if old is not None:
            self._pool.mark_failed(old)  # cooldown + host:port-only log (T-4im-01)
        acquired = self._pool.acquire(exclude=exclude)
        if acquired is None:
            return None
        self._pins[source_key] = acquired
        return acquired
