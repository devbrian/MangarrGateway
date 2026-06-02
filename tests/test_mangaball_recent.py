"""Unit tests for MangaBall ``/recent`` deferred resolution (Task 2, D-10/D-11).

Coverage mirrors ``tests/test_comix_recent.py`` adapted to MangaBall's shapes:

* :meth:`MangaBallSource.recent` — POSTs ``/api/v1/title/search/`` with
  ``search_type=getRecentlyUpdatedChapter`` and synthesizes one DEFERRED Release
  per promised chapter (guid ``…:DEFERRED``, composite chapter_id in the handle).
* :func:`_make_deferred_composite` / parse round-trip — the
  ``DEFERRED|{title_id}|{number}|{language}`` composite the handle carries.
* :func:`_resolve_deferred` — Decimal-aware strict-match against the FLAT
  ``ALL_CHAPTERS`` chapter listing, tie-break newest absolute ``date`` →
  non-empty ``group`` → lowest ``id``, fail-closed on zero matches.
* :meth:`MangaBallSource.fetch_manifest` DEFERRED branch — detects the sentinel,
  late-binds title_id→translation_id, and NEVER rewrites the ``:DEFERRED`` guid.

No network: a fake ``SourceContext`` serves the canned recent envelope + the
chapter-listing envelope and records calls.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from manga_gateway.framework.errors import SourceError
from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.mangaball import (
    _DEFERRED_SENTINEL,
    MangaBallSource,
    _decode_deferred_composite,
    _DeferredResolutionError,
    _make_deferred_composite,
    _resolve_deferred,
)

_DEFERRED_GUID_RE = re.compile(r"^mangaball:[0-9a-f]{24}:ch-[\d.]+:[a-z]{2,}:DEFERRED$")


# ─────────────────────── composite encode/decode round-trip ─────────────────


def test_make_deferred_composite_well_formed() -> None:
    comp = _make_deferred_composite("68515540702284f8341784c8", "1184.1", "vi")
    assert comp == "DEFERRED|68515540702284f8341784c8|1184.1|vi"


def test_deferred_composite_roundtrips() -> None:
    comp = _make_deferred_composite("68515540702284f8341784c8", "23", "en")
    sentinel, title_id, number, language = _decode_deferred_composite(comp)
    assert sentinel == _DEFERRED_SENTINEL == "DEFERRED"
    assert title_id == "68515540702284f8341784c8"
    assert number == "23"
    assert language == "en"


def test_make_deferred_composite_rejects_empty_segments() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("", "23", "en")
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("t", "", "en")
    with pytest.raises(ValueError, match="non-empty"):
        _make_deferred_composite("t", "23", "")


# ─────────────────────────── _resolve_deferred (D-11) ───────────────────────


def _tx(
    *,
    tx_id: str,
    language: str = "vi",
    group_name: str | None = "Rayquaza",
    date: str = "2026-06-01 23:33:42",
    pages: int = 66,
) -> dict[str, Any]:
    group = {"_id": "g", "name": group_name} if group_name else None
    return {
        "id": tx_id,
        "language": language,
        "group": group,
        "date": date,
        "pages": pages,
    }


def _chapter(
    *, number_float: Any, translations: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"number_float": number_float, "translations": translations}


def test_resolve_deferred_int_matches() -> None:
    chapters = [
        _chapter(number_float=23.0, translations=[_tx(tx_id="a" * 24)]),
        _chapter(number_float=22.0, translations=[_tx(tx_id="b" * 24)]),
    ]
    tx = _resolve_deferred("23", "vi", chapters)
    assert tx["id"] == "a" * 24


def test_resolve_deferred_decimal_aware_23_matches_23_0() -> None:
    """A '23'-keyed handle resolves against a 23.0 chapter (Decimal-aware)."""
    chapters = [_chapter(number_float="23.0", translations=[_tx(tx_id="c" * 24)])]
    tx = _resolve_deferred("23", "vi", chapters)
    assert tx["id"] == "c" * 24


def test_resolve_deferred_filters_by_language() -> None:
    """Only translations matching the promised language are eligible."""
    chapters = [
        _chapter(
            number_float=23.0,
            translations=[
                _tx(tx_id="e" * 24, language="en"),
                _tx(tx_id="v" * 24, language="vi"),
            ],
        )
    ]
    tx = _resolve_deferred("23", "vi", chapters)
    assert tx["id"] == "v" * 24


def test_resolve_deferred_tiebreak_newest_date_wins() -> None:
    chapters = [
        _chapter(
            number_float=10.0,
            translations=[
                _tx(tx_id="o" * 24, date="2026-05-01 00:00:00", group_name="Old"),
                _tx(tx_id="n" * 24, date="2026-06-01 00:00:00", group_name="New"),
                _tx(tx_id="m" * 24, date="2026-05-15 00:00:00", group_name="Mid"),
            ],
        )
    ]
    tx = _resolve_deferred("10", "vi", chapters)
    assert tx["id"] == "n" * 24  # newest absolute date


def test_resolve_deferred_tiebreak_non_empty_group_over_empty() -> None:
    same_date = "2026-06-01 00:00:00"
    chapters = [
        _chapter(
            number_float=10.0,
            translations=[
                _tx(tx_id="1" * 24, date=same_date, group_name=None),
                _tx(tx_id="2" * 24, date=same_date, group_name="G"),
            ],
        )
    ]
    tx = _resolve_deferred("10", "vi", chapters)
    assert tx["id"] == "2" * 24


def test_resolve_deferred_tiebreak_lowest_id_last_resort() -> None:
    same_date = "2026-06-01 00:00:00"
    chapters = [
        _chapter(
            number_float=10.0,
            translations=[
                _tx(tx_id="000000000000000000000002", date=same_date, group_name="G"),
                _tx(tx_id="000000000000000000000001", date=same_date, group_name="G"),
            ],
        )
    ]
    tx = _resolve_deferred("10", "vi", chapters)
    assert tx["id"] == "000000000000000000000001"


def test_resolve_deferred_missing_chapter_raises_strict() -> None:
    """Fail-closed: a promised chapter absent from ALL_CHAPTERS raises (D-10)."""
    chapters = [
        _chapter(number_float=24.0, translations=[_tx(tx_id="a" * 24)]),
        _chapter(number_float=22.0, translations=[_tx(tx_id="b" * 24)]),
    ]
    with pytest.raises(_DeferredResolutionError, match="not present"):
        _resolve_deferred("23", "vi", chapters)


def test_resolve_deferred_matched_number_wrong_language_raises() -> None:
    """A chapter exists but only in another language → fail-closed."""
    chapters = [
        _chapter(number_float=23.0, translations=[_tx(tx_id="e" * 24, language="en")])
    ]
    with pytest.raises(_DeferredResolutionError):
        _resolve_deferred("23", "vi", chapters)


def test_resolve_deferred_skips_malformed_rows() -> None:
    chapters: list[Any] = [
        {"number_float": None, "translations": []},
        "not-a-dict",
        None,
        _chapter(number_float=5.0, translations=[_tx(tx_id="f" * 24)]),
    ]
    tx = _resolve_deferred("5", "vi", chapters)
    assert tx["id"] == "f" * 24


# ─────────────────────────────── recent() shape ─────────────────────────────


class _FakeCtxForRecent:
    """``SourceContext`` stand-in: serves a canned recent envelope via post_json."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.handle_store = HandleStore()
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, data))
        return self._payload


def _recent_title(
    *,
    title_id: str,
    name: str,
    number_float: Any,
    language: str = "vi",
) -> dict[str, Any]:
    """A /recent Title carrying one promised chapter (its newest)."""
    return {
        "_id": title_id,
        "name": name,
        "status": "<span>Ongoing</span>",
        "chapters": [
            {
                "number_float": number_float,
                "translations": [
                    {
                        "id": "f" * 24,
                        "language": language,
                        "date": "2026-06-01 23:33:42",
                        "group": {"name": "Rayquaza"},
                        "pages": 10,
                    }
                ],
            }
        ],
    }


def _recent_envelope(titles: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": 200, "message": "ok", "data": titles, "pagination": None}


@pytest.mark.asyncio
async def test_recent_posts_search_with_recently_updated_type() -> None:
    payload = _recent_envelope(
        [
            _recent_title(
                title_id="68515540702284f8341784c8",
                name="One Piece",
                number_float=1184.1,
            )
        ]
    )
    ctx = _FakeCtxForRecent(payload)
    source = MangaBallSource()
    await source.recent(languages=None, limit=20, since=None, ctx=ctx)

    assert len(ctx.calls) == 1
    url, body = ctx.calls[0]
    assert url == "https://mangaball.net/api/v1/title/search/"
    assert body["search_type"] == "getRecentlyUpdatedChapter"
    assert body["page"] == 1


@pytest.mark.asyncio
async def test_recent_mints_deferred_releases() -> None:
    payload = _recent_envelope(
        [
            _recent_title(
                title_id="68515540702284f8341784c8",
                name="One Piece",
                number_float="30.1",
                language="vi",
            )
        ]
    )
    ctx = _FakeCtxForRecent(payload)
    source = MangaBallSource()
    releases = await source.recent(languages=None, limit=20, since=None, ctx=ctx)

    assert len(releases) == 1
    rel = releases[0]
    assert _DEFERRED_GUID_RE.match(rel.guid), rel.guid
    assert rel.guid.endswith(":DEFERRED")
    assert ":ch-30.1:" in rel.guid
    assert rel.chapter_number == Decimal("30.1")
    # The handle resolves to a DEFERRED composite (translation_id dropped).
    record = ctx.handle_store.resolve(rel.download_handle)
    assert record is not None
    sentinel, title_id, number, language = _decode_deferred_composite(record.chapter_id)
    assert sentinel == _DEFERRED_SENTINEL
    assert title_id == "68515540702284f8341784c8"
    assert number == "30.1"
    assert language == "vi"


@pytest.mark.asyncio
async def test_recent_since_cuts_older_items() -> None:
    payload = _recent_envelope(
        [
            _recent_title(title_id="a" * 24, name="A", number_float=1.0),
            _recent_title(title_id="b" * 24, name="B", number_float=2.0),
        ]
    )
    ctx = _FakeCtxForRecent(payload)
    source = MangaBallSource()
    # Both translations carry date 2026-06-01 (space-separated); an ISO-`T` `since`
    # far in the future cuts everything (WR-01: parsed-datetime compare, not lexical).
    releases = await source.recent(
        languages=None, limit=20, since="2027-01-01T00:00:00+00:00", ctx=ctx
    )
    assert releases == []


@pytest.mark.asyncio
async def test_recent_since_keeps_genuinely_newer_space_separated_date() -> None:
    """WR-01 regression: a genuinely-newer space-separated chapter date must NOT
    be dropped against an ISO-`T` `since`.

    The chapter date ``"2026-06-01 23:33:42"`` is LATER than the ISO `since`
    ``"2026-06-01T20:00:00+00:00"``, but the space byte (0x20) sorts before `T`
    (0x54), so a raw lexical ``date <= since`` compare would wrongly drop it. The
    parsed-datetime compare keeps it."""
    payload = _recent_envelope(
        [_recent_title(title_id="a" * 24, name="A", number_float=1.0)]
    )
    ctx = _FakeCtxForRecent(payload)
    source = MangaBallSource()
    releases = await source.recent(
        languages=None, limit=20, since="2026-06-01T20:00:00+00:00", ctx=ctx
    )
    assert len(releases) == 1, "a chapter genuinely newer than `since` was dropped"


# ──────────────────── fetch_manifest DEFERRED late-bind branch ───────────────
#
# Task 2 verifies the DEFERRED detect → chapter-listing POST → strict-match →
# resolved translation_id handoff. The manifest TAIL (HTML img[data-src] extract
# + SSRF allowlist + pages-count guard) is Task 3; here we monkeypatch
# ``_manifest_for_translation`` so this test isolates the late-bind branch.


class _FakeCtxForManifest:
    """Serves the chapter-listing flat envelope (post_json). Records calls."""

    def __init__(self, *, listing: dict[str, Any]) -> None:
        self.handle_store = HandleStore()
        self._listing = listing
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        self.post_calls.append((url, data))
        return self._listing


def _listing_envelope(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": 200, "message": "ok", "ALL_CHAPTERS": chapters}


@pytest.mark.asyncio
async def test_fetch_manifest_deferred_resolves_to_translation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DEFERRED handle late-binds title_id→translation_id via the chapter
    listing, then hands the RESOLVED id (never the sentinel) to the manifest
    tail."""
    title_id = "68515540702284f8341784c8"
    tx_id = "6a1e164ac01e2cf095f75b1a"
    composite = _make_deferred_composite(title_id, "23", "vi")
    listing = _listing_envelope(
        [
            _chapter(
                number_float=23.0,
                translations=[_tx(tx_id=tx_id, language="vi", pages=2)],
            ),
            _chapter(number_float=22.0, translations=[_tx(tx_id="z" * 24)]),
        ]
    )
    ctx = _FakeCtxForManifest(listing=listing)

    seen_ids: list[str] = []

    async def fake_manifest_for_translation(
        self: MangaBallSource,
        translation_id: str,
        pages: int | None,
        ctx_: Any,
    ) -> list[str]:
        seen_ids.append(translation_id)
        return ["https://cdn.example.net/storage/t/0/23/g/vi/x-001.jpg"]

    monkeypatch.setattr(
        MangaBallSource, "_manifest_for_translation", fake_manifest_for_translation
    )

    source = MangaBallSource()
    urls = await source.fetch_manifest(composite, ctx)  # type: ignore[arg-type]

    assert urls  # the fake returns a non-empty list
    # The chapter-listing POST used the title_id from the composite.
    assert len(ctx.post_calls) == 1
    listing_url, listing_body = ctx.post_calls[0]
    assert listing_url.endswith("/api/v1/chapter/chapter-listing-by-title-id/")
    assert listing_body["title_id"] == title_id
    # The manifest tail received the RESOLVED translation id (not the sentinel).
    assert seen_ids == [tx_id]
    assert "DEFERRED" not in seen_ids[0]


@pytest.mark.asyncio
async def test_fetch_manifest_deferred_missing_chapter_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DEFERRED handle whose chapter is absent raises source_unavailable and
    never reaches the manifest tail (no silent rebind, D-10)."""
    composite = _make_deferred_composite("68515540702284f8341784c8", "23", "vi")
    listing = _listing_envelope(
        [_chapter(number_float=24.0, translations=[_tx(tx_id="z" * 24)])]
    )
    ctx = _FakeCtxForManifest(listing=listing)

    tail_calls: list[str] = []

    async def fake_manifest_for_translation(
        self: MangaBallSource,
        translation_id: str,
        pages: int | None,
        ctx_: Any,
    ) -> list[str]:
        tail_calls.append(translation_id)
        return []

    monkeypatch.setattr(
        MangaBallSource, "_manifest_for_translation", fake_manifest_for_translation
    )

    source = MangaBallSource()
    with pytest.raises(SourceError) as excinfo:
        await source.fetch_manifest(composite, ctx)  # type: ignore[arg-type]
    assert excinfo.value.code == "source_unavailable"
    # Failed BEFORE the manifest tail.
    assert tail_calls == []
