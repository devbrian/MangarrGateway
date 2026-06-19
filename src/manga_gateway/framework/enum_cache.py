"""The UNIVERSAL source-agnostic enumeration cache (framework facility, CACHE-01).

ONE mechanism, written once in the framework, that every source opts into the same
way the rate-limiter / ``ctx.get_json`` are opted into. It is NOT source-specific —
each source supplies only the cache key + the fetch closure.

Design (carried from spikes 006–008, blueprint
``.claude/skills/spike-findings-mangarrgateway/references/search-result-caching.md``):

  * Two layers behind ONE :class:`EnumerationCache`:
    - **Layer 1** (``cached_resolve``) — title/query → resolved series-id list.
    - **Layer 2** (``cached_enumerate``) — the chapter-feed enumeration per series.
    Each layer is an independent :class:`SingleFlightCache` (separate ``_cache`` +
    ``_inflight``) sharing the same TTL / maxsize / kill-switch / TTL-override config.
  * Cache the UNFILTERED enumeration (pre-``_to_release``, pre-``chapter_matches``).
    The floor filter stays in ``search()`` AFTER this call (CACHE-02) — never moves.
  * Key on enumeration inputs only: ``(source_key, series_id, sorted(languages))``.
    NEVER ``type`` / ``chapter`` — so a manga search and its chapter follow-ups
    share one entry (T-09-01).
  * The cache check happens BEFORE ``fetch_fn`` runs; ``fetch_fn`` is what acquires
    the rate-limiter and calls upstream, so a HIT costs no upstream call and no
    rate-limit token (CACHE-03).
  * :class:`SingleFlightCache` mirrors ``BrowserLifecycle.solve`` (D-04/D-05): N
    concurrent misses on the SAME key collapse to ONE fetch; a fetch error sets the
    future's exception, is NEVER cached, and the ``finally`` pops ``_inflight[key]``
    so the next caller re-fetches (T-09-03).
  * Store COMPLETENESS metadata (:class:`Enumeration`) so a source can make a
    correct serve-vs-refetch decision for chapter searches (CACHE-04).
  * Emits a redacted, failure-isolated ``kind="cache"`` metric event (D-06) — the
    raw query / series-id is NEVER recorded (``url=None``; only ``source_key`` +
    layer op), and a collector error never breaks the cached call.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from cachetools import TLRUCache

from ..metrics.collector import get_collector

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_DEFAULT_TTL = 1800  # 30 min — ≤ the handle TTL (D-09, blueprint R5)
_DEFAULT_MAXSIZE = 512  # D-07 memory bound
# Default hard ceiling for any cached entry's TTL = the LEGACY 60-min handle TTL
# (D-09). This is now INJECTABLE (``max_ttl``) and the lifespan passes the
# configured ``handle_ttl_seconds`` so a deliberately-raised handle TTL is not
# silently clamped back to 60 min (debug session release-no-longer-resolvable).
# The 3600 default is retained only as the safe fallback for callers that do not
# pass a ceiling (tests / direct construction).
_MAX_TTL = 3600

# IN-01: distinct absence sentinel. ``self._cache.get(key)`` defaults to ``None``,
# but ``None`` is a value a future ``fetch_fn`` could legitimately return — under an
# ``is not None`` presence check that value would be indistinguishable from absence
# (re-fetched every call, always classified "miss"). A unique object can never
# collide with any cached value, so it cleanly means "not present".
_MISSING = object()

# The uniform cache-key shape ALL layers use: a heterogeneous tuple whose first
# element is the ``source_key`` (read by ``_ttu`` for per-source TTL overrides).
type CacheKey = tuple[Any, ...]


@dataclass(frozen=True)
class Enumeration:
    """A cached chapter enumeration + the completeness facts a source needs.

    ``items`` are RAW upstream rows (dicts / source-native objects) — NOT Releases
    and NOT handles. Handles are minted on the way out of ``search()`` per serve
    (fresh-handle-per-serve, CACHE-03/05) via the EXISTING ``HandleStore`` — this
    cache never stores minted handles.
    """

    items: list[Any]
    chapter_numbers: tuple[Decimal, ...]
    exhausted: bool  # upstream returned fewer than requested → we have the whole feed
    requested_limit: int

    def covers_floor(self, chapter: float) -> bool:
        """Can a ``type=chapter`` floor query for ``chapter`` be answered
        confidently from THIS cached window?

        - If the feed was exhausted, we have everything → always yes.
        - Else yes iff the requested floor is within the cached window's
          ``[min_floor, max_floor]``. A floor below the window means older chapters
          we never fetched → the source must fetch deeper (refetch), NOT serve a
          false-empty. This is the decision the engine-level cache could not make.
        """
        if self.exhausted:
            return True
        if not self.chapter_numbers:
            return False
        lo = math.floor(min(self.chapter_numbers))
        hi = math.floor(max(self.chapter_numbers))
        return lo <= math.floor(chapter) <= hi


class SingleFlightCache[V]:
    """A generic single-flight TTL store (PEP-695 generic, UP047 style).

    Mirrors ``BrowserLifecycle.solve`` (D-04/D-05): the cache HIT is the fast path;
    concurrent misses on the SAME key collapse onto ONE in-flight future; a fetch
    error sets that future's exception, is NEVER cached, and the ``finally`` pops
    ``_inflight[key]`` so the next caller re-fetches (T-09-03).

    Backed by :class:`cachetools.TLRUCache` so each entry's expiry can honor a
    per-source TTL override (``ttl_overrides[key[0]]``); every entry's TTL is
    clamped to ``_MAX_TTL`` (the 60-min handle TTL, D-09). ``maxsize`` bounds the
    entry count (D-07/T-09-05). When ``enabled`` is ``False`` (the D-08 kill-switch)
    ``get_or_fetch`` short-circuits to a bare ``await fetch_fn()`` — no cache read,
    no cache write, no in-flight future.
    """

    def __init__(
        self,
        *,
        ttl: int = _DEFAULT_TTL,
        maxsize: int = _DEFAULT_MAXSIZE,
        enabled: bool = True,
        ttl_overrides: dict[str, int] | None = None,
        max_ttl: int = _MAX_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._default_ttl = ttl
        self._enabled = enabled
        self._ttl_overrides = ttl_overrides or {}
        self._max_ttl = max_ttl
        self._cache: TLRUCache[CacheKey, V] = TLRUCache(
            maxsize=maxsize, ttu=self._ttu, timer=clock
        )
        self._inflight: dict[CacheKey, asyncio.Future[V]] = {}

    def _ttu(self, key: CacheKey, value: V, now: float) -> float:
        """Per-entry expiry: ``now + min(override_or_default_ttl, self._max_ttl)``.

        The per-source override is keyed on ``key[0]`` (the ``source_key``); absent
        → the default ttl. Every TTL is clamped to ``self._max_ttl`` (the configured
        handle TTL, D-09) so no entry outlives the handle it serves.
        """
        source_key = key[0]
        ttl = self._ttl_overrides.get(source_key, self._default_ttl)
        return now + min(ttl, self._max_ttl)

    async def get_or_fetch(
        self, key: CacheKey, fetch_fn: Callable[[], Awaitable[V]]
    ) -> V:
        """Return the cached value, or run ``fetch_fn`` (which owns the rate-limiter
        + upstream call) on a miss and cache it.

        The cache check is BEFORE ``fetch_fn`` — a HIT touches no network and no
        limiter (CACHE-03).
        """
        if not self._enabled:
            # D-08 kill-switch: bare fetch, no cache state, no single-flight.
            return await fetch_fn()

        # Fast path: a live cache HIT returns without invoking fetch_fn. The
        # ``_MISSING`` sentinel (IN-01) distinguishes a genuinely absent key from a
        # cached ``None`` value a future ``fetch_fn`` might return.
        hit = self._cache.get(key, _MISSING)
        if hit is not _MISSING:
            return cast("V", hit)

        # Snapshot the in-flight future BEFORE awaiting so the leader's ``finally``
        # cannot pop it between the .done() check and the await (single-flight race).
        inflight = self._inflight.get(key)
        if inflight is not None and not inflight.done():
            return await inflight

        # Become the single-flight leader: publish a future others collapse onto.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[V] = loop.create_future()
        self._inflight[key] = future
        try:
            value = await fetch_fn()
        except BaseException as exc:  # D-05: propagate to awaiters, never cache
            if not future.done():
                future.set_exception(exc)
                # WR-02: on the common solo-miss error path nothing ever awaits this
                # future (the leader awaits ``fetch_fn`` directly; followers exist
                # only under contention), so without retrieving the exception
                # ``Future.__del__`` would log a spurious ERROR-level "Future
                # exception was never retrieved" with a traceback on every transient
                # upstream failure. Mark it retrieved to keep the failure signal clean.
                future.exception()
            raise
        else:
            # 260606-lyb Change 3 (deliberate WR-03 reversal, commit aef08ce):
            # successful results — INCLUDING empties (an empty candidate list or an
            # Enumeration with no items) — ARE cached for the full TTL. A repeat
            # NO-MATCH search now short-circuits with zero upstream calls. Hard
            # failures are STILL never cached (the ``except`` branch above is
            # untouched), and a DOWN source never reaches this success branch (it
            # raises → handled by the per-source failure cooldown), so we only ever
            # cache GENUINE successful empties — not a transient upstream outage.
            self._cache[key] = value
            if not future.done():
                future.set_result(value)
            return value
        finally:
            self._inflight.pop(key, None)  # ordered-finally cleanup (D-05)

    def replace(self, key: CacheKey, value: V) -> None:
        """Overwrite the cached value (used by the completeness-driven refetch)."""
        self._cache[key] = value

    def contains(self, key: CacheKey) -> bool:
        """Return whether ``key`` has a live (non-expired) entry — the HIT check
        used to classify a cache event before ``get_or_fetch`` runs."""
        return self._cache.get(key, _MISSING) is not _MISSING


def _emit_cache(*, op: str, outcome: str, source_key: str | None) -> None:
    """No-op-safe, failure-isolated ``emit_cache`` (D-06).

    Mirrors ``_emit_solve`` in ``solver_lifecycle.py``: a ``None`` collector is a
    no-op and a collector-side error never breaks the cached call.
    """
    collector = get_collector()
    if collector is None:
        return
    try:
        collector.emit_cache(op=op, outcome=outcome, source_key=source_key)
    except Exception:  # noqa: BLE001 — a metric failure must never break a search
        pass


class EnumerationCache:
    """Source-agnostic two-layer cache. One instance per process (R1).

    Composes two independent :class:`SingleFlightCache` layers from the SAME
    ``ttl`` / ``maxsize`` / ``enabled`` / ``ttl_overrides`` config (D-03 — Layer 1
    shares Layer 2's staleness window):

    * ``_resolve`` (Layer 1, ``op="resolve"``) — title/query → resolved series-id.
    * ``_enum`` (Layer 2, ``op="enumerate"``) — the chapter-feed enumeration.

    Each ``cached_*`` call emits a redacted, failure-isolated ``kind="cache"`` event
    (``hit`` when the layer already holds the key, else ``miss`` after the fetch);
    ``cache_replace`` emits ``refetch``. Nothing is emitted (and nothing is cached)
    when the kill-switch is off (D-08).
    """

    def __init__(
        self,
        *,
        ttl: int = _DEFAULT_TTL,
        maxsize: int = _DEFAULT_MAXSIZE,
        enabled: bool = True,
        ttl_overrides: dict[str, int] | None = None,
        max_ttl: int = _MAX_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._enum: SingleFlightCache[Enumeration] = SingleFlightCache(
            ttl=ttl,
            maxsize=maxsize,
            enabled=enabled,
            ttl_overrides=ttl_overrides,
            max_ttl=max_ttl,
            clock=clock,
        )
        self._resolve: SingleFlightCache[Any] = SingleFlightCache(
            ttl=ttl,
            maxsize=maxsize,
            enabled=enabled,
            ttl_overrides=ttl_overrides,
            max_ttl=max_ttl,
            clock=clock,
        )

    async def cached_enumerate(
        self, key: CacheKey, fetch_fn: Callable[[], Awaitable[Enumeration]]
    ) -> Enumeration:
        """Layer-2 chapter-feed enumeration (CACHE-03). Emits enumerate hit/miss."""
        if not self._enabled:
            return await self._enum.get_or_fetch(key, fetch_fn)
        outcome = "hit" if self._enum.contains(key) else "miss"
        result = await self._enum.get_or_fetch(key, fetch_fn)
        _emit_cache(op="enumerate", outcome=outcome, source_key=key[0])
        return result

    async def cached_resolve(
        self, key: CacheKey, fetch_fn: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Layer-1 title/query → series-id resolution (D-01). Emits resolve hit/miss."""
        if not self._enabled:
            return await self._resolve.get_or_fetch(key, fetch_fn)
        outcome = "hit" if self._resolve.contains(key) else "miss"
        result = await self._resolve.get_or_fetch(key, fetch_fn)
        _emit_cache(op="resolve", outcome=outcome, source_key=key[0])
        return result

    def cache_replace(self, key: CacheKey, enum: Enumeration) -> None:
        """Replace the Layer-2 window after a completeness-driven deeper refetch
        (CACHE-04). Emits ``op="enumerate", outcome="refetch"``."""
        if not self._enabled:
            return
        self._enum.replace(key, enum)
        _emit_cache(op="enumerate", outcome="refetch", source_key=key[0])

    @staticmethod
    def enum_key(
        source_key: str,
        series_id: str,
        languages: list[str],
        *,
        extra: Any = None,
    ) -> CacheKey:
        """Layer-2 key (CACHE-03). Excludes ``type``/``chapter`` so a manga search
        and its chapter follow-ups share one entry (T-09-01).

        ``extra`` is an optional, source-supplied discriminator appended ONLY when
        non-``None`` so the historic 3-tuple shape is preserved for sources that do
        not vary their enumeration by anything beyond ``(series_id, languages)``.
        Sources whose cached enumeration depends on pagination inputs pass the
        window (e.g. MangaDex's ``(stop_floor, offset)``) here so a HIT can never
        serve a stale window (CR-01). Language-AGNOSTIC sources (Comix, MangaBall,
        Mangadot) pass ``languages=[]`` so different-language requests for one title
        share the cached walk (IN-02)."""
        base = (source_key, series_id, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)

    @staticmethod
    def resolve_key(
        source_key: str,
        normalized_query: str,
        languages: list[str],
        *,
        extra: Any = None,
    ) -> CacheKey:
        """Layer-1 key (D-01) — title/query → resolved series-id list.

        ``extra`` carries a source-supplied discriminator (appended ONLY when
        non-``None`` to preserve the historic 3-tuple shape) for sources whose
        resolved candidate set depends on more than ``(query, languages)`` — e.g.
        the interactive vs default candidate count on MangaDex/Comix (WR-01)."""
        base = (source_key, normalized_query, tuple(sorted(languages)))
        return base if extra is None else (*base, extra)
