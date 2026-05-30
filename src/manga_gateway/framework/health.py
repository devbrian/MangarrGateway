"""Per-source health / consecutive-failure breaker (D-36/D-38, RESEARCH Pattern 6).

A lifespan-owned, plain (non-async) counter injected by reference into the parts
that observe a source's health. The breaker OPENS (``is_enabled`` → False) after N
consecutive failures and RESETS on the next success — so a transient Cloudflare
wobble self-heals while a hard-down source stops being advertised
(``source_cap().enabled`` reads this, D-38).

``force_disabled`` is the D-33 eager-launch-failed escape hatch: if the eager
Patchright launch at startup fails, the source is held disabled regardless of the
failure counter until it is cleared.

No asyncio primitives: the counter mutations are synchronous and cheap, and the
single-process model (R1) means there is no cross-process contention to guard.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("manga_gateway.framework.health")


class SourceHealth:
    """Consecutive-failure breaker for one source (D-36)."""

    def __init__(self, threshold: int) -> None:
        """Args: ``threshold`` — N consecutive failures that OPEN the breaker."""
        self.threshold = threshold
        self.consecutive_failures = 0
        self.force_disabled = False

    def record_failure(self) -> None:
        """Count one consecutive failure toward the breaker threshold."""
        was_enabled = self.is_enabled
        self.consecutive_failures += 1
        # #21: log only the level-edge (breaker TRIP), not every counted failure
        # — the latter would be tied to retry storms and add noise.
        if was_enabled and not self.is_enabled:
            _log.warning(
                "health breaker TRIPPED after %d consecutive failures (threshold=%d)",
                self.consecutive_failures,
                self.threshold,
            )

    def record_success(self) -> None:
        """Reset the consecutive-failure counter (the breaker self-heals)."""
        was_tripped = not self.is_enabled and not self.force_disabled
        self.consecutive_failures = 0
        if was_tripped:
            _log.info("health breaker RECOVERED")

    @property
    def is_enabled(self) -> bool:
        """False once the breaker is OPEN (``>= threshold``) or force-disabled."""
        if self.force_disabled:
            return False
        return self.consecutive_failures < self.threshold
