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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangaball import (
    MangaBallSource,
    _parse_last_chapter,
    _relative_to_iso,
)

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


def test_parse_last_chapter_skips_non_flag_img_before_flag() -> None:
    """WR-01: a preceding non-flag <img> must NOT poison ``language``.

    The blob is NOT guaranteed to hold only the flag <img>. With a group-icon img
    (``alt="Rayquaza Group"``) ordered BEFORE the real flag (``alt="vi"``), the old
    "first <img> with any alt/title" logic selected ``"rayquaza group"`` — a value
    with a space that breaks the ``[a-z-]`` guid shape and never matches a real
    BCP-47 code, wrongly dropping the release under a ``languages`` filter. The
    BCP-47-shape guard now skips the group icon and selects ``"vi"``.
    """
    tx_id = "6a1e164ac01e2cf095f75b1a"
    html = (
        "<div>"
        '<img class="group-icon" alt="Rayquaza Group" title="Rayquaza Group" '
        'src="/storage/groups/r.png">'
        f'<a href="https://mangaball.net/chapter-detail/{tx_id}/">Ch. 12</a>'
        '<img class="flag" alt="vi" title="vi" src="/storage/flags/vi.png">'
        "</div>"
    )
    parsed = _parse_last_chapter(html)
    assert parsed is not None
    assert parsed["language"] == "vi"


def test_parse_last_chapter_region_subtag_language_accepted() -> None:
    """WR-01: a BCP-47 region subtag (``pt-br``) is a valid language token."""
    tx_id = "6a1e164ac01e2cf095f75b1a"
    html = (
        "<div>"
        f'<a href="https://mangaball.net/chapter-detail/{tx_id}/">Ch. 7</a>'
        '<img class="flag" alt="pt-br" src="/storage/flags/ptbr.png">'
        "</div>"
    )
    parsed = _parse_last_chapter(html)
    assert parsed is not None
    assert parsed["language"] == "pt-br"


def test_parse_last_chapter_rejects_trailing_double_dot_number() -> None:
    """WR-04: a malformed ``Ch. 1.2.3`` must not yield an off-shape number.

    The anchored ``\\d+(?:\\.\\d+)?`` capture stops at ``"1.2"`` rather than greedily
    grabbing ``"1.2.3"`` (which ``_parse_decimal`` would reject → a ``ch-?`` guid /
    silent drop). A clean ``N``/``N.M`` capture is guaranteed.
    """
    tx_id = "6a1e164ac01e2cf095f75b1a"
    html = (
        f'<div><a href="https://mangaball.net/chapter-detail/{tx_id}/">'
        "Ch. 1.2.3</a></div>"
    )
    parsed = _parse_last_chapter(html)
    assert parsed is not None
    assert parsed["number"] == "1.2"


def test_parse_last_chapter_trailing_dot_number_clean() -> None:
    """WR-04: ``Ch. 23.`` captures a clean ``"23"`` (no trailing dot)."""
    tx_id = "6a1e164ac01e2cf095f75b1a"
    html = (
        f'<div><a href="https://mangaball.net/chapter-detail/{tx_id}/">'
        "Ch. 23.</a></div>"
    )
    parsed = _parse_last_chapter(html)
    assert parsed is not None
    assert parsed["number"] == "23"


# ──────────────────────────────── _relative_to_iso ──────────────────────────


def _ago_seconds(raw: str) -> float:
    """How many seconds ago ``_relative_to_iso`` resolved ``raw`` to."""
    iso = _relative_to_iso(raw)
    assert iso is not None
    return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()


def test_relative_to_iso_disambiguates_months_minutes() -> None:
    """WR-02: ``mo``/``min`` win over the bare ``m`` (longer-token-first regex)."""
    # bare ``m`` = minutes (observed MangaBall convention) → ~2 min, not ~2 months.
    assert _ago_seconds("2m ago") == pytest.approx(120, abs=5)
    # ``min`` is also minutes.
    assert _ago_seconds("5min ago") == pytest.approx(300, abs=5)
    # ``mo`` = months (~30d), NOT minutes.
    assert _ago_seconds("2mo ago") == pytest.approx(2 * 2592000, abs=5)


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
async def test_recent_returns_all_parseable_limit_left_to_route() -> None:
    """WR-03: the source no longer self-trims to ``limit`` in raw FEED order.

    Truncating in feed order before the route's authoritative newest-first sort
    would hide a genuinely-newer title sitting past position ``limit``, and —
    combined with skip-on-unparseable — could shrink the result below ``limit``.
    The source now returns every parseable release from the single page; the route
    (recent.py) sorts by publishDate + applies the merged ``limit``.
    """
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
    # All 5 parseable titles are returned; the route enforces the merged limit.
    assert len(releases) == 5
