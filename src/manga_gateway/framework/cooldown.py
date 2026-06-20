"""Per-source failure cooldown — a negative cache for FAILURES (260606-lyb Change 2).

Distinct from the enumeration cache (``framework/enum_cache.py``), which caches
successful enumerations: this is the source-wide negative cache for HARD failures.
When a source's fan-out branch hits a timeout / ``SourceError`` / 5xx / transport /
unexpected error, ``fan_out`` records a failure for that source key; while a cooldown
is live, a subsequent identical search SKIPS the upstream call entirely and surfaces a
``source_unavailable`` warning — so a repeat search for a DOWN source returns instantly
(zero upstream requests, no retries, no browser navs, no timeout).

Cooldown grows with the CONSECUTIVE-failure streak (260620 rework) instead of jumping
straight to a flat window — a single blip should not sideline a source for minutes:

* Failure 1 records the streak but opens NO cooldown — the source stays live and is
  retried on the very next search.
* Failure 2+ opens an exponential-backoff cooldown ``min(base * 2**(streak-2), max)``,
  e.g. base 30s / max 600s → 30, 60, 120, 240, 480, 600 (10-min cap) for streaks 2..7.
* ``record_success`` clears BOTH the streak and any live cooldown, so a recovered
  source starts fresh ("failure 1 = live") next time.

* Keyed on ``source_key`` ONLY — source-wide, NOT per-query. One failing source is
  suppressed across every query while its cooldown is live.
* Knobs come from config: ``GATEWAY_SOURCE_FAILURE_COOLDOWN_SECONDS`` is the MAX cap
  (default 600s) and ``GATEWAY_SOURCE_FAILURE_COOLDOWN_BASE_SECONDS`` the first
  backoff step (default 30s). Setting EITHER to ``0`` DISABLES the cooldown entirely
  (every ``in_cooldown`` returns ``False``).
* MONOTONIC clock (injectable for tests) so wall-clock jumps never extend/shorten a
  cooldown.
* Single-process (R1), in-memory, plain (non-async) — synchronous mutations like
  ``framework/health.py``; no asyncio primitives, no cross-process contention to guard.
* A successful return (including a 200-empty / zero-results) NEVER records a failure —
  ``fan_out`` calls ``record_success`` on the success path and ``record_failure`` only
  on an except branch (the success and cooldown-skip returns happen inside the ``try``).
"""

from __future__ import annotations

import time
from collections.abc import Callable


class SourceFailureCooldown:
    """Process-lived per-source failure cooldown with consecutive-failure backoff."""

    def __init__(
        self,
        *,
        base_seconds: int,
        max_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Args: ``base_seconds`` — the first backoff step (cooldown opened on the
        2nd consecutive failure); ``max_seconds`` — the cooldown cap; ``clock`` —
        injectable monotonic clock for tests. The cooldown is DISABLED (every
        ``in_cooldown`` → ``False``) when either ``base_seconds`` or ``max_seconds``
        is ``<= 0`` — ``0`` is the documented disable value for both knobs.
        """
        self._base = base_seconds
        self._max = max_seconds
        self._enabled = base_seconds > 0 and max_seconds > 0
        self._clock = clock
        # source_key → monotonic expiry timestamp (only set once streak >= 2).
        self._until: dict[str, float] = {}
        # source_key → consecutive hard-failure count (reset by record_success).
        self._streak: dict[str, int] = {}

    def record_failure(self, source_key: str) -> None:
        """Record a hard failure for ``source_key`` and grow its backoff cooldown.

        A no-op when disabled. Called by ``fan_out`` ONLY on a hard-failure branch —
        never on a successful (incl. 200-empty) return. The FIRST consecutive failure
        opens no cooldown (the source stays live); the 2nd onward opens a cooldown of
        ``min(base * 2**(streak-2), max)`` seconds.
        """
        if not self._enabled:
            return
        streak = self._streak.get(source_key, 0) + 1
        self._streak[source_key] = streak
        if streak < 2:
            # First failure: stay live — one blip never sidelines a source.
            return
        duration = min(self._base * (2 ** (streak - 2)), self._max)
        self._until[source_key] = self._clock() + duration

    def record_success(self, source_key: str) -> None:
        """Clear ``source_key``'s failure streak AND any live cooldown.

        A no-op when disabled. Called by ``fan_out`` on the success return path so a
        recovered source resets to "failure 1 = live" rather than carrying forward an
        escalated backoff.
        """
        if not self._enabled:
            return
        self._streak.pop(source_key, None)
        self._until.pop(source_key, None)

    def in_cooldown(self, source_key: str) -> bool:
        """True iff ``source_key`` has a live (non-expired) cooldown.

        Always ``False`` when disabled or when no cooldown is open (incl. after only a
        single failure). Pops an expired ``_until`` entry to keep the dict bounded, but
        leaves the streak intact so the next failure keeps escalating until a success.
        """
        if not self._enabled:
            return False
        expiry = self._until.get(source_key)
        if expiry is None:
            return False
        if self._clock() < expiry:
            return True
        # Expired: drop the stale expiry so the dict stays bounded. The streak is
        # deliberately retained — escalation persists until record_success.
        self._until.pop(source_key, None)
        return False

    def remaining(self, source_key: str) -> float:
        """Seconds left on ``source_key``'s cooldown (``0.0`` if none/expired/disabled).

        Test/introspection helper — never drives control flow."""
        if not self._enabled:
            return 0.0
        expiry = self._until.get(source_key)
        if expiry is None:
            return 0.0
        return max(0.0, expiry - self._clock())
