"""Opaque ``downloadHandle`` store (HDL-01/02, D-15/D-16/D-17).

A handle is a random ``secrets.token_urlsafe`` token carrying ZERO structure
(D-15, 128-bit CSPRNG — never guessable/sequential, SEC-01). It maps to a
:class:`ResolutionRecord` that stores only STABLE ids (the source's chapter
resolve unit) plus the advisory snapshot already known from search.

Two-tier storage (D-16 REVERSAL — debug session release-no-longer-resolvable):

* an in-memory ``TTLCache`` is the AUTHORITY for fresh handles — ``mint`` writes
  it synchronously so a just-minted handle always resolves, and it bounds memory
  via ``maxsize``; and
* an aiosqlite table behind it gives handles RESTART SURVIVAL. The prior
  in-memory-only design (D-16) meant every redeploy wiped all live handles, which
  — combined with the 60-min TTL — was the CONFIRMED cause of the production
  "release no longer resolvable" failures: Mangarr's automated library-sync
  replays grabs long after a handle was minted, so an expired (or restart-wiped)
  handle was a ``bad_handle`` miss. Persisting + a longer configurable TTL
  (``GATEWAY_HANDLE_TTL_SECONDS``, default 6h) closes that window.

Why ``mint`` stays SYNCHRONOUS while the table is async (aiosqlite): sources call
``ctx.handle_store.mint(...)`` from synchronous release-normalization code that
runs INSIDE the async fan-out (on the event loop). We must not change that wide
call surface to ``await``. So ``mint`` writes the in-memory cache synchronously
(the fresh-handle authority) and SCHEDULES a fire-and-forget background task to
persist the row — best-effort durability for the restart case, never on the
fresh-resolve hot path. ``resolve`` (the POST /downloads path, already async) is
the only reader: in-memory hit → return; miss → one indexed SQLite lookup that
treats an EXPIRED row as a miss (TTL is STILL enforced — handles age out, SEC
posture / D-15 never-keep-forever) and re-warms the cache on a hit. ``resolve`` is
NOT on the GET /downloads poll path, so poll-friendliness (DL-05) is preserved.

Principle (D-17 / Pitfall 6): store STABLE data, resolve VOLATILE tokens fresh.
The at-home ``baseUrl`` / cookies are NEVER stored here or embedded in the handle
(HDL-01) — Phase 3 resolves them fresh at grab time.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal

import aiosqlite
from cachetools import TTLCache

_log = logging.getLogger("manga_gateway.handles")

# Default handle TTL (HDL-02). The 60-min default was the CONFIRMED cause of the
# production "release no longer resolvable" misses; the lifespan now passes the
# configurable GATEWAY_HANDLE_TTL_SECONDS (default 6h) — this constant is the
# fallback for direct construction / tests only.
_HANDLE_TTL_SECONDS = 3600
# Cap MUST exceed a full library-sync burst so handles are never evicted before
# their TTL (production telemetry saw ~23k handles minted in a 2-hour window,
# 2.3x the prior 10_000 cap, causing 739 `bad_handle` rejections, GAP-2). A
# `ResolutionRecord` is small (~9 short fields), so 200k is ~tens of MB RAM. The
# SQLite tier holds the durable copy regardless of this cap. Operator-overridable
# via GATEWAY_HANDLE_MAXSIZE.
_HANDLE_MAXSIZE = 200_000

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS handles (
  handle           TEXT PRIMARY KEY,
  source_key       TEXT NOT NULL,
  chapter_id       TEXT NOT NULL,
  language         TEXT NOT NULL,
  title            TEXT NOT NULL,
  manga_title      TEXT,
  chapter_number   TEXT,
  volume           INTEGER,
  scanlation_group TEXT,
  page_count       INTEGER,
  expires_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_handles_expires_at ON handles(expires_at);
"""

_COLUMNS = (
    "handle",
    "source_key",
    "chapter_id",
    "language",
    "title",
    "manga_title",
    "chapter_number",
    "volume",
    "scanlation_group",
    "page_count",
    "expires_at",
)

_INSERT_SQL = (
    f"INSERT OR REPLACE INTO handles ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)


@dataclass(frozen=True)
class ResolutionRecord:
    """The resolve target + advisory snapshot for a minted handle (D-17).

    Stores STABLE ids (``chapter_id`` = the source's chapter resolve unit) and the
    metadata already known from search. NEVER stores the volatile at-home
    ``baseUrl`` / cookies (HDL-01 / Pitfall 6) — those are resolved fresh in
    Phase 3.
    """

    source_key: str
    chapter_id: str
    language: str
    title: str
    manga_title: str | None
    chapter_number: Decimal | None
    volume: int | None
    scanlation_group: str | None
    page_count: int | None


def _record_from_row(row: aiosqlite.Row) -> ResolutionRecord:
    raw_number = row["chapter_number"]
    return ResolutionRecord(
        source_key=row["source_key"],
        chapter_id=row["chapter_id"],
        language=row["language"],
        title=row["title"],
        manga_title=row["manga_title"],
        chapter_number=(Decimal(raw_number) if raw_number is not None else None),
        volume=row["volume"],
        scanlation_group=row["scanlation_group"],
        page_count=row["page_count"],
    )


def _row_values(
    handle: str, record: ResolutionRecord, expires_at: float
) -> tuple[object, ...]:
    return (
        handle,
        record.source_key,
        record.chapter_id,
        record.language,
        record.title,
        record.manga_title,
        # chapter_number stored as TEXT to round-trip Decimal exactly (REAL would
        # lose precision on values like 1.5 vs the canonical Decimal form).
        str(record.chapter_number) if record.chapter_number is not None else None,
        record.volume,
        record.scanlation_group,
        record.page_count,
        expires_at,
    )


class HandleStore:
    """Two-tier opaque-handle → ``ResolutionRecord`` store (HDL-01/02, D-16 reversal).

    In-memory ``TTLCache`` is the fresh-handle authority; an aiosqlite table behind
    it gives restart survival. Construct via :func:`open_handle_store` (which opens
    the DB, creates the schema, and rehydrates non-expired rows). A connection-less
    instance (``conn=None``) degrades to in-memory-only — used by lightweight tests
    and as a safety net if the DB cannot be opened.
    """

    def __init__(
        self,
        ttl: int = _HANDLE_TTL_SECONDS,
        maxsize: int = _HANDLE_MAXSIZE,
        conn: aiosqlite.Connection | None = None,
    ) -> None:
        self._ttl = ttl
        self._cache: TTLCache[str, ResolutionRecord] = TTLCache(
            maxsize=maxsize, ttl=ttl
        )
        self._conn = conn
        self._write_lock = asyncio.Lock()
        # Track fire-and-forget persist tasks so they are not GC'd mid-flight and so
        # the lifespan can drain them on shutdown (CR — never lose a write to GC).
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def ttl(self) -> int:
        return self._ttl

    def mint(self, record: ResolutionRecord) -> str:
        """Mint an opaque handle for ``record`` and return it (D-15 CSPRNG token).

        Synchronous by contract — sources call this from sync release-normalization
        on the event loop. The in-memory cache is written immediately (fresh-handle
        authority); the SQLite persist is scheduled as a fire-and-forget task so the
        handle survives a restart without blocking the caller (best-effort: a failed
        persist degrades to in-memory-only for that handle, which is the prior D-16
        behaviour, never worse).
        """
        handle = secrets.token_urlsafe(16)  # opaque, zero structure, 128-bit (SEC-01)
        self._cache[handle] = record
        if self._conn is not None:
            expires_at = time.time() + self._ttl
            self._schedule_persist(handle, record, expires_at)
        # #21: DEBUG (not INFO) — a title search mints one handle per release, which
        # would drown the console at INFO. Operator opts in with
        # GATEWAY_LOG_LEVEL=DEBUG when chasing handle/TTL behaviour.
        _log.debug("minted handle src=%s ttl=%ds", record.source_key, self._ttl)
        return handle

    def _schedule_persist(
        self, handle: str, record: ResolutionRecord, expires_at: float
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (a sync unit test minting directly): the in-memory
            # write already happened; skip durable persist rather than crash.
            return
        task = loop.create_task(self._persist(handle, record, expires_at))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _persist(
        self, handle: str, record: ResolutionRecord, expires_at: float
    ) -> None:
        if self._conn is None:
            return
        try:
            async with self._write_lock:
                await self._conn.execute(
                    _INSERT_SQL, _row_values(handle, record, expires_at)
                )
                await self._conn.commit()
        except Exception:  # noqa: BLE001 - best-effort durability, never crash mint
            # The in-memory copy already resolves fresh; a persist failure only costs
            # restart survival for this one handle. Log and move on (Pitfall: a raised
            # background task would otherwise log a bare "Task exception was never
            # retrieved" with no context).
            _log.warning(
                "handle persist failed src=%s (in-memory only for this handle)",
                record.source_key,
                exc_info=True,
            )

    async def resolve(self, handle: str) -> ResolutionRecord | None:
        """Resolve a handle to its record, or ``None`` if unknown/expired (Phase 3).

        In-memory hit is the fast path. On a miss (cache eviction OR a restart wiped
        the in-memory tier) fall back to one indexed SQLite lookup; an EXPIRED row is
        treated as a miss (TTL still enforced) and a live row re-warms the in-memory
        cache so subsequent resolves stay fast. This is the POST /downloads path, not
        the GET /downloads poll path (DL-05 poll-friendliness preserved).
        """
        record = self._cache.get(handle)
        if record is not None:
            _log.debug("resolve handle hit (mem) src=%s", record.source_key)
            return record
        if self._conn is None:
            _log.debug("resolve handle miss (mem-only store)")
            return None
        record = await self._resolve_from_db(handle)
        _log.debug(
            "resolve handle %s (db) src=%s",
            "hit" if record is not None else "miss",
            record.source_key if record is not None else "?",
        )
        return record

    async def _resolve_from_db(self, handle: str) -> ResolutionRecord | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM handles WHERE handle = ?", (handle,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        if row["expires_at"] <= time.time():
            # Expired row: a miss. Prune it so the table does not accumulate dead
            # rows (best-effort — never fail a resolve on a prune error).
            await self._prune_handle(handle)
            return None
        record = _record_from_row(row)
        # Re-warm the in-memory tier (post-restart, the first resolve repopulates it).
        self._cache[handle] = record
        return record

    async def _prune_handle(self, handle: str) -> None:
        if self._conn is None:
            return
        try:
            async with self._write_lock:
                await self._conn.execute(
                    "DELETE FROM handles WHERE handle = ?", (handle,)
                )
                await self._conn.commit()
        except Exception:  # noqa: BLE001 - prune is opportunistic
            _log.debug("expired-handle prune failed", exc_info=True)

    async def rehydrate(self) -> int:
        """Restart recovery (D-16 reversal): load NON-EXPIRED rows into the cache and
        prune expired ones. Returns the number of live handles rehydrated.

        After a restart the in-memory tier is empty; without this the first resolve
        of every persisted handle would pay a DB round-trip. Loading them up front
        (bounded by ``maxsize`` — the TTLCache drops the oldest if the on-disk set
        somehow exceeds it) restores the fast path immediately. Expired rows are
        deleted in the same pass so the table self-trims across restarts.
        """
        if self._conn is None:
            return 0
        now = time.time()
        async with self._write_lock:
            await self._conn.execute(
                "DELETE FROM handles WHERE expires_at <= ?", (now,)
            )
            await self._conn.commit()
        async with self._conn.execute(
            "SELECT * FROM handles WHERE expires_at > ?", (now,)
        ) as cur:
            rows = await cur.fetchall()
        count = 0
        for row in rows:
            self._cache[row["handle"]] = _record_from_row(row)
            count += 1
        if count:
            _log.info("rehydrated %d live downloadHandle(s) from disk", count)
        return count

    async def close(self) -> None:
        """Drain pending persists, then close the backing connection (teardown)."""
        pending = list(self._pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._conn is not None:
            await self._conn.close()


async def open_handle_store(
    path: str, *, ttl: int = _HANDLE_TTL_SECONDS, maxsize: int = _HANDLE_MAXSIZE
) -> HandleStore:
    """Open (and schema-init + rehydrate) the handle store at ``path``.

    Mirrors :func:`manga_gateway.jobs.store.open_store`: connects, enables WAL
    (Pitfall 7), sets a ``Row`` factory for dict-shaped reads, creates the table +
    expiry index if absent, then rehydrates the live (non-expired) handles into the
    in-memory tier. Returns a ready :class:`HandleStore`.
    """
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(_CREATE_SCHEMA)
    await conn.commit()
    store = HandleStore(ttl=ttl, maxsize=maxsize, conn=conn)
    await store.rehydrate()
    return store
