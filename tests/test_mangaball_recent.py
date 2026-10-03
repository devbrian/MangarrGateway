"""Unit tests for MangaBall ``/recent`` DIRECT releases (261003-mangaball-api-v2).

The v2 recent flow: ``POST /api/v1/title/search``
(``search_type=getRecentlyUpdatedChapter``) returns titles, each carrying
``recent_chapters`` rows of the SAME shape as the chapter listing (``last_chapter``
is now None — there is no HTML to parse). ``recent`` mints ONE DIRECT Release per
row: the row ``id`` lands in ``ResolutionRecord.chapter_id`` (no ``:DEFERRED``
composite), and ``publishDate`` comes from the row ``created_at`` (falling back to
the title ``updated_at``).

The DIRECT ``fetch_manifest(row id)`` path is covered by
``tests/test_mangaball_manifest.py`` and is not duplicated here.

No network: a fake ``SourceContext`` serves the canned recent envelope via
``post_json`` and records calls.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import Release
from manga_gateway.sources.mangaball import MangaBallSource
from tests.test_mangaball_search import _chapter, _title

# DIRECT guid: mangaball:{24-hex title}:ch-{float}:{lang}:{24-hex row} — NOT :DEFERRED
_DIRECT_GUID_RE = re.compile(
    r"^mangaball:[0-9a-f]{24}:ch-[\d.]+:[a-z-]{2,}:[0-9a-f]{24}$"
)


class _FakeCtxForRecent:
    """``SourceContext`` stand-in: serves a canned recent envelope via post_json."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.handle_store = HandleStore()
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, data))
        return self._payload


def _ctx(titles: list[dict[str, Any]]) -> Any:
    return _FakeCtxForRecent(
        {"code": 200, "message": "ok", "data": titles, "pagination": None}
    )


def _recent_row(**kw: Any) -> dict[str, Any]:
    """A ``recent_chapters`` row — the listing row shape minus ``views``."""
    row = _chapter(**kw)
    row.pop("views", None)
    return row


async def _recent(
    ctx: Any, *, languages: list[str] | None = None, limit: int = 20
) -> list[Release]:
    return await MangaBallSource().recent(
        languages=languages, limit=limit, since=None, ctx=ctx
    )


@pytest.mark.asyncio
async def test_recent_posts_search_with_recently_updated_type() -> None:
    ctx = _ctx([_title(title_id="a" * 24, recent_chapters=[_recent_row()])])
    await _recent(ctx)

    assert len(ctx.calls) == 1
    url, body = ctx.calls[0]
    assert url == "https://mangaball.com/api/v1/title/search"
    assert body["search_type"] == "getRecentlyUpdatedChapter"
    assert body["page"] == 1


@pytest.mark.asyncio
async def test_recent_mints_direct_release_with_row_id() -> None:
    title_id = "68515540702284f8341784c8"
    row_id = "6a1e164ac01e2cf095f75b1a"
    ctx = _ctx(
        [
            _title(
                title_id=title_id,
                recent_chapters=[_recent_row(row_id=row_id, number=30.1, lang="vi")],
            )
        ]
    )
    releases = await _recent(ctx)

    assert len(releases) == 1
    rel = releases[0]
    assert _DIRECT_GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == f"mangaball:{title_id}:ch-30.1:vi:{row_id}"
    assert rel.chapter_number == Decimal("30.1")
    assert rel.votes is None  # recent rows carry no views
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == row_id
    assert record.page_count is None


@pytest.mark.asyncio
async def test_recent_publish_date_from_created_at() -> None:
    ctx = _ctx(
        [
            _title(
                title_id="a" * 24,
                recent_chapters=[_recent_row(created_at="2026-10-02T08:15:00")],
            )
        ]
    )
    releases = await _recent(ctx)
    assert releases[0].publish_date == "2026-10-02T08:15:00+00:00"


@pytest.mark.asyncio
async def test_recent_publish_date_falls_back_to_title_updated_at() -> None:
    row = _recent_row()
    del row["created_at"]
    title = _title(title_id="a" * 24, recent_chapters=[row])
    title["updated_at"] = "2026-09-30T12:00:00"
    releases = await _recent(_ctx([title]))
    assert releases[0].publish_date == "2026-09-30T12:00:00+00:00"


@pytest.mark.asyncio
async def test_recent_language_filter_maps_site_codes() -> None:
    """The filter applies to the MAPPED code (``kr`` → ``ko``)."""
    ctx = _ctx(
        [
            _title(
                title_id="a" * 24,
                recent_chapters=[
                    _recent_row(row_id="1" * 24, lang="en"),
                    _recent_row(row_id="2" * 24, lang="kr"),
                ],
            ),
            _title(
                title_id="b" * 24,
                recent_chapters=[_recent_row(row_id="3" * 24, lang="vi")],
            ),
        ]
    )
    releases = await _recent(ctx, languages=["ko", "vi"])
    assert sorted(r.language for r in releases) == ["ko", "vi"]


@pytest.mark.asyncio
async def test_recent_skips_titles_without_recent_chapters() -> None:
    """Empty/missing ``recent_chapters`` yields nothing — no crash."""
    no_key = _title(title_id="c" * 24)
    del no_key["recent_chapters"]
    ctx = _ctx(
        [
            _title(title_id="a" * 24, recent_chapters=[]),
            no_key,
            _title(title_id="b" * 24, recent_chapters=[_recent_row()]),
        ]
    )
    releases = await _recent(ctx)
    assert len(releases) == 1
    assert releases[0].ids is not None
    assert releases[0].ids["mangaballTitleId"] == "b" * 24


@pytest.mark.asyncio
async def test_recent_multi_row_newest_first_across_titles() -> None:
    ctx = _ctx(
        [
            _title(
                title_id="a" * 24,
                recent_chapters=[
                    _recent_row(row_id="1" * 24, created_at="2026-10-01T00:00:00"),
                    _recent_row(row_id="2" * 24, created_at="2026-10-03T00:00:00"),
                ],
            ),
            _title(
                title_id="b" * 24,
                recent_chapters=[
                    _recent_row(row_id="3" * 24, created_at="2026-10-02T00:00:00")
                ],
            ),
        ]
    )
    releases = await _recent(ctx)
    assert [r.ids["mangaballTranslationId"] for r in releases if r.ids] == [
        "2" * 24,
        "3" * 24,
        "1" * 24,
    ]


@pytest.mark.asyncio
async def test_recent_returns_whole_page_limit_left_to_route() -> None:
    """WR-03: the source does not self-trim to ``limit`` in raw feed order — the
    route (recent.py) sorts by publishDate and applies the merged ``limit``."""
    ctx = _ctx(
        [
            _title(
                title_id=f"{i:024x}",
                recent_chapters=[_recent_row(row_id=f"{i:024x}")],
            )
            for i in range(5)
        ]
    )
    releases = await _recent(ctx, limit=2)
    assert len(releases) == 5
