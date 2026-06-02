"""Unit tests for MangaBall ``/recent`` DIRECT releases (Task 2, rebuilt for GAP-1).

The LIVE recent flow is TITLE-ONLY: ``POST /api/v1/title/search/``
(``search_type=getRecentlyUpdatedChapter``) returns titles with NO ``chapters``
key — the newest chapter is an HTML blob in each title's ``last_chapter`` field.
``_parse_last_chapter`` extracts the real ``translation_id``
(``href=".../chapter-detail/{id}/"``), number (``Ch. N``), language flag
(``<img alt/title="en">``), and group (``<a href="/group/{slug}/">``), and
``recent`` mints a DIRECT Release (the bare translation_id in the handle — NOT a
``:DEFERRED`` composite; MangaBall does not need the Comix late-bind).

The old fixtures fabricated a ``title["chapters"]`` shape the API never returns and
the DEFERRED machinery is gone from mangaball.py — those tests are RETIRED. The
DIRECT ``fetch_manifest(bare translation_id)`` path is covered by
``tests/test_mangaball_manifest.py`` and is not duplicated here.

No network: a fake ``SourceContext`` serves the canned recent envelope via
``post_json`` and records calls.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangaball import MangaBallSource, _parse_last_chapter

# DIRECT guid: mangaball:{24-hex title}:ch-{float}:{lang}:{24-hex tx} — NOT :DEFERRED
_DIRECT_GUID_RE = re.compile(
    r"^mangaball:[0-9a-f]{24}:ch-[\d.]+:[a-z-]{2,}:[0-9a-f]{24}$"
)


def _last_chapter_html(
    *,
    translation_id: str,
    number: str = "1184.1",
    language: str = "en",
    group_slug: str = "rayquaza",
    group_name: str = "Rayquaza",
    ago: str = "1d ago",
) -> str:
    """A realistic ``last_chapter`` HTML blob (GAP-1 locked structure)."""
    return (
        '<div class="d-flex align-items-center">'
        f'<a href="https://mangaball.net/chapter-detail/{translation_id}/">'
        f"Ch. {number}</a>"
        f'<img class="flag" alt="{language}" title="{language}" '
        'src="/storage/flags/x.png">'
        f'<a href="/group/{group_slug}/" title="{group_name}">{group_name}</a>'
        f'<span class="time">{ago}</span>'
        "</div>"
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


def _ctx(payload: dict[str, Any]) -> Any:
    return _FakeCtxForRecent(payload)


def _recent_title(
    *,
    title_id: str,
    name: str,
    last_chapter_html: str,
) -> dict[str, Any]:
    """A /recent TITLE-ONLY hit — NO ``chapters`` key; ``last_chapter`` is HTML."""
    return {
        "_id": title_id,
        "name": name,
        "status": "<span>Ongoing</span>",
        "last_chapter": last_chapter_html,
        "updated_at": "2026-06-01 23:33:42",
    }


def _recent_envelope(titles: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": 200, "message": "ok", "data": titles, "pagination": None}


# ───────────────────────────── _parse_last_chapter ──────────────────────────


def test_parse_last_chapter_extracts_all_fields() -> None:
    tx_id = "6a1e164ac01e2cf095f75b1a"
    html = _last_chapter_html(
        translation_id=tx_id,
        number="1184.1",
        language="vi",
        group_name="Rayquaza",
        ago="3h ago",
    )
    parsed = _parse_last_chapter(html)
    assert parsed is not None
    assert parsed["translation_id"] == tx_id
    assert parsed["number"] == "1184.1"
    assert parsed["language"] == "vi"
    assert parsed["group"] == "Rayquaza"
    assert "3h" in parsed["date_raw"]


def test_parse_last_chapter_returns_none_without_chapter_detail_anchor() -> None:
    html = '<div class="lc"><a href="/title-detail/foo/">Foo</a></div>'
    assert _parse_last_chapter(html) is None


def test_parse_last_chapter_returns_none_for_empty_blob() -> None:
    assert _parse_last_chapter("") is None


def test_parse_last_chapter_language_falls_back_to_en() -> None:
    """No flag <img> → language degrades gracefully to ``en``."""
    tx_id = "6a1e164ac01e2cf095f75b1a"
    html = (
        f'<div><a href="https://mangaball.net/chapter-detail/{tx_id}/">Ch. 5</a></div>'
    )
    parsed = _parse_last_chapter(html)
    assert parsed is not None
    assert parsed["language"] == "en"
    assert parsed["group"] is None
    assert parsed["number"] == "5"


# ───────────────────────────────── recent() shape ───────────────────────────


@pytest.mark.asyncio
async def test_recent_posts_search_with_recently_updated_type() -> None:
    payload = _recent_envelope(
        [
            _recent_title(
                title_id="68515540702284f8341784c8",
                name="One Piece",
                last_chapter_html=_last_chapter_html(
                    translation_id="6a1e164ac01e2cf095f75b1a"
                ),
            )
        ]
    )
    ctx = _ctx(payload)
    source = MangaBallSource()
    await source.recent(languages=None, limit=20, since=None, ctx=ctx)

    assert len(ctx.calls) == 1
    url, body = ctx.calls[0]
    assert url == "https://mangaball.net/api/v1/title/search/"
    assert body["search_type"] == "getRecentlyUpdatedChapter"
    assert body["page"] == 1


@pytest.mark.asyncio
async def test_recent_mints_direct_release_with_real_translation_id() -> None:
    title_id = "68515540702284f8341784c8"
    tx_id = "6a1e164ac01e2cf095f75b1a"
    payload = _recent_envelope(
        [
            _recent_title(
                title_id=title_id,
                name="One Piece",
                last_chapter_html=_last_chapter_html(
                    translation_id=tx_id, number="30.1", language="vi"
                ),
            )
        ]
    )
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.recent(languages=None, limit=20, since=None, ctx=ctx)

    assert len(releases) == 1
    rel = releases[0]
    # DIRECT guid — ends with the real translation_id, NOT ``:DEFERRED``.
    assert _DIRECT_GUID_RE.match(rel.guid), rel.guid
    assert not rel.guid.endswith(":DEFERRED")
    assert rel.guid == f"mangaball:{title_id}:ch-30.1:vi:{tx_id}"
    assert rel.chapter_number == Decimal("30.1")
    # The handle resolves to a record whose chapter_id is the bare translation_id.
    record = ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == tx_id
    assert "DEFERRED" not in record.chapter_id
    assert "|" not in record.chapter_id


@pytest.mark.asyncio
async def test_recent_publish_date_is_isoformat_parseable() -> None:
    payload = _recent_envelope(
        [
            _recent_title(
                title_id="a" * 24,
                name="A",
                last_chapter_html=_last_chapter_html(
                    translation_id="b" * 24, ago="5h ago"
                ),
            )
        ]
    )
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.recent(languages=None, limit=20, since=None, ctx=ctx)
    assert len(releases) == 1
    # Non-empty + parseable so the route's newest-first sort + `since` cut keep it.
    assert releases[0].publish_date
    datetime.fromisoformat(releases[0].publish_date.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_recent_language_filter_drops_unrequested() -> None:
    payload = _recent_envelope(
        [
            _recent_title(
                title_id="a" * 24,
                name="A",
                last_chapter_html=_last_chapter_html(
                    translation_id="1" * 24, language="en"
                ),
            ),
            _recent_title(
                title_id="b" * 24,
                name="B",
                last_chapter_html=_last_chapter_html(
                    translation_id="2" * 24, language="vi"
                ),
            ),
        ]
    )
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.recent(languages=["vi"], limit=20, since=None, ctx=ctx)
    assert len(releases) == 1
    assert releases[0].language == "vi"


@pytest.mark.asyncio
async def test_recent_skips_titles_with_unparseable_last_chapter() -> None:
    """A malformed/empty ``last_chapter`` is skipped — no crash, no release."""
    payload = _recent_envelope(
        [
            _recent_title(
                title_id="a" * 24,
                name="A",
                last_chapter_html="<div>no chapter-detail anchor here</div>",
            ),
            _recent_title(
                title_id="b" * 24,
                name="B",
                last_chapter_html=_last_chapter_html(translation_id="2" * 24),
            ),
        ]
    )
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.recent(languages=None, limit=20, since=None, ctx=ctx)
    assert len(releases) == 1
    assert releases[0].ids is not None
    assert releases[0].ids["mangaballTitleId"] == "b" * 24


@pytest.mark.asyncio
async def test_recent_respects_limit() -> None:
    payload = _recent_envelope(
        [
            _recent_title(
                title_id=f"{i:024x}",
                name=f"T{i}",
                last_chapter_html=_last_chapter_html(translation_id=f"{i:024x}"),
            )
            for i in range(5)
        ]
    )
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.recent(languages=None, limit=2, since=None, ctx=ctx)
    assert len(releases) == 2
