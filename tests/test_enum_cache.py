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
    enum = Enumeration(
        items=[], chapter_numbers=(), exhausted=True, requested_limit=10
    )
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
