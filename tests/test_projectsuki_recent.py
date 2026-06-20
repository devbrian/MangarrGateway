"""Unit tests for ``ProjectSukiSource.recent`` — homepage cards → DIRECT mint.

The recent feed is the homepage "Latest Chapters" section (``GET /``): each card
carries a book link ``/book/{bookId}``, the newest chapter link
``/read/{bookId}/{chapterId}/1``, a ``/group/{gid}`` anchor, and an "English" token.
Both ids are present, so ``recent`` mints DIRECT releases (no ``:DEFERRED``); the
minted handle carries the COMPOSITE ``{bookId}:{chapterId}`` chapter id. The route
owns the authoritative newest-first sort + ``since`` cut, so the whole page is
consumed here.

No network: a fake ``SourceContext`` serves the homepage HTML via ``get_bytes``.
"""

from __future__ import annotations

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.projectsuki import ProjectSukiSource

_HOME = "https://projectsuki.com/"


def _card(book_id: str, chapter_id: str, number: str, title: str) -> str:
    return (
        f'<div class="latest-card">'
        f'<a href="/book/{book_id}">{title}</a>'
        f'<a href="/read/{book_id}/{chapter_id}/1">Chapter {number}</a>'
        f'<a href="/group/9">Asura Scans</a>'
        f"<span>English</span><span>2026-06-19</span>"
        f"</div>"
    )


def _home_html(cards: list[str]) -> str:
    body = "".join(cards)
    return f"<html><body><section class='latest'>{body}</section></body></html>"


class _FakeCtxForRecent:
    """``SourceContext`` stand-in: serves the homepage HTML via ``get_bytes``."""

    def __init__(self, html: str) -> None:
        self.handle_store = HandleStore()
        self._html = html
        self.calls: list[str] = []

    async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
        self.calls.append(url)
        if url == _HOME:
            return self._html.encode()
        raise AssertionError(f"unexpected get_bytes url: {url}")


@pytest.mark.asyncio
async def test_recent_mints_direct_releases_per_card() -> None:
    ctx = _FakeCtxForRecent(
        _home_html(
            [
                _card("180716", "ch-221", "221", "Solo Leveling"),
                _card("42", "ch-50", "50", "Omniscient Reader"),
            ]
        )
    )
    releases = await ProjectSukiSource().recent(
        languages=None,
        limit=10,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
    )
    assert len(releases) == 2
    by_title = {r.manga_title: r for r in releases}
    assert by_title["Solo Leveling"].guid == "projectsuki:180716:ch-221:ch-221"
    assert by_title["Omniscient Reader"].guid == "projectsuki:42:ch-50:ch-50"
    # Composite chapter_id on the minted record.
    record = await ctx.handle_store.resolve(by_title["Solo Leveling"].download_handle)
    assert record is not None
    assert record.chapter_id == "180716:ch-221"
    # publishDate is RFC3339 (parsed from the card date).
    assert by_title["Solo Leveling"].publish_date == "2026-06-19T00:00:00+00:00"
    # Advisory group resolved from the /group/ anchor.
    assert by_title["Solo Leveling"].scanlation_group == "Asura Scans"


@pytest.mark.asyncio
async def test_recent_only_one_homepage_call() -> None:
    ctx = _FakeCtxForRecent(_home_html([_card("1", "c1", "1", "X")]))
    await ProjectSukiSource().recent(
        languages=None,
        limit=10,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
    )
    assert ctx.calls == [_HOME]


@pytest.mark.asyncio
async def test_recent_dedupes_repeated_cards() -> None:
    ctx = _FakeCtxForRecent(
        _home_html(
            [
                _card("1", "c1", "1", "X"),
                _card("1", "c1", "1", "X"),  # duplicate read anchor
            ]
        )
    )
    releases = await ProjectSukiSource().recent(
        languages=None,
        limit=10,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
    )
    assert len(releases) == 1


@pytest.mark.asyncio
async def test_recent_non_english_language_returns_empty() -> None:
    ctx = _FakeCtxForRecent(_home_html([_card("1", "c1", "1", "X")]))
    releases = await ProjectSukiSource().recent(
        languages=["ja"],
        limit=10,
        since=None,
        ctx=ctx,  # type: ignore[arg-type]
    )
    assert releases == []
    assert ctx.calls == []  # short-circuits before any network call
