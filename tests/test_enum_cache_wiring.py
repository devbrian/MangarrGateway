"""Plan 09-03 wiring guard: the ``SourceContext`` enum-cache seam + lifespan singleton.

Two layers of coverage:

* **SourceContext seam (Task 1)** — ``ctx.cached_enumerate``/``cached_resolve``/
  ``cache_replace`` + the key helpers are thin pass-throughs to an injected
  :class:`EnumerationCache`. With a real cache a second same-key call is a HIT (the
  fetch closure does NOT re-run); with the default ``enum_cache=None`` they degrade to
  a bare ``await fetch_fn()`` so every existing construction site stays byte-for-byte
  unchanged (the default-off framework-seam discipline). The limiter is NEVER acquired
  by the cache pass-throughs — it lives inside the fetch closure (CACHE-03), so a HIT
  costs no token.
* **Lifespan + route wiring (Task 2)** — the app builds ONE ``EnumerationCache`` on
  ``app.state.enum_cache`` from settings, ``get_enum_cache`` reads it, POST /search
  threads a non-None ``enum_cache`` into its ``SourceContext`` while the /recent path
  carries ``None`` (CACHE-05: recent is never cached; download resolves one handle).

All deterministic: no real browser warm (conftest autouse no-ops it), no network.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.enum_cache import Enumeration, EnumerationCache
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.handles.store import HandleStore

TEST_API_KEY = "test-key-deterministic-0123456789"


def _make_ctx(enum_cache: EnumerationCache | None) -> SourceContext:
    """Build a network-free SourceContext for the cache-seam unit tests.

    ``session`` is never touched by the cache pass-throughs, so a ``None`` cast keeps
    the harness off the transport entirely.
    """
    return SourceContext(
        source_key="mangadex",
        rate_limit_per_minute=6000,
        session=cast(Any, None),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        enum_cache=enum_cache,
    )


def _enum() -> Enumeration:
    return Enumeration(
        items=[{"id": "c1"}],
        chapter_numbers=(),
        exhausted=True,
        requested_limit=10,
    )


# ─────────────────────────── Task 1: SourceContext seam ───────────────────────────


@pytest.mark.asyncio
async def test_cached_enumerate_hit_does_not_rerun_fetch() -> None:
    """A second same-key call returns the cached Enumeration without re-fetching."""
    ctx = _make_ctx(EnumerationCache())
    key = ctx.cached_enumerate_key("series-1", ["en"])
    calls = 0

    async def fetch() -> Enumeration:
        nonlocal calls
        calls += 1
        return _enum()

    first = await ctx.cached_enumerate(key, fetch)
    second = await ctx.cached_enumerate(key, fetch)

    assert calls == 1  # the HIT did NOT re-run the fetch closure (CACHE-03)
    assert first is second


@pytest.mark.asyncio
async def test_cached_enumerate_none_runs_fetch_every_call() -> None:
    """With the default enum_cache=None, the fetch closure runs on every call."""
    ctx = _make_ctx(None)
    key = ctx.cached_enumerate_key("series-1", ["en"])
    calls = 0

    async def fetch() -> Enumeration:
        nonlocal calls
        calls += 1
        return _enum()

    await ctx.cached_enumerate(key, fetch)
    await ctx.cached_enumerate(key, fetch)

    assert calls == 2  # no caching — byte-for-byte the pre-seam path


@pytest.mark.asyncio
async def test_cached_resolve_hit_does_not_rerun_fetch() -> None:
    """Layer-1 cached_resolve has the same HIT semantics as cached_enumerate."""
    ctx = _make_ctx(EnumerationCache())
    key = ctx.cached_resolve_key("naruto", ["en"])
    calls = 0

    async def fetch() -> list[str]:
        nonlocal calls
        calls += 1
        return ["series-1"]

    first = await ctx.cached_resolve(key, fetch)
    second = await ctx.cached_resolve(key, fetch)

    assert calls == 1
    assert first == second == ["series-1"]


@pytest.mark.asyncio
async def test_cached_resolve_none_runs_fetch_every_call() -> None:
    ctx = _make_ctx(None)
    key = ctx.cached_resolve_key("naruto", ["en"])
    calls = 0

    async def fetch() -> list[str]:
        nonlocal calls
        calls += 1
        return ["series-1"]

    await ctx.cached_resolve(key, fetch)
    await ctx.cached_resolve(key, fetch)

    assert calls == 2


@pytest.mark.asyncio
async def test_cache_replace_overwrites_entry() -> None:
    """cache_replace overwrites the Layer-2 window (completeness-driven refetch)."""
    cache = EnumerationCache()
    ctx = _make_ctx(cache)
    key = ctx.cached_enumerate_key("series-1", ["en"])

    async def fetch_first() -> Enumeration:
        return _enum()

    await ctx.cached_enumerate(key, fetch_first)

    replacement = Enumeration(
        items=[{"id": "deeper"}], chapter_numbers=(), exhausted=True, requested_limit=99
    )
    ctx.cache_replace(key, replacement)

    async def fetch_should_not_run() -> Enumeration:  # pragma: no cover - must not run
        raise AssertionError("replaced entry must be served without re-fetch")

    served = await ctx.cached_enumerate(key, fetch_should_not_run)
    assert served is replacement


def test_cache_replace_none_is_noop() -> None:
    """With enum_cache=None, cache_replace is a silent no-op (no raise)."""
    ctx = _make_ctx(None)
    key = ctx.cached_enumerate_key("series-1", ["en"])
    ctx.cache_replace(key, _enum())  # must not raise


def test_key_helpers_build_canonical_tuples() -> None:
    """The key helpers prefix source_key and sort languages (T-09-01)."""
    ctx = _make_ctx(None)
    assert ctx.cached_enumerate_key("series-1", ["ja", "en"]) == (
        "mangadex",
        "series-1",
        ("en", "ja"),
    )
    assert ctx.cached_resolve_key("naruto", ["ja", "en"]) == (
        "mangadex",
        "naruto",
        ("en", "ja"),
    )
