"""Per-source failure cooldown tests (260606-lyb Change 2).

Network-free. Exercises the cooldown wiring at the ``fan_out`` choke point:
  * Test 1 — a hard-failing source records a cooldown; a repeat ``fan_out`` SKIPS
    the upstream call entirely (zero additional ``run_one`` invocations) and still
    surfaces a ``source_unavailable`` warning (instant repeat search for a DOWN
    source).
  * Test 5 — a successful 200-empty return NEVER sets a cooldown (the cooldown trips
    only on the except/timeout path, never on a valid-but-empty success).
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.cooldown import SourceFailureCooldown
from manga_gateway.framework.fanout import fan_out


class _FakeSource:
    def __init__(self, key: str) -> None:
        self.key = key


@pytest.mark.asyncio
async def test_cooldown_skips_upstream_on_repeat_when_down() -> None:
    # Locked-scope invariant: a repeat identical search for a DOWN source makes ZERO
    # upstream requests for that source while in cooldown, and still surfaces its
    # warning. Proves record_failure trips on the except branch, then in_cooldown
    # short-circuits the second fan_out.
    down = _FakeSource("down")
    cd = SourceFailureCooldown(ttl_seconds=300, clock=lambda: 0.0)
    calls = 0

    async def run_one(_: _FakeSource) -> list[str]:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    # First call: run_one raises → a warning AND the cooldown is now set.
    releases1, warnings1 = await fan_out([down], run_one, cooldown=cd)
    assert releases1 == []
    assert calls == 1
    assert len(warnings1) == 1
    assert warnings1[0][0] == "down"
    assert warnings1[0][1] == "source_unavailable"
    assert cd.in_cooldown("down") is True

    # Second call: skipped — run_one is NOT invoked again (zero upstream calls),
    # result is [], and a cooldown warning is still returned.
    releases2, warnings2 = await fan_out([down], run_one, cooldown=cd)
    assert releases2 == []
    assert calls == 1  # ZERO additional invocations
    assert len(warnings2) == 1
    assert warnings2[0] == (
        "down",
        "source_unavailable",
        "upstream unavailable (cooldown)",
    )


@pytest.mark.asyncio
async def test_empty_success_never_sets_cooldown() -> None:
    # Locked-scope invariant: a successful-but-empty (200-empty / zero-results)
    # source return NEVER sets a failure cooldown — cooldown trips only on the
    # exception/timeout path. The success return happens INSIDE the try, so it never
    # reaches the trailing record_failure.
    empty = _FakeSource("empty")
    cd = SourceFailureCooldown(ttl_seconds=300, clock=lambda: 0.0)

    async def run_one(_: _FakeSource) -> list[str]:
        return []  # successful 200-empty

    releases, warnings = await fan_out([empty], run_one, cooldown=cd)
    assert releases == []
    assert warnings == []  # SRCH-04: empty success is not a warning
    assert cd.in_cooldown("empty") is False  # no cooldown on a successful empty
