"""Per-source failure cooldown tests (260606-lyb Change 2 / 260620 backoff rework).

Network-free. Exercises the cooldown wiring at the ``fan_out`` choke point plus the
consecutive-failure backoff schedule on the unit class:
  * Test 1 — a hard-failing source is NOT cooled down after a single failure (it stays
    live and is retried), but a SECOND consecutive failure opens a cooldown so the
    third ``fan_out`` SKIPS the upstream call entirely (zero additional ``run_one``
    invocations) and still surfaces a ``source_unavailable`` warning.
  * Test 5 — a successful 200-empty return NEVER sets a cooldown (the cooldown trips
    only on the except/timeout path, never on a valid-but-empty success).
  * Backoff unit tests — the 2nd+ failure escalates min(base*2**(streak-2), max), and
    record_success resets the streak so the next failure is "live" again.
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
async def test_cooldown_skips_upstream_only_after_second_failure() -> None:
    # Locked-scope invariant: ONE blip never sidelines a source — the first hard
    # failure leaves it live (retried on the next search). The SECOND consecutive
    # failure opens a backoff cooldown, after which a repeat identical search makes
    # ZERO upstream requests while in cooldown and still surfaces its warning.
    down = _FakeSource("down")
    cd = SourceFailureCooldown(base_seconds=30, max_seconds=600, clock=lambda: 0.0)
    calls = 0

    async def run_one(_: _FakeSource) -> list[str]:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    # First call: run_one raises → a warning, streak=1, but NO cooldown yet.
    releases1, warnings1 = await fan_out([down], run_one, cooldown=cd)
    assert releases1 == []
    assert calls == 1
    assert len(warnings1) == 1
    assert warnings1[0][0] == "down"
    assert warnings1[0][1] == "source_unavailable"
    assert cd.in_cooldown("down") is False  # one failure stays live

    # Second call: still retried (not skipped) → run_one raises again, streak=2 → a
    # 30s cooldown opens.
    releases2, warnings2 = await fan_out([down], run_one, cooldown=cd)
    assert releases2 == []
    assert calls == 2  # retried, not skipped
    assert warnings2[0][1] == "source_unavailable"
    assert cd.in_cooldown("down") is True

    # Third call: skipped — run_one is NOT invoked again (zero upstream calls),
    # result is [], and a cooldown warning is still returned.
    releases3, warnings3 = await fan_out([down], run_one, cooldown=cd)
    assert releases3 == []
    assert calls == 2  # ZERO additional invocations
    assert len(warnings3) == 1
    assert warnings3[0] == (
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
    cd = SourceFailureCooldown(base_seconds=30, max_seconds=600, clock=lambda: 0.0)

    async def run_one(_: _FakeSource) -> list[str]:
        return []  # successful 200-empty

    releases, warnings = await fan_out([empty], run_one, cooldown=cd)
    assert releases == []
    assert warnings == []  # SRCH-04: empty success is not a warning
    assert cd.in_cooldown("empty") is False  # no cooldown on a successful empty


def test_backoff_schedule_escalates_on_consecutive_failures() -> None:
    # Unit-level: the consecutive-failure backoff opens no cooldown on the 1st failure,
    # then min(base*2**(streak-2), max) on the 2nd onward — 30, 60, 120, 240, 480, 600
    # (capped) for base=30 / max=600. Clock pinned at 0.0 so remaining() == duration.
    cd = SourceFailureCooldown(base_seconds=30, max_seconds=600, clock=lambda: 0.0)

    cd.record_failure("s")  # streak 1 → live
    assert cd.in_cooldown("s") is False
    assert cd.remaining("s") == 0.0

    for expected in (30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0):  # streaks 2..8
        cd.record_failure("s")
        assert cd.in_cooldown("s") is True
        assert cd.remaining("s") == expected


def test_record_success_resets_streak_to_live() -> None:
    # A clean run clears BOTH the streak and any live cooldown, so the next failure
    # starts fresh at "failure 1 = live" instead of carrying the escalated backoff.
    cd = SourceFailureCooldown(base_seconds=30, max_seconds=600, clock=lambda: 0.0)

    cd.record_failure("s")
    cd.record_failure("s")  # streak 2 → 30s cooldown
    assert cd.in_cooldown("s") is True

    cd.record_success("s")  # recovery → streak + cooldown cleared
    assert cd.in_cooldown("s") is False
    assert cd.remaining("s") == 0.0

    cd.record_failure("s")  # back to streak 1 → live, no cooldown
    assert cd.in_cooldown("s") is False


def test_zero_knob_disables_cooldown() -> None:
    # 0 on either knob is the documented disable value: every in_cooldown → False,
    # record_failure/record_success are no-ops.
    for cd in (
        SourceFailureCooldown(base_seconds=0, max_seconds=600, clock=lambda: 0.0),
        SourceFailureCooldown(base_seconds=30, max_seconds=0, clock=lambda: 0.0),
    ):
        cd.record_failure("s")
        cd.record_failure("s")
        cd.record_failure("s")
        assert cd.in_cooldown("s") is False
        assert cd.remaining("s") == 0.0
