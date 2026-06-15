"""Background ``JobEngine`` state-machine tests (Task 1 — JOB-01/02/03/04, D-29).

The engine drives a single job ``resolving → downloading → archiving → completed``
on the all-pages-succeed path and ends ``failed`` (no partial CBZ) on ANY
unrecoverable page loss (D-29 strict). It is SOURCE-AGNOSTIC: it resolves the
source class from the registry by ``sourceKey`` and calls ``fetch_manifest`` /
``fetch_image`` — it NEVER names MangaDex (SRC-01).

These tests construct a ``JobEngine`` directly against a real on-disk ``JobStore``
(tmp_path) and a STUB source registered into a fresh ``SourceRegistry`` so the
engine semantics are asserted without the full HTTP stack. The stub source's
manifest/image behavior is parameterized per test (success, permanent fetch
failure, HTML-blob validation failure, manifest-resolve failure, stale-baseUrl
403 → re-resolve).
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from PIL import Image

from manga_gateway.config import Settings
from manga_gateway.framework.errors import SourceError
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore
from manga_gateway.jobs.engine import JobEngine
from manga_gateway.jobs.model import Job, JobStatus
from manga_gateway.jobs.store import JobStore, open_store

if TYPE_CHECKING:
    from manga_gateway.framework.context import SourceContext


# ─────────────────────────────── test helpers ───────────────────────────────


def _png_bytes() -> bytes:
    """A tiny valid PNG that Pillow ``verify()`` accepts."""
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _exists(path: str) -> bool:
    """Sync existence check (avoids ASYNC240 Path methods inside async tests)."""
    return os.path.exists(path)


def _has_cbz_under(root: Path) -> bool:
    """Sync recursive ``*.cbz`` probe under ``root`` (no Path I/O in async scope)."""
    for _dirpath, _dirnames, filenames in os.walk(root):
        if any(name.endswith(".cbz") for name in filenames):
            return True
    return False


def _make_job(
    job_id: str = "j_test",
    release_handle: str = "h_test",
    *,
    output_root: str | None = None,
    source_key: str = "stub",
) -> Job:
    return Job(
        job_id=job_id,
        release_handle=release_handle,
        source_key=source_key,
        title="Solo Leveling - Chapter 1 (en)",
        status=JobStatus.QUEUED,
        manga_id=42,
        output_format="cbz",
        chapter_id="11111111-2222-3333-4444-555555555555",
        language="en",
        total_pages=None,
        downloaded_pages=None,
        total_bytes=0,
        remaining_bytes=0,
        output_path=None,
        message=None,
        created_at=_now(),
        updated_at=_now(),
        completed_at=None,
    )


class _StubSource:
    """A minimal source the engine dispatches to source-agnostically.

    ``key``/``rate_limit_per_minute`` mirror the framework ``Source`` surface the
    engine reads when building a ``SourceContext``. ``fetch_manifest`` /
    ``fetch_image`` are configurable per test.
    """

    key = "stub"
    rate_limit_per_minute = 6000

    def __init__(
        self,
        *,
        manifest: list[str] | None = None,
        image_map: dict[str, bytes] | None = None,
        manifest_error: Exception | None = None,
        image_errors: dict[str, Exception] | None = None,
        # For the stale-baseUrl test: a list of manifests returned in sequence on
        # successive fetch_manifest calls.
        manifest_sequence: list[list[str]] | None = None,
    ) -> None:
        self._manifest = manifest
        self._manifest_sequence = manifest_sequence
        self._manifest_calls = 0
        self._image_map = image_map or {}
        self._manifest_error = manifest_error
        self._image_errors = image_errors or {}
        self.image_fetch_count = 0

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        self._manifest_calls += 1
        if self._manifest_error is not None:
            raise self._manifest_error
        if self._manifest_sequence is not None:
            idx = min(self._manifest_calls - 1, len(self._manifest_sequence) - 1)
            return self._manifest_sequence[idx]
        assert self._manifest is not None
        return self._manifest

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        self.image_fetch_count += 1
        if url in self._image_errors:
            raise self._image_errors[url]
        return self._image_map[url]


def _engine_for(source: _StubSource, store: JobStore, *, output_root: str) -> JobEngine:
    registry = SourceRegistry()
    # The engine instantiates the registered source fresh; register a factory that
    # returns our configured stub instance so per-test behavior is honored.
    registry._sources[source.key] = lambda: source  # type: ignore[assignment]
    settings = Settings(api_key="k", output_root=output_root, image_fetch_concurrency=4)
    return JobEngine(
        store=store,
        registry=registry,
        session=SessionManager(_NullTransport()),
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        settings=settings,
    )


class _NullTransport:
    """A transport the engine never actually calls (the stub source bypasses HTTP)."""

    async def request(self, method: str, url: str, **kwargs: object) -> object:
        raise AssertionError("stub source must not perform real HTTP")

    async def aclose(self) -> None:  # pragma: no cover - interface completeness
        pass


# ─────────────────────────────── happy path ───────────────────────────────


@pytest.mark.asyncio
async def test_run_completes_and_writes_cbz(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png", "http://node/data/h/p2.png"]
        src = _StubSource(
            manifest=urls,
            image_map={urls[0]: _png_bytes(), urls[1]: _png_bytes()},
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        assert job.total_pages == 2
        assert job.downloaded_pages == 2
        assert job.output_path is not None
        assert job.completed_at is not None
        assert _exists(job.output_path)
        # A valid ZIP_STORED CBZ with two ordered, zero-padded entries.
        with zipfile.ZipFile(job.output_path) as zf:
            names = zf.namelist()
        assert names == sorted(names)
        assert len(names) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_persists_status_to_store_before_completion(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png"]
        src = _StubSource(manifest=urls, image_map={urls[0]: _png_bytes()})
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        # The store row reflects the terminal write-through (SQLite, not projection).
        persisted = await store.get(job.job_id)
        assert persisted is not None
        assert persisted.status == JobStatus.COMPLETED
        assert persisted.output_path is not None
    finally:
        await store.close()


# ─────────────────────────── strict fail (D-29) ───────────────────────────


@pytest.mark.asyncio
async def test_run_fails_strict_on_permanent_page_error(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png", "http://node/data/h/p2.png"]
        src = _StubSource(
            manifest=urls,
            image_map={urls[0]: _png_bytes()},
            image_errors={urls[1]: SourceError("source_unavailable", "upstream 404")},
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        assert job.message
        # No partial CBZ written anywhere under the output root.
        assert not _has_cbz_under(tmp_path / "out")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_fails_on_invalid_image_blob(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png", "http://node/data/h/p2.png"]
        src = _StubSource(
            manifest=urls,
            image_map={
                urls[0]: _png_bytes(),
                urls[1]: b"<html>404 not found</html>",  # not a real image
            },
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        assert not _has_cbz_under(tmp_path / "out")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_invalid_page_failure_is_attributed_and_self_diagnosing(
    tmp_path: Path,
) -> None:
    """#238: an invalid-image (non-image bytes) page failure must (1) attribute the
    ``kind="job"`` failure event to the job's resolved series/chapter — the 1024
    MangaBall failures recorded null — and (2) carry a self-diagnosing message that
    sniffs the non-image body (byte length + class + head) so a recurrence is
    diagnosable from telemetry alone."""
    from manga_gateway.metrics.collector import Collector, set_collector
    from manga_gateway.metrics.context import current_source
    from manga_gateway.metrics.store import InMemoryStore

    from ._metrics_helpers import CapturingRingWriter

    ring = CapturingRingWriter()
    set_collector(Collector(InMemoryStore(slow_factor=3.0), ring_writer=ring))  # type: ignore[arg-type]
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png", "http://node/data/h/p2.png"]
        # A Cloudflare-style HTML challenge body in place of page 2's image — the
        # exact non-image shape the bounded MangaBall window returned.
        html_blob = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
        src = _StubSource(
            manifest=urls,
            image_map={urls[0]: _png_bytes(), urls[1]: html_blob},
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        job.manga_title = "Solo Leveling"
        job.chapter_number = 40.2
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        assert not _has_cbz_under(tmp_path / "out")

        # (1) the persisted/wire message is self-diagnosing: byte length + sniff + head.
        assert job.message is not None
        assert "page 2 invalid" in job.message
        assert "sniff=html" in job.message
        assert f"{len(html_blob)} bytes" in job.message

        # (2) the kind="job" failure event is ATTRIBUTED (was null pre-#238).
        fail = next(
            e for e in ring.iter_recent() if e.kind == "job" and e.op == "failed"
        )
        assert fail.source_key == "stub"
        assert fail.manga_title == "Solo Leveling"
        assert fail.chapter_number == 40.2
        assert fail.error is not None and "sniff=html" in fail.error
    finally:
        await store.close()
        set_collector(None)
        current_source.set(None)


@pytest.mark.asyncio
async def test_run_fails_when_manifest_unresolvable(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        src = _StubSource(
            manifest_error=SourceError("source_unavailable", "upstream 404"),
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        assert job.message
        assert not _has_cbz_under(tmp_path / "out")
    finally:
        await store.close()


# ─────────────────────── stale baseUrl re-resolve (Pitfall 4) ─────────────


@pytest.mark.asyncio
async def test_run_reresolves_manifest_on_image_403_then_completes(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        stale = ["http://stale/data/h/p1.png", "http://stale/data/h/p2.png"]
        fresh = ["http://fresh/data/h/p1.png", "http://fresh/data/h/p2.png"]
        src = _StubSource(
            manifest_sequence=[stale, fresh],
            # The first (stale) page-2 URL 403s; the fresh URLs all succeed.
            image_map={
                stale[0]: _png_bytes(),
                fresh[0]: _png_bytes(),
                fresh[1]: _png_bytes(),
            },
            image_errors={
                stale[1]: SourceError("source_unavailable", "upstream 403", status=403)
            },
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        assert job.output_path is not None
        assert _exists(job.output_path)
        assert src._manifest_calls >= 2  # re-resolved at least once
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_fails_when_reresolve_budget_exceeded(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        # Every manifest the source returns 403s on page 2 — re-resolve never helps.
        def _bad(n: int) -> list[str]:
            return [f"http://m{n}/p1.png", f"http://m{n}/p2.png"]

        manifests = [_bad(0), _bad(1), _bad(2), _bad(3)]
        image_map: dict[str, bytes] = {}
        image_errors: dict[str, Exception] = {}
        for m in manifests:
            image_map[m[0]] = _png_bytes()
            image_errors[m[1]] = SourceError(
                "source_unavailable", "upstream 403", status=403
            )
        src = _StubSource(
            manifest_sequence=manifests,
            image_map=image_map,
            image_errors=image_errors,
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        assert not _has_cbz_under(tmp_path / "out")
    finally:
        await store.close()


# ─────────── stale-baseUrl detection (WR-07: branch on status, not text) ───


@pytest.mark.asyncio
async def test_run_does_not_reresolve_when_message_only_mentions_403(
    tmp_path: Path,
) -> None:
    """A non-403 SourceError whose message contains the digits "403" must NOT
    trigger stale-baseUrl re-resolve (WR-07). Engine branches on ``e.status``."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png", "http://node/data/h/p2.png"]
        src = _StubSource(
            manifest=urls,
            image_map={urls[0]: _png_bytes()},
            image_errors={
                # No status= → ``status`` is None, so this is NOT a 403 even
                # though the rendered message contains the digits "403".
                urls[1]: SourceError("source_unavailable", "page 403 invalid layout"),
            },
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        # Manifest fetched exactly once — no re-resolve was triggered.
        assert src._manifest_calls == 1
        assert not _has_cbz_under(tmp_path / "out")
    finally:
        await store.close()


# ──────────────────── diagnosability (issue #70) ──────────────────────────


@pytest.mark.asyncio
async def test_unexpected_error_logs_traceback_but_wire_message_stays_generic(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-SourceError escaping the engine (issue #70) must record its full
    traceback to the INTERNAL log while the wire-visible ``job.message`` carries
    ONLY the generic prefix + the exception TYPE — never the internal message/
    traceback (download-jobs-failed-23: the type survives a restart that wipes the
    logs, while internals are still never leaked over the API)."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png"]
        secret = "the-internal-cause-not-for-the-wire"
        src = _StubSource(
            manifest=urls,
            # A bare RuntimeError (NOT a SourceError) — the exact "non-SourceError
            # exception escaped unwrapped" shape from the issue.
            image_errors={urls[0]: RuntimeError(secret)},
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        with caplog.at_level(logging.WARNING, logger="manga_gateway.jobs.engine"):
            await engine.run(job)

        assert job.status == JobStatus.FAILED
        # Wire-visible message carries the generic prefix + the exception TYPE only.
        assert job.message == "job failed: RuntimeError"
        # The internal cause (the exception's MESSAGE) is NEVER reflected on the wire.
        assert secret not in (job.message or "")

        # The internal log records the cause WITH a traceback (exc_info attached).
        records = [r for r in caplog.records if r.exc_info is not None]
        assert records, "expected an exc_info log record carrying the traceback"
        # The captured traceback names the underlying exception type + message.
        text = caplog.text
        assert "RuntimeError" in text
        assert secret in text
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_httpx_error_in_fetch_surfaces_as_structured_source_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A raw httpx transport error from ``fetch_image`` (the realistic
    ~98s-then-generic-fail escape: tenacity exhausts its reraise=True retries and
    re-raises the httpx error) must be wrapped as a page-scoped, structured
    ``SourceError("source_unavailable", "page N …")`` rather than degrading to the
    generic "job failed" (issue #70, fix 3)."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png", "http://node/data/h/p2.png"]
        src = _StubSource(
            manifest=urls,
            image_map={urls[0]: _png_bytes()},
            image_errors={urls[1]: httpx.ConnectTimeout("image host timed out")},
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        with caplog.at_level(logging.WARNING, logger="manga_gateway.jobs.engine"):
            await engine.run(job)

        assert job.status == JobStatus.FAILED
        # Wrapped into a specific, page-scoped reason — NOT the generic fallback.
        assert job.message is not None
        assert job.message != "job failed"
        assert "page 2" in job.message
        assert "ConnectTimeout" in job.message
        assert not _has_cbz_under(tmp_path / "out")
        # Because the failure is now a SourceError, the engine takes the structured
        # SourceError branch — NO unexpected-error traceback is emitted.
        assert not [r for r in caplog.records if r.exc_info is not None]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_transition_logs_carry_release_identity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every status-transition log must carry the release identity (title +
    chapter id + a truncated handle) so a transition can be tied to WHAT it was
    downloading (issue #70)."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png"]
        src = _StubSource(manifest=urls, image_map={urls[0]: _png_bytes()})
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job(release_handle="h_abcdef0123456789secrettail")
        await store.insert(job)

        with caplog.at_level(logging.INFO, logger="manga_gateway.jobs.engine"):
            await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        # At least one transition log line carries the human-readable identity.
        transition_lines = [
            r.getMessage()
            for r in caplog.records
            if "→" in r.getMessage() and "→failed" not in r.getMessage()
        ]
        assert transition_lines, "expected status-transition log lines"
        joined = "\n".join(transition_lines)
        assert "Solo Leveling - Chapter 1 (en)" in joined  # title
        assert job.chapter_id is not None
        assert job.chapter_id in joined  # chapter id
        # The handle is TRUNCATED to a short prefix — never logged in full.
        assert "h_abcd022" not in joined  # sanity: not a false match
        assert "h_abcdef" in joined  # prefix present
        assert "secrettail" not in joined  # full secret-like handle withheld
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fail_log_carries_release_identity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The terminal fail log must carry the release identity so a failed job can be
    tied to WHAT it was downloading (issue #70 — the core gap that made run
    26774561225's failure unrecoverable)."""
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png"]
        src = _StubSource(
            manifest=urls,
            image_errors={urls[0]: SourceError("source_unavailable", "upstream 404")},
        )
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job(release_handle="h_abcdef0123456789secrettail")
        await store.insert(job)

        with caplog.at_level(logging.WARNING, logger="manga_gateway.jobs.engine"):
            await engine.run(job)

        assert job.status == JobStatus.FAILED
        fail_lines = [
            r.getMessage() for r in caplog.records if "→failed" in r.getMessage()
        ]
        assert fail_lines, "expected a terminal fail log line"
        joined = "\n".join(fail_lines)
        assert "Solo Leveling - Chapter 1 (en)" in joined  # title
        assert job.chapter_id is not None
        assert job.chapter_id in joined  # chapter id
        assert "h_abcdef" in joined  # truncated handle prefix
        assert "secrettail" not in joined  # full secret-like handle withheld
    finally:
        await store.close()


# ─────────────────────────── source-agnostic (SRC-01) ──────────────────────


def test_engine_module_contains_no_mangadex_literal() -> None:
    import manga_gateway.jobs.engine as engine_mod

    source = Path(engine_mod.__file__).read_text(encoding="utf-8")
    code_lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    assert "mangadex" not in "\n".join(code_lines).lower()


# ───────────────────── page filename / extension (issue #204) ──────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Plain page URL — extension preserved unchanged.
        ("https://cdn.example/h/p1.png", "p1.png"),
        ("https://cdn.example/chapter/03.webp", "03.webp"),
        # issue #204: a kagane page URL carries a JWT (dotted header.payload.sig)
        # in ?token=... plus &is_datasaver=false. The query MUST be stripped so the
        # packager derives ``.jpg`` — not the JWT-signature-plus-query tail.
        (
            "https://kstatic.to/api/v2/books/page/BK/PG.jpg"
            "?token=aaa.bbb.WH4CvZhPRhFjb3IiDckA71FVVcGLKrTbf-ni-tONpDk"
            "&is_datasaver=false",
            "PG.jpg",
        ),
        # Degenerate manifest URL with no path segment → harmless constant.
        ("https://cdn.example/", "page"),
    ],
)
def test_page_filename_strips_query_before_extension(url: str, expected: str) -> None:
    from manga_gateway.jobs.engine import _page_filename

    name = _page_filename(url)
    assert name == expected
    # The suffix the packager keys CBZ entries on is now a clean image ext —
    # never the dotted JWT-signature tail (the #204 garbage extension).
    assert "&" not in Path(name).suffix
    assert "is_datasaver" not in Path(name).suffix
