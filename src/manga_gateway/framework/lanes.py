"""Pure lane-partition helper for the configurable solver-lane phase (LANE-01).

A *lane* is one redroid device (an adb target) with its own
``AndroidSolver`` instance (own device lock + held-clearance store). This module
holds ONLY the pure data + the pure function that turns the deployer's lane
config (``android_lanes`` lane_id->adb_target, ``android_source_lane_map``
source_key->lane_id) plus the set of android source keys into a
``dict[lane_id, LanePlan]``. The app lifespan (Plan 15-04) consumes the result to
instantiate one ``AndroidSolver`` per lane.

The CRITICAL regression property the whole phase rests on: with NO lane config
(both dicts empty) this collapses to EXACTLY ONE ``default`` lane carrying every
android source with ``adb_target=None`` — so Plan 15-04 omits the sidecar
``target`` field and a deploy with no lane config is byte-for-byte today.

R1: this is gateway framework code only — no adb/CDP/device machinery lives here
(that is the sidecar's job). The function is pure (no Settings import, no I/O) so
it is unit-testable in isolation and importable by config-level tests. Per-lane
proxy / sticky-session fields are intentionally absent (forward dependency only —
no proxy ships on the deploy; CONTEXT Pitfall 6).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_LANE = "default"
"""The lane every unmapped android source falls to (and the sole lane when no
lane config is present — the single-lane collapse)."""


@dataclass(frozen=True)
class LanePlan:
    """A resolved lane: its id, its adb target, and the sources it owns.

    ``adb_target`` is ``None`` ONLY in the single-lane collapse (no lane config),
    which signals Plan 15-04 to omit the sidecar ``target`` field entirely and
    stay byte-for-byte today. ``source_keys`` may be empty for a declared-but-
    unmapped lane (an idle dedicated device — still constructed, never dropped).
    """

    lane_id: str
    adb_target: str | None
    source_keys: frozenset[str]


def resolve_lane_plans(
    *,
    android_lanes: dict[str, str],
    source_lane_map: dict[str, str],
    android_keys: frozenset[str],
) -> dict[str, LanePlan]:
    """Partition ``android_keys`` across the configured lanes.

    Collapse branch — ``android_lanes`` empty: return a single ``default`` lane
    carrying every android key with ``adb_target=None`` (the regression guard).

    Multi-lane branch — for each declared ``lane_id`` build a ``LanePlan`` whose
    ``source_keys`` are the android keys mapped to it (an unmapped key falls to
    ``DEFAULT_LANE``). A declared lane with no sources mapped to it yields an
    EMPTY ``source_keys`` (kept, not dropped — the app may run an idle device).

    The result is deterministic, json-free pure data; ``LanePlan`` is frozen.
    """
    if not android_lanes:
        return {
            DEFAULT_LANE: LanePlan(
                lane_id=DEFAULT_LANE,
                adb_target=None,
                source_keys=frozenset(android_keys),
            )
        }

    return {
        lane_id: LanePlan(
            lane_id=lane_id,
            adb_target=target,
            source_keys=frozenset(
                key
                for key in android_keys
                if source_lane_map.get(key, DEFAULT_LANE) == lane_id
            ),
        )
        for lane_id, target in android_lanes.items()
    }
