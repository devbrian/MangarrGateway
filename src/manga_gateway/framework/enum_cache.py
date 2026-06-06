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

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_DEFAULT_TTL = 1800  # 30 min — ≤ the 60-min handle TTL (D-09, blueprint R5)
_DEFAULT_MAXSIZE = 512  # D-07 memory bound
_MAX_TTL = 3600  # hard ceiling = the 60-min handle TTL (D-09)


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


class EnumerationCache:
    """Source-agnostic two-layer cache. One instance per process (R1)."""

    @staticmethod
    def enum_key(source_key: str, series_id: str, languages: list[str]) -> tuple:
        """Layer-2 key (CACHE-03). Excludes ``type``/``chapter`` so a manga search
        and its chapter follow-ups share one entry (T-09-01)."""
        return (source_key, series_id, tuple(sorted(languages)))

    @staticmethod
    def resolve_key(
        source_key: str, normalized_query: str, languages: list[str]
    ) -> tuple:
        """Layer-1 key (D-01) — title/query → resolved series-id list."""
        return (source_key, normalized_query, tuple(sorted(languages)))
