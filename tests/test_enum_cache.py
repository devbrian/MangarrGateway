"""Unit tests for the source-agnostic enumeration cache (Phase 09 Plan 01).

Network-free. Exercises:
  * ``Enumeration`` + ``covers_floor`` completeness math (CACHE-04)
  * ``EnumerationCache.enum_key`` / ``resolve_key`` shape (CACHE-03, D-01) —
    type/chapter NEVER in the key; language-order-insensitive
  * ``SingleFlightCache`` single-flight collapse, error cleanup (D-04/D-05),
    kill-switch (D-08), maxsize (D-07), per-source TTL override (D-09/CACHE-05)
  * ``EnumerationCache`` two-layer composition + the redacted, failure-isolated
    ``kind="cache"`` metric emit (D-06)
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from manga_gateway.framework.enum_cache import (
    Enumeration,
    EnumerationCache,
    SingleFlightCache,
)

# ───────────────────────────── Enumeration / covers_floor ────────────────────


def test_exhausted_enumeration_covers_any_floor() -> None:
    enum = Enumeration(items=[], chapter_numbers=(), exhausted=True, requested_limit=10)
    assert enum.covers_floor(999.0) is True


def test_non_exhausted_empty_window_covers_nothing() -> None:
    enum = Enumeration(
        items=[], chapter_numbers=(), exhausted=False, requested_limit=10
    )
    assert enum.covers_floor(1.0) is False


def test_covers_floor_within_window_true_below_window_false() -> None:
    enum = Enumeration(
        items=[object(), object()],
        chapter_numbers=(Decimal("10"), Decimal("20")),
        exhausted=False,
        requested_limit=10,
    )
    # below the cached window → older chapters never fetched → refetch, not empty
    assert enum.covers_floor(5.0) is False
    # inside the window → confidently answerable
    assert enum.covers_floor(15.0) is True
    # at the boundaries (floor math) → still covered
    assert enum.covers_floor(10.0) is True
    assert enum.covers_floor(20.0) is True


# ───────────────────────────── key builders ──────────────────────────────────


def test_enum_key_shape_and_excludes_type_chapter() -> None:
    key = EnumerationCache.enum_key("mangadex", "series-123", ["en", "ja"])
    assert key == ("mangadex", "series-123", ("en", "ja"))


def test_resolve_key_shape() -> None:
    key = EnumerationCache.resolve_key("mangadex", "one piece", ["en"])
    assert key == ("mangadex", "one piece", ("en",))


def test_keys_are_language_order_insensitive() -> None:
    a = EnumerationCache.enum_key("mangadex", "s", ["ja", "en"])
    b = EnumerationCache.enum_key("mangadex", "s", ["en", "ja"])
    assert a == b
    c = EnumerationCache.resolve_key("mangadex", "q", ["ja", "en"])
    d = EnumerationCache.resolve_key("mangadex", "q", ["en", "ja"])
    assert c == d


# ───────────────────────────── SingleFlightCache ─────────────────────────────


async def test_cold_key_fetches_once_and_caches() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "value"

    key = ("src", "a", ())
    assert await cache.get_or_fetch(key, fetch) == "value"
    # warm key: returns the stored value WITHOUT calling fetch again
    assert await cache.get_or_fetch(key, fetch) == "value"
    assert calls == 1


async def test_concurrent_same_key_collapses_to_one_fetch() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0
    gate = asyncio.Event()

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        return "v"

    key = ("src", "a", ())
    t1 = asyncio.create_task(cache.get_or_fetch(key, fetch))
    t2 = asyncio.create_task(cache.get_or_fetch(key, fetch))
    await asyncio.sleep(0)  # let both reach the gate
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 == r2 == "v"
    assert calls == 1  # single-flight collapse (D-04)


async def test_concurrent_different_keys_each_fetch() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "v"

    await asyncio.gather(
        cache.get_or_fetch(("src", "a", ()), fetch),
        cache.get_or_fetch(("src", "b", ()), fetch),
    )
    assert calls == 2


async def test_fetch_error_is_not_cached_and_inflight_popped() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0

    async def boom() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream down")

    key = ("src", "a", ())
    with pytest.raises(RuntimeError):
        await cache.get_or_fetch(key, boom)
    # failure never cached; inflight cleaned up (D-05 / T-09-03)
    assert cache._inflight == {}
    with pytest.raises(RuntimeError):
        await cache.get_or_fetch(key, boom)
    assert calls == 2  # the next caller re-fetches


async def test_concurrent_error_propagates_to_all_awaiters() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(ttl=1800, maxsize=8)
    gate = asyncio.Event()

    async def boom() -> str:
        await gate.wait()
        raise RuntimeError("boom")

    key = ("src", "a", ())
    t1 = asyncio.create_task(cache.get_or_fetch(key, boom))
    t2 = asyncio.create_task(cache.get_or_fetch(key, boom))
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(t1, t2, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert cache._inflight == {}


async def test_kill_switch_bypasses_cache() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(
        ttl=1800, maxsize=8, enabled=False
    )
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "v"

    key = ("src", "a", ())
    await cache.get_or_fetch(key, fetch)
    await cache.get_or_fetch(key, fetch)
    assert calls == 2  # fetch runs every time
    assert len(cache._cache) == 0  # nothing written (D-08)
    assert cache._inflight == {}


async def test_none_value_is_cached_as_present_not_refetched() -> None:
    """IN-01: a fetch_fn returning None is a present value, not mistaken for absence."""
    cache: SingleFlightCache[str | None] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0

    async def fetch() -> None:
        nonlocal calls
        calls += 1
        return None

    key = ("src", "a", ())
    assert await cache.get_or_fetch(key, fetch) is None
    # The _MISSING sentinel distinguishes a cached None from absence.
    assert cache.contains(key) is True
    assert await cache.get_or_fetch(key, fetch) is None
    assert calls == 1  # the None HIT did not re-fetch


async def test_empty_list_payload_is_not_cached() -> None:
    """WR-03: a valid-but-empty candidate list is returned but never cached."""
    cache: SingleFlightCache[list[str]] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0

    async def fetch() -> list[str]:
        nonlocal calls
        calls += 1
        return []

    key = ("src", "a", ())
    assert await cache.get_or_fetch(key, fetch) == []
    assert cache.contains(key) is False  # empty not written
    assert await cache.get_or_fetch(key, fetch) == []
    assert calls == 2  # re-fetched — no negative caching of the empty body


async def test_empty_enumeration_payload_is_not_cached() -> None:
    """WR-03: an Enumeration with no items is returned but never cached."""
    cache: SingleFlightCache[Enumeration] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0

    async def fetch() -> Enumeration:
        nonlocal calls
        calls += 1
        return Enumeration(
            items=[], chapter_numbers=(), exhausted=True, requested_limit=0
        )

    key = ("src", "a", ())
    await cache.get_or_fetch(key, fetch)
    assert cache.contains(key) is False
    await cache.get_or_fetch(key, fetch)
    assert calls == 2


async def test_nonempty_payload_is_cached() -> None:
    """WR-03 control: a non-empty payload IS cached (one fetch)."""
    cache: SingleFlightCache[list[str]] = SingleFlightCache(ttl=1800, maxsize=8)
    calls = 0

    async def fetch() -> list[str]:
        nonlocal calls
        calls += 1
        return ["x"]

    key = ("src", "a", ())
    await cache.get_or_fetch(key, fetch)
    assert cache.contains(key) is True
    await cache.get_or_fetch(key, fetch)
    assert calls == 1


async def test_replace_overwrites_cached_value() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(ttl=1800, maxsize=8)
    key = ("src", "a", ())

    async def fetch() -> str:
        return "first"

    assert await cache.get_or_fetch(key, fetch) == "first"
    cache.replace(key, "second")

    async def fail() -> str:  # pragma: no cover - must not run on a hit
        raise AssertionError("fetch must not run after replace")

    assert await cache.get_or_fetch(key, fail) == "second"


async def test_maxsize_is_bounded() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(ttl=1800, maxsize=2)

    async def fetch() -> str:
        return "v"

    for i in range(5):
        await cache.get_or_fetch(("src", str(i), ()), fetch)
    assert len(cache._cache) <= 2


async def test_per_source_ttl_override_and_clamp() -> None:
    now = [0.0]
    cache: SingleFlightCache[str] = SingleFlightCache(
        ttl=1800,
        maxsize=16,
        ttl_overrides={"slow": 60, "big": 99_999},
        clock=lambda: now[0],
    )
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "v"

    slow_key = ("slow", "x", ())
    default_key = ("fast", "y", ())
    big_key = ("big", "z", ())
    await cache.get_or_fetch(slow_key, fetch)  # ttl 60
    await cache.get_or_fetch(default_key, fetch)  # ttl 1800 (default)
    await cache.get_or_fetch(big_key, fetch)  # 99999 clamped to 3600
    assert calls == 3

    now[0] = 61.0
    await cache.get_or_fetch(slow_key, fetch)  # expired → re-fetch
    assert calls == 4
    await cache.get_or_fetch(default_key, fetch)  # still warm
    assert calls == 4

    now[0] = 3601.0
    await cache.get_or_fetch(big_key, fetch)  # clamped to 3600 → expired
    assert calls == 5


# ───────────────────── EnumerationCache two-layer + emit ──────────────────────


class _FakeCollector:
    """Records emit_cache calls; everything else is a no-op."""

    def __init__(self) -> None:
        self.cache_calls: list[dict[str, object]] = []

    def emit_cache(
        self, *, op: str, outcome: str, source_key: str | None = None
    ) -> None:
        self.cache_calls.append(
            {"op": op, "outcome": outcome, "source_key": source_key}
        )


class _RaisingCollector(_FakeCollector):
    def emit_cache(
        self, *, op: str, outcome: str, source_key: str | None = None
    ) -> None:
        raise RuntimeError("collector exploded")


def _enum(items: list) -> Enumeration:
    return Enumeration(
        items=items, chapter_numbers=(), exhausted=True, requested_limit=10
    )


async def test_cached_enumerate_emits_miss_then_hit() -> None:
    from manga_gateway.metrics.collector import set_collector

    fake = _FakeCollector()
    set_collector(fake)  # type: ignore[arg-type]
    try:
        cache = EnumerationCache(ttl=1800, maxsize=8)
        key = EnumerationCache.enum_key("mangadex", "series-123", ["en"])
        calls = 0

        async def fetch() -> Enumeration:
            nonlocal calls
            calls += 1
            return _enum([1, 2])

        first = await cache.cached_enumerate(key, fetch)
        second = await cache.cached_enumerate(key, fetch)
        assert first is second
        assert calls == 1  # second served from cache
        assert fake.cache_calls == [
            {"op": "enumerate", "outcome": "miss", "source_key": "mangadex"},
            {"op": "enumerate", "outcome": "hit", "source_key": "mangadex"},
        ]
    finally:
        set_collector(None)


async def test_cache_replace_emits_refetch() -> None:
    from manga_gateway.metrics.collector import set_collector

    fake = _FakeCollector()
    set_collector(fake)  # type: ignore[arg-type]
    try:
        cache = EnumerationCache(ttl=1800, maxsize=8)
        key = EnumerationCache.enum_key("comix", "hid-abc", ["en"])
        cache.cache_replace(key, _enum([9]))
        assert fake.cache_calls == [
            {"op": "enumerate", "outcome": "refetch", "source_key": "comix"}
        ]
    finally:
        set_collector(None)


async def test_cached_resolve_emits_resolve_op() -> None:
    from manga_gateway.metrics.collector import set_collector

    fake = _FakeCollector()
    set_collector(fake)  # type: ignore[arg-type]
    try:
        cache = EnumerationCache(ttl=1800, maxsize=8)
        key = EnumerationCache.resolve_key("mangadex", "one piece", ["en"])

        async def fetch() -> list:
            return ["id-1", "id-2"]

        await cache.cached_resolve(key, fetch)
        await cache.cached_resolve(key, fetch)
        assert [c["op"] for c in fake.cache_calls] == ["resolve", "resolve"]
        assert [c["outcome"] for c in fake.cache_calls] == ["miss", "hit"]
        assert all(c["source_key"] == "mangadex" for c in fake.cache_calls)
    finally:
        set_collector(None)


async def test_emit_carries_source_key_but_no_url() -> None:
    from typing import cast

    from manga_gateway.metrics.collector import Collector, set_collector
    from manga_gateway.metrics.snapshot import MetricSnapshotStore
    from manga_gateway.metrics.store import InMemoryStore

    from ._metrics_helpers import CapturingRingWriter

    ring = CapturingRingWriter()
    c = Collector(
        InMemoryStore(slow_factor=3.0),
        ring_writer=cast("MetricSnapshotStore", ring),
    )
    set_collector(c)
    try:
        cache = EnumerationCache(ttl=1800, maxsize=8)
        key = EnumerationCache.enum_key("mangadex", "secret-series-id", ["en"])

        async def fetch() -> Enumeration:
            return _enum([1])

        await cache.cached_enumerate(key, fetch)
        events = [e for e in ring.iter_recent() if e.kind == "cache"]
        assert len(events) == 1
        ev = events[0]
        assert ev.url is None  # D-06: raw series-id NEVER recorded
        assert ev.source_key == "mangadex"
        assert ev.op == "enumerate"
        assert ev.outcome == "miss"
        # the raw key / series-id appears nowhere on the event
        assert "secret-series-id" not in repr(ev)
    finally:
        set_collector(None)


async def test_raising_collector_does_not_break_cached_call() -> None:
    from manga_gateway.metrics.collector import set_collector

    set_collector(_RaisingCollector())  # type: ignore[arg-type]
    try:
        cache = EnumerationCache(ttl=1800, maxsize=8)
        key = EnumerationCache.enum_key("mangadex", "s", ["en"])

        async def fetch() -> Enumeration:
            return _enum([1])

        result = await cache.cached_enumerate(key, fetch)  # must NOT raise
        assert result.items == [1]
    finally:
        set_collector(None)


async def test_disabled_emits_nothing_and_bare_fetches() -> None:
    from manga_gateway.metrics.collector import set_collector

    fake = _FakeCollector()
    set_collector(fake)  # type: ignore[arg-type]
    try:
        cache = EnumerationCache(ttl=1800, maxsize=8, enabled=False)
        key = EnumerationCache.enum_key("mangadex", "s", ["en"])
        calls = 0

        async def fetch() -> Enumeration:
            nonlocal calls
            calls += 1
            return _enum([1])

        await cache.cached_enumerate(key, fetch)
        await cache.cached_enumerate(key, fetch)
        assert calls == 2  # no caching when disabled
        assert fake.cache_calls == []  # no emit when disabled (D-08)
    finally:
        set_collector(None)
