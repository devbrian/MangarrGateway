"""Per-source rate-limiter seam.

Thin wrapper over ``aiolimiter.AsyncLimiter`` (token bucket). One limiter per
``sourceKey``, keyed to that source's ``rateLimitPerMinute`` from ``/caps``.
Phase 1 establishes the seam; no limits are exercised until Phase 2.
"""

from __future__ import annotations

from aiolimiter import AsyncLimiter


class RateLimiter:
    """Lazily provisions one ``AsyncLimiter`` per source key (Phase 2 use)."""

    def __init__(self) -> None:
        self._limiters: dict[str, AsyncLimiter] = {}

    def for_source(self, source_key: str, rate_per_minute: int) -> AsyncLimiter:
        """Return (creating if needed) the limiter for ``source_key``."""
        limiter = self._limiters.get(source_key)
        if limiter is None:
            limiter = AsyncLimiter(rate_per_minute, time_period=60)
            self._limiters[source_key] = limiter
        return limiter
