"""Table-driven external-tracker normalizer (Phase 13, R2/R3/R8).

The single shared choke point that turns a source's raw, third-party-controlled
tracker dict into a typed :class:`~manga_gateway.models.search.ExternalLinks` carrying
ONLY bare canonical IDs/slugs (never a URL). Every one of the six sources routes its
already-parsed raw links through :func:`normalize` — no source re-implements URL->ID
extraction or the canonical-key mapping (CLAUDE.md "shared framework, thin sources").

Two invariants this module owns (the security surface, threat T-13-02/T-13-03):

* **Drop everything not in the per-source map.** Storefront/official/raw keys
  (Amazon ``amz``, ``ebj``, ``cdj``, ``raw``, ``engtl``, NovelUpdates ``nu``, ``bl``,
  WeebCentral ``Official Source``, MangaFire internal ``manga_id``/``page``, ...) have
  NO entry in any map and are structurally dropped (R3) — never passed through.
* **No emitted value contains ``"http"``.** Full-URL sources (Comix, WeebCentral, and
  Atsumaru's ``kenmeiUrl``) extract the bare ID/slug via a regex capture; the final
  ``"http" not in value`` guard is the hard belt-and-braces choke point (R2) so a
  reflected upstream URL can never reach the wire.

The frozen per-source raw->canonical maps below are copied VERBATIM from the
13-RESEARCH frozen table (R8). Adding/removing a tracker is a one-line map edit.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..models.search import ExternalLinks

if TYPE_CHECKING:
    from collections.abc import Callable

# ─────────────────────────── URL/path extraction regexes ───────────────────────────
# Each captures the bare ID/slug from a full-URL or path value (live-verified shapes,
# 13-RESEARCH "Value-format invariants").
_AL = re.compile(r"/manga/(\d+)")  # anilist / myAnimeList numeric, .../manga/{digits}
_MU = re.compile(r"/series/([0-9a-z]+)")  # mangaUpdates base36, .../series/{base36}
_MD = re.compile(r"/title/([0-9a-f-]{36})")  # mangaDex uuid, .../title/{uuid}
# mangaBaka numeric, mangabaka.org/{digits}. The negative lookbehind anchors the
# domain so a hostile look-alike host whose name merely ENDS in ``mangabaka.org``
# (``notmangabaka.org/123``, ``attackermangabaka.org/123``) cannot false-match — the
# char preceding ``mangabaka`` must be a host/scheme separator, not another label char
# (WR-04 / R2 defensive posture).
_MB = re.compile(r"(?<![a-z0-9-])mangabaka\.org/(\d+)")
# bookwalker numeric, MangaDex "series/N[/list]" (a bare PATH fragment, not a full
# URL — so the IN-01 domain-anchor suggestion does not apply to this value shape).
# Anchor ``series/`` to string-start or a ``/`` boundary so a foreign path segment like
# ``.../aseries/42`` or ``.../webseries/42`` cannot false-match as a Bookwalker id.
_BW = re.compile(r"(?:^|/)series/(\d+)")
_KEN = re.compile(r"/series/([^/?#]+)")  # kenmei slug, .../series/{slug}


def pass_through(value: Any) -> str | None:
    """Bare-ID value already in final form — str-cast verbatim, drop falsy/empty."""
    if value is None:
        return None
    return str(value) or None


def stringify(value: Any) -> str | None:
    """Numeric bare-ID value (e.g. Mangadot ``anilist_id=105398``) -> ``str``.

    Drops ``None`` (a ``null`` tracker field, e.g. Mangadot ``mangadex_id=None``).
    """
    if value is None:
        return None
    return str(value)


def path_after(pattern: re.Pattern[str]) -> Callable[[Any], str | None]:
    """Build an extractor returning ``pattern``'s first capture group, or ``None``.

    A non-matching value (or ``None``) yields ``None`` and is skipped — so a malformed
    upstream value never reaches the wire (threat T-13-02).
    """

    def _extract(value: Any) -> str | None:
        if value is None:
            return None
        match = pattern.search(str(value))
        return match.group(1) if match else None

    return _extract


# ─────────────────────── per-source {raw_key: (canonical, extractor)} ───────────────
# VERBATIM from 13-RESEARCH "FROZEN Per-Source Tracker Table" (R8). The canonical keys
# ARE the ExternalLinks camelCase aliases.

_COMIX_MAP: dict[str, tuple[str, Callable[[Any], str | None]]] = {
    "al": ("anilist", path_after(_AL)),
    "mal": ("myAnimeList", path_after(_AL)),
    "mu": ("mangaUpdates", path_after(_MU)),
    "md": ("mangaDex", path_after(_MD)),
    "mb": ("mangaBaka", path_after(_MB)),
}

_MANGADEX_MAP: dict[str, tuple[str, Callable[[Any], str | None]]] = {
    "al": ("anilist", pass_through),
    "mal": ("myAnimeList", pass_through),
    "mu": ("mangaUpdates", pass_through),
    "ap": ("animePlanet", pass_through),
    "kt": ("kitsu", pass_through),
    "bw": ("bookwalker", path_after(_BW)),
}

_ATSUMARU_MAP: dict[str, tuple[str, Callable[[Any], str | None]]] = {
    "anilistId": ("anilist", pass_through),
    "malId": ("myAnimeList", pass_through),
    "mangaUpdatesId": ("mangaUpdates", pass_through),
    "mangaBakaId": ("mangaBaka", pass_through),
    "kitsuId": ("kitsu", pass_through),
    "apId": ("animePlanet", pass_through),
    "annId": ("ann", pass_through),
    "kenmeiUrl": ("kenmei", path_after(_KEN)),
}

_MANGADOT_MAP: dict[str, tuple[str, Callable[[Any], str | None]]] = {
    "anilist_id": ("anilist", stringify),
    "mal_id": ("myAnimeList", stringify),
    "mangaupdates_id": ("mangaUpdates", pass_through),
    "mangabaka_id": ("mangaBaka", stringify),
    "kitsu_id": ("kitsu", stringify),
    "mangadex_id": ("mangaDex", stringify),
}

# Keyed by the rendered ``data-tip`` label (exact, case-sensitive). No entry for
# ``Official Source`` -> dropped (R3).
_WEEBCENTRAL_MAP: dict[str, tuple[str, Callable[[Any], str | None]]] = {
    "AniList": ("anilist", path_after(_AL)),
    "MangaUpdates": ("mangaUpdates", path_after(_MU)),
}

_MANGAFIRE_MAP: dict[str, tuple[str, Callable[[Any], str | None]]] = {
    "anilist_id": ("anilist", pass_through),
    "mal_id": ("myAnimeList", pass_through),
}

_SOURCE_MAPS: dict[str, dict[str, tuple[str, Callable[[Any], str | None]]]] = {
    "comix": _COMIX_MAP,
    "mangadex": _MANGADEX_MAP,
    "atsumaru": _ATSUMARU_MAP,
    "mangadot": _MANGADOT_MAP,
    "weebcentral": _WEEBCENTRAL_MAP,
    "mangafire": _MANGAFIRE_MAP,
}


def normalize(raw: dict[str, Any] | None, source_key: str) -> ExternalLinks | None:
    """Map a source's raw tracker dict onto canonical bare-ID ``ExternalLinks``.

    Walks ``source_key``'s frozen map: for each raw key present, applies the extractor,
    skips ``None``/empty results, and — as the single hard choke point — skips any value
    still containing ``"http"`` (R2, never emit a URL). Non-mapped raw keys are simply
    absent from the map and dropped (R3). Returns an :class:`ExternalLinks` if any
    canonical key survived, else ``None`` (an all-dropped / empty input -> ``None``,
    so the release's ``externalLinks`` is omitted, D-07).

    A falsy ``raw`` (``None`` from a cache miss, or an empty dict) short-circuits to
    ``None`` — so a source can pass ``ctx.external_links_raw.get(series_id)`` straight
    through with no ``or {}`` guard (IN-03).
    """
    if not raw:
        return None
    source_map = _SOURCE_MAPS.get(source_key)
    if source_map is None:
        return None
    kwargs: dict[str, str] = {}
    for raw_key, (canonical_key, extractor) in source_map.items():
        if raw_key not in raw:
            continue
        value = extractor(raw[raw_key])
        if not value:
            continue
        if re.search(  # hard R2 choke point — a URL must never reach the wire
            r"(?i)https?://", value
        ):
            continue
        kwargs[canonical_key] = value
    if not kwargs:
        return None
    return ExternalLinks(**kwargs)
