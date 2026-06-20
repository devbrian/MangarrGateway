"""Unit tests for the Phase-13 external-links seam (offline, no network).

Two units under test:

* ``framework/external_links.normalize`` — the table-driven normalizer: per-source
  raw->canonical mapping (R2), the dropped-key policy (R3), and the hard no-``http``
  invariant. Fixtures are the EXACT live strings frozen in 13-RESEARCH (R8) — values
  are not invented, so a real upstream-shape drift would surface here / in live-smoke.
* ``SourceContext.resolve_external_links`` — the best-effort wrapper (D-03): a parse
  that RAISES or exceeds the timeout yields ``None`` (never fails the search); a parse
  returning an ``ExternalLinks`` is passed through unchanged. Built over a REAL
  ``SourceContext`` with ``enum_cache=None`` so ``cached_resolve`` is the bare
  pass-through and only the timeout + swallow logic is exercised.
"""

from __future__ import annotations

import asyncio

import pytest

from manga_gateway.framework import context as context_mod
from manga_gateway.framework.context import SourceContext
from manga_gateway.framework.external_links import normalize
from manga_gateway.framework.ratelimit import RateLimiter
from manga_gateway.framework.session import SessionManager
from manga_gateway.handles.store import HandleStore
from manga_gateway.models.search import ExternalLinks

# ─────────────────────────── frozen live fixtures (R8) ───────────────────────────
# {source_key: (raw_dict, expected_canonical_dump)}. raw_dict carries BOTH the mapped
# tracker keys and the storefront/official/raw keys that MUST be dropped (R3).

_COMIX_RAW = {
    "al": "https://anilist.co/manga/189223/",
    "mal": "https://myanimelist.net/manga/182157/",
    "mu": "https://www.mangaupdates.com/series/nx0v0fc/",
    "md": "https://mangadex.org/title/bc1a9a5d-0000-0000-0000-48954d3152ba/",
    "mb": "https://mangabaka.org/222572/",
    "amz": "https://www.amazon.com/dp/x",  # storefront -> DROP
}
_COMIX_EXPECTED = {
    "anilist": "189223",
    "myAnimeList": "182157",
    "mangaUpdates": "nx0v0fc",
    "mangaDex": "bc1a9a5d-0000-0000-0000-48954d3152ba",
    "mangaBaka": "222572",
}

_MANGADEX_RAW = {
    "al": "179445",
    "mal": "2",
    "mu": "njeqwry",
    "ap": "berserk",
    "kt": "8",
    "bw": "series/16664/list",
    "amz": "B0xxxx",  # DROP
    "cdj": "x",  # DROP
    "ebj": "x",  # DROP
    "raw": "https://official.example/raw",  # DROP
    "engtl": "https://official.example/en",  # DROP
    "nu": "12345",  # NovelUpdates -> DROP
    "bl": "181526",  # non-canonical -> DROP
}
_MANGADEX_EXPECTED = {
    "anilist": "179445",
    "myAnimeList": "2",
    "mangaUpdates": "njeqwry",
    "animePlanet": "berserk",
    "kitsu": "8",
    "bookwalker": "16664",
}

_ATSUMARU_RAW = {
    "anilistId": "105398",
    "malId": "121496",
    "mangaUpdatesId": "6z1uqw7",
    "mangaBakaId": "3397",
    "kitsuId": "54114",
    "apId": "solo-leveling",
    "annId": "32926",
    "kenmeiUrl": "https://www.kenmei.co/series/solo-leveling",
    "officialUrl": "https://official.example",  # not in map -> DROP
}
_ATSUMARU_EXPECTED = {
    "anilist": "105398",
    "myAnimeList": "121496",
    "mangaUpdates": "6z1uqw7",
    "mangaBaka": "3397",
    "kitsu": "54114",
    "animePlanet": "solo-leveling",
    "ann": "32926",
    "kenmei": "solo-leveling",
}

_MANGADOT_RAW = {
    "anilist_id": 105398,  # numeric -> stringify
    "mal_id": 121496,
    "mangaupdates_id": "6z1uqw7",
    "mangabaka_id": 3397,
    "kitsu_id": 54114,
    "mangadex_id": None,  # null -> DROP
    "source_url": "https://mangadot.example",  # not in map -> DROP
}
_MANGADOT_EXPECTED = {
    "anilist": "105398",
    "myAnimeList": "121496",
    "mangaUpdates": "6z1uqw7",
    "mangaBaka": "3397",
    "kitsu": "54114",
}

_WEEBCENTRAL_RAW = {
    "AniList": "https://anilist.co/manga/105398/Solo-Leveling/",
    "MangaUpdates": "https://www.mangaupdates.com/series/6z1uqw7/solo-leveling",
    "Official Source": "https://www.webtoons.com/en/x",  # DROP
}
_WEEBCENTRAL_EXPECTED = {
    "anilist": "105398",
    "mangaUpdates": "6z1uqw7",
}

_MANGAFIRE_RAW = {
    "anilist_id": "30002",
    "mal_id": "2",
    "manga_id": "8842",  # MangaFire internal -> DROP
    "page": "1",  # DROP
    "name": "Berserk",  # DROP
    "base_url": "https://mangafire.to",  # DROP
}
_MANGAFIRE_EXPECTED = {
    "anilist": "30002",
    "myAnimeList": "2",
}

_FIXTURES: dict[str, tuple[dict, dict]] = {
    "comix": (_COMIX_RAW, _COMIX_EXPECTED),
    "mangadex": (_MANGADEX_RAW, _MANGADEX_EXPECTED),
    "atsumaru": (_ATSUMARU_RAW, _ATSUMARU_EXPECTED),
    "mangadot": (_MANGADOT_RAW, _MANGADOT_EXPECTED),
    "weebcentral": (_WEEBCENTRAL_RAW, _WEEBCENTRAL_EXPECTED),
    "mangafire": (_MANGAFIRE_RAW, _MANGAFIRE_EXPECTED),
}

# Per-source keys that MUST NOT appear (canonical) in the output (R3 drop policy).
_DROPPED_CANONICAL = {
    "comix": "amazon",  # amz never maps to anything
    "mangadex": "novelUpdates",
    "atsumaru": "official",
    "mangadot": "mangaDex",  # mangadex_id was None -> dropped
    "weebcentral": "official",
    "mangafire": "manga_id",
}


# ─────────────────────────────── normalizer: mapping ───────────────────────────────


@pytest.mark.parametrize("source_key", list(_FIXTURES))
def test_normalize_maps_to_canonical(source_key: str) -> None:
    raw, expected = _FIXTURES[source_key]
    result = normalize(raw, source_key)
    assert result is not None
    assert result.model_dump(by_alias=True) == expected


# ─────────────────────────────── normalizer: drop policy ───────────────────────────


@pytest.mark.parametrize("source_key", list(_FIXTURES))
def test_normalize_drops_non_tracker_keys(source_key: str) -> None:
    raw, expected = _FIXTURES[source_key]
    result = normalize(raw, source_key)
    assert result is not None
    dump = result.model_dump(by_alias=True)
    # Only the expected canonical keys survive — every storefront/official/raw/internal
    # key in the raw fixture is structurally absent.
    assert set(dump) == set(expected)
    assert _DROPPED_CANONICAL[source_key] not in dump


# ─────────────────────────── normalizer: no-http invariant ─────────────────────────


@pytest.mark.parametrize("source_key", list(_FIXTURES))
def test_normalize_never_emits_http(source_key: str) -> None:
    raw, _ = _FIXTURES[source_key]
    result = normalize(raw, source_key)
    assert result is not None
    for value in result.model_dump(by_alias=True).values():
        assert "http" not in value, f"{source_key} leaked a URL: {value!r}"


# ─────────────────────────── normalizer: empty / all-dropped ───────────────────────


def test_normalize_empty_input_is_none() -> None:
    assert normalize({}, "comix") is None


def test_normalize_none_input_is_none() -> None:
    # IN-03: a None ``raw`` (cache miss) short-circuits to None, never TypeErrors —
    # so a source can drop its ``or {}`` guard and pass the .get() result straight in.
    assert normalize(None, "atsumaru") is None
    assert normalize(None, "comix") is None


def test_normalize_all_dropped_is_none() -> None:
    # Only non-mapped storefront keys present -> nothing survives -> None.
    assert normalize({"amz": "x", "raw": "y"}, "mangadex") is None


def test_normalize_unknown_source_is_none() -> None:
    assert normalize({"al": "1"}, "not-a-source") is None


def test_normalize_mangadot_null_field_dropped() -> None:
    # A null bare-ID field (mangadex_id=None) is dropped, not stringified to "None".
    result = normalize({"anilist_id": 1, "mangadex_id": None}, "mangadot")
    assert result is not None
    assert result.model_dump(by_alias=True) == {"anilist": "1"}


def test_normalize_bookwalker_extracts_numeric_from_path() -> None:
    # MangaDex bookwalker "series/N[/list]" -> bare numeric N (drop trailing /list).
    result = normalize({"bw": "series/441768"}, "mangadex")
    assert result is not None
    assert result.model_dump(by_alias=True) == {"bookwalker": "441768"}


def test_normalize_bookwalker_anchored_rejects_foreign_segment() -> None:
    # IN-01: ``series/`` must be anchored to start or a ``/`` boundary, so a foreign
    # path segment ending in ``series`` cannot false-match as a Bookwalker id.
    assert normalize({"bw": "webseries/42"}, "mangadex") is None
    # A genuine boundary-prefixed value still extracts.
    result = normalize({"bw": "x/series/777"}, "mangadex")
    assert result is not None
    assert result.model_dump(by_alias=True) == {"bookwalker": "777"}


def test_normalize_mangabaka_anchored_rejects_lookalike_host() -> None:
    # WR-04: a look-alike host whose name merely ends in ``mangabaka.org`` must NOT
    # have an id extracted; the real domain still works.
    assert normalize({"mb": "https://notmangabaka.org/123"}, "comix") is None
    assert normalize({"mb": "https://attackermangabaka.org/123"}, "comix") is None
    result = normalize({"mb": "https://mangabaka.org/222572/"}, "comix")
    assert result is not None
    assert result.model_dump(by_alias=True) == {"mangaBaka": "222572"}


# ───────────────────────── wrapper: resolve_external_links (D-03) ───────────────────


def _ctx() -> SourceContext:
    """A REAL SourceContext built offline (enum_cache=None -> cached_resolve is a bare
    pass-through, so only the timeout + swallow path is exercised)."""
    return SourceContext(
        source_key="comix",
        rate_limit_per_minute=6000,
        session=SessionManager(None),  # type: ignore[arg-type]
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
    )


@pytest.mark.asyncio
async def test_resolve_external_links_swallows_exception() -> None:
    async def _raises() -> ExternalLinks | None:
        raise RuntimeError("boom")

    assert await _ctx().resolve_external_links("s1", _raises) is None


@pytest.mark.asyncio
async def test_resolve_external_links_times_out_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_mod, "_EXTERNAL_LINKS_TIMEOUT_S", 0.05)

    async def _slow() -> ExternalLinks | None:
        await asyncio.sleep(0.2)
        return ExternalLinks(anilist="1")

    assert await _ctx().resolve_external_links("s2", _slow) is None


@pytest.mark.asyncio
async def test_resolve_external_links_returns_value() -> None:
    expected = ExternalLinks(anilist="105398")

    async def _ok() -> ExternalLinks | None:
        return expected

    result = await _ctx().resolve_external_links("s3", _ok)
    assert result is expected
