"""aiosqlite ``HandleStore`` tests (HDL-01/02, D-15/D-16 reversal).

Covers the two-tier store added by debug session release-no-longer-resolvable:

* the in-memory tier stays the fresh-handle authority (mint → immediate resolve);
* the configurable TTL (``GATEWAY_HANDLE_TTL_SECONDS``) is plumbed into both tiers;
* handles PERSIST to SQLite and SURVIVE a restart (rehydrate);
* an EXPIRED on-disk row is a resolve MISS (TTL is still enforced) and gets pruned;
* the connection-less store degrades to in-memory-only without crashing.

aiosqlite is async-native, so every store call is awaited; tests run under
``asyncio_mode = "auto"`` (pyproject) with no explicit event-loop plumbing.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path

import aiosqlite

from manga_gateway.handles.store import (
    HandleStore,
    ResolutionRecord,
    open_handle_store,
)


def _record(chapter_id: str = "ch-1", source_key: str = "mangadex") -> ResolutionRecord:
    return ResolutionRecord(
        source_key=source_key,
        chapter_id=chapter_id,
        language="en",
        title="Solo Leveling - Chapter 1 (en)",
        manga_title="Solo Leveling",
        chapter_number=Decimal("1.5"),
        volume=None,
        scanlation_group="Team Lumikha",
        page_count=2,
    )


async def _drain(store: HandleStore) -> None:
    """Await the fire-and-forget persist tasks so the DB write is observable."""
    pending = list(store._pending)  # noqa: SLF001 - test reaches into background tasks
    if pending:
        await asyncio.gather(*pending)


async def test_mint_resolves_from_memory_immediately(tmp_path: Path) -> None:
    """The in-memory tier is the authority — a just-minted handle resolves at once,
    before the background SQLite persist has had to run."""
    store = await open_handle_store(str(tmp_path / "handles.db"))
    handle = store.mint(_record())
    record = await store.resolve(handle)
    assert record is not None
    assert record.chapter_id == "ch-1"
    assert record.chapter_number == Decimal("1.5")
    await store.close()


async def test_unknown_handle_is_a_miss(tmp_path: Path) -> None:
    store = await open_handle_store(str(tmp_path / "handles.db"))
    assert await store.resolve("never-minted") is None
    await store.close()


async def test_handle_survives_restart_via_sqlite(tmp_path: Path) -> None:
    """D-16 reversal: a handle minted before a restart is still resolvable after a
    fresh ``open_handle_store`` against the same DB (rehydrated into memory)."""
    db = str(tmp_path / "handles.db")
    store = await open_handle_store(db)
    handle = store.mint(_record(chapter_id="durable"))
    await _drain(store)  # ensure the persist landed
    await store.close()

    # Simulate a restart: brand-new store over the same on-disk DB.
    store2 = await open_handle_store(db)
    record = await store2.resolve(handle)
    assert record is not None
    assert record.chapter_id == "durable"
    await store2.close()


async def test_resolve_falls_back_to_db_when_cache_evicted(tmp_path: Path) -> None:
    """A handle absent from the in-memory tier (eviction / cold cache) still resolves
    from SQLite, and the lookup re-warms the cache."""
    store = await open_handle_store(str(tmp_path / "handles.db"))
    handle = store.mint(_record(chapter_id="evicted"))
    await _drain(store)
    # Evict it from the in-memory tier to force the DB path.
    store._cache.clear()  # noqa: SLF001 - exercising the SQLite fallback
    record = await store.resolve(handle)
    assert record is not None
    assert record.chapter_id == "evicted"
    # Re-warmed: it is back in memory.
    assert store._cache.get(handle) is not None  # noqa: SLF001
    await store.close()


async def test_expired_db_row_is_a_miss_and_pruned(tmp_path: Path) -> None:
    """TTL is STILL enforced: an expired on-disk row resolves to None and is pruned
    (SEC posture / D-15 — handles never live forever)."""
    db = str(tmp_path / "handles.db")
    # ttl=1s so the row expires quickly; we also force-expire via a past expires_at.
    store = await open_handle_store(db, ttl=1)
    handle = store.mint(_record(chapter_id="stale"))
    await _drain(store)
    store._cache.clear()  # noqa: SLF001 - force the DB path
    # Backdate the row's expiry into the past.
    assert store._conn is not None  # noqa: SLF001
    await store._conn.execute(  # noqa: SLF001
        "UPDATE handles SET expires_at = ? WHERE handle = ?",
        (time.time() - 10, handle),
    )
    await store._conn.commit()  # noqa: SLF001

    assert await store.resolve(handle) is None  # expired → miss

    # Pruned from the table.
    async with store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM handles WHERE handle = ?", (handle,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["n"] == 0
    await store.close()


async def test_rehydrate_drops_expired_rows(tmp_path: Path) -> None:
    """Restart rehydration loads live rows and DELETES expired ones in one pass."""
    db = str(tmp_path / "handles.db")
    store = await open_handle_store(db)
    live = store.mint(_record(chapter_id="live"))
    expired = store.mint(_record(chapter_id="dead"))
    await _drain(store)
    assert store._conn is not None  # noqa: SLF001
    await store._conn.execute(  # noqa: SLF001
        "UPDATE handles SET expires_at = ? WHERE handle = ?",
        (time.time() - 10, expired),
    )
    await store._conn.commit()  # noqa: SLF001
    await store.close()

    store2 = await open_handle_store(db)
    assert await store2.resolve(live) is not None
    assert await store2.resolve(expired) is None
    # The expired row is gone from disk after rehydrate.
    async with store2._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM handles"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["n"] == 1
    await store2.close()


async def test_ttl_knob_is_plumbed_into_expires_at(tmp_path: Path) -> None:
    """The configurable TTL governs the persisted ``expires_at`` (≈ now + ttl)."""
    db = str(tmp_path / "handles.db")
    ttl = 21600
    before = time.time()
    store = await open_handle_store(db, ttl=ttl)
    assert store.ttl == ttl
    handle = store.mint(_record())
    await _drain(store)
    assert store._conn is not None  # noqa: SLF001
    async with store._conn.execute(  # noqa: SLF001
        "SELECT expires_at FROM handles WHERE handle = ?", (handle,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    # expires_at should sit roughly ttl seconds in the future.
    assert before + ttl - 5 <= row["expires_at"] <= time.time() + ttl + 5
    await store.close()


async def test_connectionless_store_is_in_memory_only() -> None:
    """A bare ``HandleStore()`` (no connection) still mints+resolves in memory and
    never crashes on the SQLite paths — the degraded / unit-test mode."""
    store = HandleStore()
    handle = store.mint(_record(chapter_id="mem"))
    record = await store.resolve(handle)
    assert record is not None
    assert record.chapter_id == "mem"
    # No connection → a cache-evicted handle is simply a miss (no DB fallback).
    store._cache.clear()  # noqa: SLF001
    assert await store.resolve(handle) is None
    assert await store.rehydrate() == 0
    await store.close()  # no-op, must not raise


async def test_wal_mode_enabled(tmp_path: Path) -> None:
    """open_handle_store enables WAL (Pitfall 7), mirroring the job store."""
    store = await open_handle_store(str(tmp_path / "handles.db"))
    assert store._conn is not None  # noqa: SLF001
    async with store._conn.execute("PRAGMA journal_mode") as cur:  # noqa: SLF001
        row = await cur.fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"
    await store.close()


async def test_schema_is_present(tmp_path: Path) -> None:
    """The handles table + expiry index exist after open."""
    db = str(tmp_path / "handles.db")
    store = await open_handle_store(db)
    await store.close()
    conn = await aiosqlite.connect(db)
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
    ) as cur:
        names = {row["name"] for row in await cur.fetchall()}
    await conn.close()
    assert "handles" in names
    assert "ix_handles_expires_at" in names
