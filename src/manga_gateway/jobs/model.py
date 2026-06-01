"""Internal download-job model + status enum (JOB-01, D-29).

The internal :class:`Job` is the gateway-side state record persisted by
:mod:`manga_gateway.jobs.store`. It is deliberately DISTINCT from the wire
:class:`~manga_gateway.models.download.DownloadJob` DTO (PATTERNS §model.py): the
wire DTO is the camelCase projection Mangarr sees; this dataclass is the durable
in-process state the engine mutates.

:class:`JobStatus` is the STRICT internal lifecycle (D-29) — exactly the six
states a job can occupy inside the engine. The wire DTO keeps the FULL contract
enum (incl. ``warning``/``paused``, D-29a); those two never become internal states.

``Job`` is MUTABLE (not ``frozen``) because progress fields (``status``,
``downloaded_pages``, byte counters, ``message``, timestamps) advance as the engine
drives the state machine. Following the ``handles/store.py`` discipline, it stores
only STABLE ids (``chapter_id`` = MangaDex chapter UUID) plus the resolution
snapshot — it NEVER carries the volatile at-home ``baseUrl``/cookies (HDL-01/D-17).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Live (non-terminal) statuses gate the partial unique index + rehydration.
# Kept here as the single source of truth the store imports (D-27/D-29).
_LIVE_STATUSES = ("queued", "resolving", "downloading", "archiving")


class JobStatus(StrEnum):
    """Strict internal job lifecycle (JOB-01, D-29 — exactly six states).

    Do NOT add ``warning``/``paused`` here: those exist only on the wire DTO
    (D-29a). A ``str`` enum so the value persists verbatim to the SQLite
    ``status`` column and rehydrates by value.
    """

    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    ARCHIVING = "archiving"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    """A single download job's durable state (PLAT-03).

    Mutable: the engine advances ``status`` and the progress fields in place,
    write-through to SQLite on each transition (D-27 store contract). Carries
    STABLE ids + the resolution snapshot only — never the volatile at-home
    ``baseUrl`` (HDL-01/D-17). Timestamps are ISO-8601 UTC strings.
    """

    job_id: str
    release_handle: str
    source_key: str
    title: str
    status: JobStatus
    manga_id: int | None
    output_format: str
    chapter_id: str | None
    language: str | None
    total_pages: int | None
    downloaded_pages: int | None
    total_bytes: int
    remaining_bytes: int
    output_path: str | None
    message: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    # Projection-only byte progress (WR-08): the in-memory running total of
    # fetched page bytes + a download-start marker for the live ETA/rate calc.
    # These are NOT persisted columns (mirroring how ``downloaded_pages`` is
    # advanced in memory without a per-page SQLite write, DL-05) — the engine
    # pins the EXACT ``total_bytes``/``remaining_bytes`` to the persisted columns
    # on completion, but the per-page accumulation + start marker stay in memory.
    # Defaulted (and placed last) so ``store._row_to_job`` (which does not select
    # them) still constructs a valid ``Job`` and every keyword-arg call site is
    # unaffected — dataclass requires defaulted fields after non-default ones.
    downloaded_bytes: int = 0
    download_started_at: str | None = None
    # The resolved series title (from ``ResolutionRecord.manga_title``). Unlike the
    # two projection-only fields above, this IS submit-time DURABLE data: it backs
    # the per-series output folder for mangaId-less grabs (#16) and PERSISTS to its
    # own SQLite column (store wires _CREATE_SCHEMA/_COLUMNS/_row_to_job/_job_values
    # + an additive ALTER migration). Defaulted so existing ``Job(...)`` / test
    # ``_make_job`` call sites that omit it still construct.
    manga_title: str | None = None
