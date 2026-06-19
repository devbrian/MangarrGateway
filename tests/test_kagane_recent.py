"""Unit tests for ``KaganeSource.recent`` — DIRECT mint from embedded latest_chapters.

The LIVE flow is ONE call (live recon 2026-06-09):
``POST /api/v2/search/series?sort=updated_at,desc`` (JSON body
``{"content_rating":["Safe","Suggestive"]}``) → newest-first series rows, each
EMBEDDING ``latest_chapters:[{book_id, chapter_no, created_at}]``. ``recent`` mints a
DIRECT release per row from ``latest_chapters[0]`` (the book_id is present — no
``:DEFERRED`` late-bind, no per-series fan-out).

No network: a fake ``SourceContext`` returns a canned search/series body via
``post_json_body`` and records the call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.kagane import KaganeSource

_SEARCH = "https://yuzuki.kagane.to/api/v2/search/series"


class _FakeCtxForRecent:
    def __init__(self, content: list[dict[str, Any]]) -> None:
        self.handle_store = HandleStore()
        self._content = content
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json_body(
        self,
        url: str,
        *,
        body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((url, {"body": body, "params": params}))
        return {"content": self._content}


def _series_with_latest(
    *,
    series_id: str,
    title: str = "Reincarnator",
    lang: str = "en",
    book_id: str = "lb1",
    chapter_no: Any = 138,
) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "title": title,
        "translated_language": lang,
        "latest_chapters": [
            {
                "book_id": book_id,
                "title": f"Episode {chapter_no}",
                "chapter_no": str(chapter_no),
                "volume_no": None,
                "created_at": "2026-06-09T01:54:55.063310+00:00",
                "available_at": "2026-06-09T01:54:55.063310+00:00",
            }
        ],
    }


@pytest.mark.asyncio
async def test_recent_one_call_sorted_updated_desc() -> None:
    ctx = _FakeCtxForRecent([_series_with_latest(series_id="s1")])
    releases = await KaganeSource().recent(
        languages=None, limit=50, since=None, ctx=ctx
    )
    assert len(ctx.calls) == 1  # ONE call, no per-series fan-out
    url, call = ctx.calls[0]
    assert url == _SEARCH
    assert call["params"]["sort"] == "updated_at,desc"
    assert call["body"]["content_rating"] == ["Safe", "Suggestive"]
    assert "title" not in call["body"]  # recent has no keyword
    assert len(releases) == 1


@pytest.mark.asyncio
async def test_recent_mints_direct_release_from_latest_chapter() -> None:
    ctx = _FakeCtxForRecent(
        [_series_with_latest(series_id="s1", book_id="lb1", chapter_no=138)]
    )
    releases = await KaganeSource().recent(
        languages=None, limit=50, since=None, ctx=ctx
    )
    rel = releases[0]
    assert rel.guid == "kagane:s1:ch-138:lb1"
    assert rel.chapter_number == Decimal("138")
    assert rel.publish_date == "2026-06-09T01:54:55.063310+00:00"
    record = await ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == "lb1"  # bare book_id
    assert rel.ids == {"kaganeSeriesId": "s1", "kaganeBookId": "lb1"}


@pytest.mark.asyncio
async def test_recent_skips_series_without_latest_chapter() -> None:
    ctx = _FakeCtxForRecent(
        [
            {"series_id": "s1", "title": "Empty", "latest_chapters": []},
            _series_with_latest(series_id="s2", book_id="lb2"),
        ]
    )
    releases = await KaganeSource().recent(
        languages=None, limit=50, since=None, ctx=ctx
    )
    assert len(releases) == 1
    assert releases[0].guid.endswith(":lb2")


@pytest.mark.asyncio
async def test_recent_non_matching_language_filtered() -> None:
    ctx = _FakeCtxForRecent([_series_with_latest(series_id="s1", lang="en")])
    releases = await KaganeSource().recent(
        languages=["ja"], limit=50, since=None, ctx=ctx
    )
    assert releases == []
