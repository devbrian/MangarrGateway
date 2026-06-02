"""Unit tests for ``MangaBallSource.search`` (Task 1).

``search`` POSTs ``/api/v1/title/search-advanced/`` via ``ctx.post_json`` (the
Plan-01 form-POST method), parses the standard ``{code,message,data,pagination}``
envelope, and for each Title resolves its ``translations`` into one Release per
translation. Each Release carries:

* the fully-specific guid
  ``mangaball:{title_id}:ch-{number_float}:{language}:{translation_id}`` (D-08);
* an opaque minted ``downloadHandle`` whose ``ResolutionRecord.chapter_id`` is the
  ``translation_id`` (the chapter-detail/download unit);
* ``page_count`` from ``translation.pages`` (reliable; ``size`` is not);
* HTML-string fields (``alternateName`` / ``status``) stripped — never raw.

No network: a fake ``SourceContext`` captures the POST URL + form body and serves
a canned envelope, exactly like ``tests/test_comix_recent.py``'s recent fake.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import SearchRequest
from manga_gateway.sources.mangaball import MangaBallSource

# guid contract (D-08): mangaball:{24-hex title}:ch-{float}:{lang}:{24-hex tx}
_GUID_RE = re.compile(r"^mangaball:[0-9a-f]{24}:ch-[\d.]+:[a-z]{2,}:[0-9a-f]{24}$")


class _FakeCtxForSearch:
    """Minimal ``SourceContext`` stand-in for the ``search`` post-path.

    ``search`` reads ``ctx`` only via :meth:`post_json` (one call) and
    ``handle_store.mint``. We capture the URL + form body for assertion and serve
    a canned envelope. The production ``SourceContext.post_json`` is the layer
    that touches httpx.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.handle_store = HandleStore()
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, data))
        return self._payload


def _ctx(payload: dict[str, Any]) -> Any:
    return _FakeCtxForSearch(payload)


def _translation(
    *,
    tx_id: str,
    language: str = "en",
    language_name: str = "English",
    group_name: str | None = "Rayquaza",
    date: str = "2026-06-01 23:33:42",
    pages: int = 66,
) -> dict[str, Any]:
    """One ``translation`` object (the release granularity; recon §3)."""
    group: dict[str, Any] | None = (
        {"_id": "daomeoden", "name": group_name, "icon": "/storage/x.png"}
        if group_name
        else None
    )
    return {
        "id": tx_id,
        "name": f"Chapter {language_name}",
        "language": language,
        "languageName": language_name,
        "group": group,
        "date": date,
        "pages": pages,
        "size": "0MB",  # unreliable — recon Gotchas
        "url": f"http://mangaball.net/chapter-detail/{tx_id}/",
        "volume": 0,
    }


def _title(
    *,
    title_id: str,
    name: str = "One Piece",
    number: str = "Ch. 1184.1",
    number_float: Any = 1184.1,
    translations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One Title with an embedded chapter+translations block.

    The search-advanced envelope returns Title objects; the per-chapter
    granularity is the ``translations`` list. Where a Title surfaces multiple
    chapters this test keeps it to one chapter with N translations (the
    multi-translation case that drives the per-translation guid mint).
    """
    return {
        "_id": title_id,
        "name": name,
        # HTML-string fields (recon Gotchas) — must be stripped, never raw.
        "alternateName": 'ワンピース<span class="text-muted">/</span>OP',
        "status": '<span class="badge">Ongoing</span>',
        "last_chapter": '<div class="lc"><a href="/x">Ch. 1184.1</a></div>',
        "url": f"http://mangaball.net/title-detail/one-piece-{title_id}/",
        "chapters": [
            {
                "number": number,
                "number_float": number_float,
                "title": "",
                "translations": translations
                or [_translation(tx_id="6a1e164ac01e2cf095f75b1a")],
            }
        ],
    }


def _envelope(titles: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": 200,
        "message": "ok",
        "data": titles,
        "pagination": {
            "total": len(titles),
            "limit": 28,
            "current_page": 1,
            "last_page": 1,
        },
    }


@pytest.mark.asyncio
async def test_search_posts_search_advanced_with_keyword() -> None:
    payload = _envelope([_title(title_id="68515540702284f8341784c8")])
    ctx = _ctx(payload)
    source = MangaBallSource()
    await source.search(SearchRequest(type="manga", query="one piece"), ctx)

    assert len(ctx.calls) == 1
    url, body = ctx.calls[0]
    assert url == "https://mangaball.net/api/v1/title/search-advanced/"
    # The keyword rides the recon-observed form field ``search_input``.
    assert body["search_input"] == "one piece"
    # The observed default filters are present (recon §1).
    assert body["filters[page]"] == 1
    assert body["filters[sort]"] == "updated_chapters_desc"


@pytest.mark.asyncio
async def test_search_mints_fully_specific_guid_and_opaque_handle() -> None:
    title_id = "68515540702284f8341784c8"
    tx_id = "6a1e164ac01e2cf095f75b1a"
    payload = _envelope(
        [
            _title(
                title_id=title_id,
                number_float=1184.1,
                translations=[_translation(tx_id=tx_id, language="vi")],
            )
        ]
    )
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="one piece"), ctx)

    assert len(releases) == 1
    rel = releases[0]
    # D-08 guid shape.
    assert _GUID_RE.match(rel.guid), rel.guid
    assert rel.guid == f"mangaball:{title_id}:ch-1184.1:vi:{tx_id}"
    # Opaque, non-empty handle.
    assert rel.download_handle
    assert ":" not in rel.download_handle  # not a structured composite
    # The handle resolves to a record whose chapter_id == the translation id.
    record = ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    assert record.chapter_id == tx_id
    assert record.source_key == "mangaball"
    assert record.page_count == 66  # from translation.pages, reliable
    assert rel.page_count == 66
    assert rel.publish_date == "2026-06-01 23:33:42"
    assert rel.language == "vi"
    assert rel.scanlation_group == "Rayquaza"
    assert rel.chapter_number == Decimal("1184.1")


@pytest.mark.asyncio
async def test_search_one_chapter_many_translations_mints_one_release_each() -> None:
    """A single number_float with N translations → N Releases, one per tx_id."""
    title_id = "68515540702284f8341784c8"
    txs = [
        _translation(tx_id="aaaaaaaaaaaaaaaaaaaaaaaa", language="en"),
        _translation(tx_id="bbbbbbbbbbbbbbbbbbbbbbbb", language="vi"),
        _translation(tx_id="cccccccccccccccccccccccc", language="es"),
    ]
    payload = _envelope([_title(title_id=title_id, translations=txs)])
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x"), ctx)

    assert len(releases) == 3
    langs = {rel.language for rel in releases}
    assert langs == {"en", "vi", "es"}
    # Every guid is distinct (per-translation uniqueness, D-08).
    guids = {rel.guid for rel in releases}
    assert len(guids) == 3
    for rel in releases:
        assert _GUID_RE.match(rel.guid), rel.guid


@pytest.mark.asyncio
async def test_search_strips_html_string_fields() -> None:
    """``alternateName`` / ``status`` HTML never reaches an emitted field value."""
    payload = _envelope([_title(title_id="68515540702284f8341784c8")])
    ctx = _ctx(payload)
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="x"), ctx)

    assert releases
    for rel in releases:
        # The title is composed from stripped fields — no raw HTML survives.
        assert "<" not in rel.title
        assert ">" not in rel.title
        assert "<" not in (rel.manga_title or "")


@pytest.mark.asyncio
async def test_search_empty_results_returns_no_releases() -> None:
    ctx = _ctx(_envelope([]))
    source = MangaBallSource()
    releases = await source.search(SearchRequest(type="manga", query="nothing"), ctx)
    assert releases == []
