"""260628-t5u: bounded re-fetch on a payload-integrity-truncated page body.

A page can come back HTTP-complete (Content-Length matches the delivered bytes)
yet be a TRUNCATED image — e.g. a WebP whose RIFF length field over-declares. The
transport stack (tenacity in get_bytes, the proxy-pool rotation in
``fetch_image_via_pool``) never classifies that as a failure; only the
payload-level ``is_valid_image`` (Pillow ``verify()``) catches it. Before this fix
that check ran OUTSIDE every retry scope, so one bad page deterministically failed
a whole multi-page job. ``engine.py:_one`` now re-fetches up to
``image_fetch_validate_attempts`` times before raising the existing
``SourceError("source_unavailable", "page N invalid: …")``.

These tests reuse the ``test_job_engine`` harness (real on-disk JobStore + a stub
source registered into a fresh registry) and drive the full ``JobEngine.run`` so
the loop is exercised through its real call path. ``asyncio.sleep`` is patched to a
no-op so the linear backoff adds no real delay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from manga_gateway.jobs.model import JobStatus
from manga_gateway.jobs.store import open_store

from .test_job_engine import _engine_for, _make_job, _png_bytes

if TYPE_CHECKING:
    from pathlib import Path

    from manga_gateway.framework.context import SourceContext

# A short non-image blob that Pillow rejects (the truncation/garbage case).
_BAD_BLOB = b"RIFF\x00\x00\x00\x00WEBPVP8 truncated"


class _FlakyImageSource:
    """A stub source whose ``fetch_image`` returns invalid bytes for the first
    ``bad_attempts`` calls per URL, then a valid PNG. ``bad_attempts=None`` ⇒ never
    recovers (every call returns the bad blob). Duck-types the framework ``Source``
    surface the engine reads (key / rate_limit_per_minute / fetch_manifest /
    fetch_image), matching ``test_job_engine._StubSource``."""

    key = "stub"
    rate_limit_per_minute = 6000
    reresolve_manifest_on_403 = True

    def __init__(self, manifest: list[str], *, bad_attempts: int | None) -> None:
        self._manifest = manifest
        self._bad_attempts = bad_attempts
        self.fetch_calls: dict[str, int] = {}

    async def fetch_manifest(self, chapter_id: str, ctx: SourceContext) -> list[str]:
        return self._manifest

    async def fetch_image(self, url: str, ctx: SourceContext) -> bytes:
        n = self.fetch_calls.get(url, 0)
        self.fetch_calls[url] = n + 1
        if self._bad_attempts is None or n < self._bad_attempts:
            return _BAD_BLOB
        return _png_bytes()


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the engine's inter-attempt ``asyncio.sleep`` a no-op (no real delay)."""

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("manga_gateway.jobs.engine.asyncio.sleep", _instant)


@pytest.mark.asyncio
async def test_invalid_then_valid_recovers_without_failing_job(tmp_path: Path) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png"]
        # Bad on attempt 1, valid on attempt 2 (within the default 3 attempts).
        src = _FlakyImageSource(urls, bad_attempts=1)
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.COMPLETED
        assert job.downloaded_pages == 1
        # Exactly one re-fetch was needed (attempt 1 invalid, attempt 2 valid).
        assert src.fetch_calls[urls[0]] == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_always_invalid_fails_after_exactly_attempts_fetches(
    tmp_path: Path,
) -> None:
    store = await open_store(str(tmp_path / "jobs.db"))
    try:
        urls = ["http://node/data/h/p1.png"]
        src = _FlakyImageSource(urls, bad_attempts=None)  # never recovers
        engine = _engine_for(src, store, output_root=str(tmp_path / "out"))
        # Default image_fetch_validate_attempts is 3 (see _engine_for's Settings).
        attempts = engine._settings.image_fetch_validate_attempts
        job = _make_job()
        await store.insert(job)

        await engine.run(job)

        assert job.status == JobStatus.FAILED
        assert job.message is not None
        assert "page 1 invalid" in job.message
        # The fetch was retried exactly ``attempts`` times — no more, no fewer.
        assert src.fetch_calls[urls[0]] == attempts
    finally:
        await store.close()
