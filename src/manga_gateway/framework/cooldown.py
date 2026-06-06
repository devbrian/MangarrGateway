"""Per-source failure cooldown — a negative cache for FAILURES (260606-lyb Change 2).

Distinct from the enumeration cache (``framework/enum_cache.py``), which caches
successful enumerations: this is the source-wide negative cache for HARD failures.
When a source's fan-out branch hits a timeout / ``SourceError`` / 5xx / transport /
unexpected error, ``fan_out`` records a cooldown for that source key; while the
cooldown is live, a subsequent identical search SKIPS the upstream call entirely and
surfaces a ``source_unavailable`` warning — so a repeat search for a DOWN source
returns instantly (zero upstream requests, no retries, no browser navs, no timeout).

* Keyed on ``source_key`` ONLY — source-wide, NOT per-query. One failing source is
  suppressed across every query for the cooldown window.
* TTL comes from config (``GATEWAY_SOURCE_FAILURE_COOLDOWN_SECONDS``, default 300s);
  ``0`` DISABLES the cooldown entirely (every ``in_cooldown`` returns ``False``).
* MONOTONIC clock (injectable for tests) so wall-clock jumps never extend/shorten a
  cooldown.
* Single-process (R1), in-memory, plain (non-async) — synchronous mutations like
  ``framework/health.py``; no asyncio primitives, no cross-process contention to guard.
* A successful return (including a 200-empty / zero-results) NEVER records a cooldown
  — ``fan_out`` only reaches ``record_failure`` on an except branch (the success and
  cooldown-skip returns happen inside the ``try``).
"""

from __future__ import annotations

import time
from collections.abc import Callable


class SourceFailureCooldown:
    """Process-lived per-source failure cooldown (negative cache for failures)."""

    def __init__(
        self, *, ttl_seconds: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Args: ``ttl_seconds`` — cooldown window (``<=0`` disables); ``clock`` —
        injectable monotonic clock for tests."""
        self._ttl = ttl_seconds
        self._clock = clock
        # source_key → monotonic expiry timestamp.
        self._until: dict[str, float] = {}

    def record_failure(self, source_key: str) -> None:
        """Open (or refresh) the cooldown for ``source_key`` (source-wide).

        A no-op when disabled (``ttl <= 0``). Called by ``fan_out`` ONLY on a
        hard-failure branch — never on a successful (incl. 200-empty) return.
        """
        if self._ttl <= 0:
            return
        self._until[source_key] = self._clock() + self._ttl

    def in_cooldown(self, source_key: str) -> bool:
        """True iff ``source_key`` has a live (non-expired) cooldown.

        Always ``False`` when disabled (``ttl <= 0``) or when no failure was
        recorded. Pops an expired entry to keep the dict bounded.
        """
        if self._ttl <= 0:
            return False
        expiry = self._until.get(source_key)
        if expiry is None:
            return False
        if self._clock() < expiry:
            return True
        # Expired: drop the stale entry so the dict stays bounded.
        self._until.pop(source_key, None)
        return False

    def remaining(self, source_key: str) -> float:
        """Seconds left on ``source_key``'s cooldown (``0.0`` if none/expired/disabled).

        Test/introspection helper — never drives control flow."""
        if self._ttl <= 0:
            return 0.0
        expiry = self._until.get(source_key)
        if expiry is None:
            return 0.0
        return max(0.0, expiry - self._clock())
