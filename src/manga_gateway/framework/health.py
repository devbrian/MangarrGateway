"""Per-source health / consecutive-failure breaker (D-36/D-38, RESEARCH Pattern 6).

A lifespan-owned, plain (non-async) counter injected by reference into the parts
that observe a source's health. The breaker OPENS (``is_enabled`` → False) after N
consecutive failures and RESETS on the next success — so a transient Cloudflare
wobble self-heals while a hard-down source stops being advertised
(``source_cap().enabled`` reads this, D-38).

``force_disabled`` is the D-33 eager-launch-failed escape hatch: if the eager
Patchright launch at startup fails, the source is held disabled regardless of the
failure counter. It is no longer a one-way latch (#153): ``record_success`` clears
it, so the first genuine success (on-demand warm or a watchdog re-probe) self-heals
the source and flips ``/caps`` back to ``enabled=true`` in the same poll.

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
        """Reset the breaker on a genuine success — clearing BOTH disable paths.

        A clean success (on-demand warm, a served request, or a watchdog re-probe)
        PROVES the source works, so it must clear the consecutive-failure counter
        AND the D-33 ``force_disabled`` eager-launch latch (#153). Previously a
        cold-start eager-warm flake latched ``force_disabled`` and nothing cleared
        it, so ``/caps`` advertised the source ``enabled=false`` for up to 12h even
        though on-demand warms succeeded immediately after. Self-healing here means
        the next success re-enables the source in the same ``/caps`` poll.
        """
        was_disabled = not self.is_enabled
        self.consecutive_failures = 0
        self.force_disabled = False
        if was_disabled:
            _log.info("health breaker RECOVERED")

    @property
    def is_enabled(self) -> bool:
        """False once the breaker is OPEN (``>= threshold``) or force-disabled."""
        if self.force_disabled:
            return False
        return self.consecutive_failures < self.threshold
