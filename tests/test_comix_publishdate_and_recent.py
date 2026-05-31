"""Unit tests for Comix issues #30 (publishDate) and #31 (supportsRecent).

Two narrow regressions surfaced by the Phase 5 first live-smoke run
(2026-05-30) and fixed in branch ``fix/comix-publishdate-recent``:

* **#30** — ``Release.publishDate`` came out as the empty string because the
  Comix chapter-list DOM does NOT expose a machine-readable absolute
  timestamp. The only public per-row date is the rendered relative string
  ("14h ago", "3mos ago"). The fix captures that string in the JS extractor
  and approximates it Python-side via :func:`_parse_relative_time`. These
  tests pin the parser against the live-recon-observed string forms and
  verify ``_to_release`` populates ``publish_date`` from the relative form
  when no absolute timestamp is present.
* **#31** — ``/recent`` silently returned an empty array because Comix has
  no public all-recent-chapters feed. The fix declares
  ``ComixSource.supports_recent = False`` so ``/caps`` advertises the gap
  honestly. These tests pin the class attribute and the ``/caps`` payload.

No network, no browser — all assertions are deterministic against the
declarative class and the pure parser. The unit-test ``client`` fixture
(``tests/conftest.py``) binds ``base_url`` to ``http://testserver/api/v1``
so route paths in this module are bare (``/caps``, ``/recent``), NOT
``/api/v1/caps``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from manga_gateway.handles.store import HandleStore
from manga_gateway.sources.comix import ComixSource, _parse_relative_time

if TYPE_CHECKING:
    from manga_gateway.framework.context import SourceContext


# ─────────────────────────── Issue #30: publishDate parser ───────────────────


@pytest.mark.parametrize(
    "raw, seconds_ago",
    [
        # Forms observed in 2026-05-30 _recon_out/network.jsonl
        ("14h ago", 14 * 3600),
        ("1h ago", 3600),
        ("3h ago", 3 * 3600),
        ("1d ago", 86400),
        ("2d ago", 2 * 86400),
        ("3d ago", 3 * 86400),
        ("4d ago", 4 * 86400),
        ("5d ago", 5 * 86400),
        ("6d ago", 6 * 86400),
        ("1w ago", 7 * 86400),
        ("2w ago", 2 * 7 * 86400),
        ("3w ago", 3 * 7 * 86400),
        ("1mo ago", 30 * 86400),
        ("2mos ago", 2 * 30 * 86400),
        ("3mos ago", 3 * 30 * 86400),
        ("5mos ago", 5 * 30 * 86400),
        ("7mos ago", 7 * 30 * 86400),
        # Forward-compat forms (seconds / minutes / years).
        ("30s ago", 30),
        ("45 sec ago", 45),
        ("5 minutes ago", 5 * 60),
        ("2y ago", 2 * 365 * 86400),
        ("1 year ago", 365 * 86400),
    ],
)
def test_parse_relative_time_returns_iso_utc_offset(raw: str, seconds_ago: int) -> None:
    """Every recon-observed form parses to ``now - seconds_ago`` as ISO UTC."""
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    result = _parse_relative_time(raw, now=now)
    assert result is not None, f"expected an ISO string for {raw!r}, got None"
    # Trailing ``Z`` is REL-01 / RFC 3339 conformant and matches MangaDex's
    # emission so cross-source ``/recent`` sorts compare consistently.
    assert result.endswith("Z"), f"expected trailing Z on {result!r}"
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    actual_seconds = (now - parsed).total_seconds()
    assert actual_seconds == seconds_ago, (
        f"{raw!r}: expected {seconds_ago}s offset, got {actual_seconds}s "
        f"(parsed={parsed!r})"
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "just now",  # not "N <unit> ago" — gracefully decline
        "yesterday",
        "ages ago",
        "abc days ago",  # quantity not numeric
        "12 fortnight ago",  # unrecognised unit
        "3 days",  # missing ``ago``
    ],
)
def test_parse_relative_time_returns_none_for_unparseable(raw: str | None) -> None:
    """Unrecognised inputs yield ``None`` so callers can fall back cleanly."""
    assert _parse_relative_time(raw) is None


def test_parse_relative_time_defaults_to_current_utc() -> None:
    """When ``now`` is omitted the parser anchors on the current UTC time."""
    before = datetime.now(UTC)
    result = _parse_relative_time("1h ago")
    after = datetime.now(UTC)
    assert result is not None
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    # 1h-ago must fall inside the wall-clock window of `before - 1h` ..
    # `after - 1h` — i.e. the parser anchored on something in [before, after].
    assert (before - parsed).total_seconds() >= 3600 - 1
    assert (after - parsed).total_seconds() <= 3600 + 1


# ─────────────────────── Issue #30: _to_release uses the parser ──────────────


class _FakeCtx:
    """Minimal ``SourceContext`` stand-in for the synchronous ``_to_release``.

    ``_to_release`` only touches ``ctx.handle_store``, so a real
    ``SourceContext`` is overkill. The fake exposes one ``HandleStore`` and
    is otherwise untouched.
    """

    def __init__(self) -> None:
        self.handle_store = HandleStore()


def _ctx() -> SourceContext:
    return _FakeCtx()  # type: ignore[return-value]


def test_to_release_populates_publish_date_from_relative_when_absolute_absent() -> None:
    """A chapter dict with only ``publishedAtRelative`` produces a non-empty
    ``publishDate`` (REL-01) parsed via :func:`_parse_relative_time`."""
    source = ComixSource()
    release = source._to_release(
        "mr3m0",
        "the-forgotten-field",
        {
            "id": "9938735",
            "chapter": "23",
            "lang": "en",
            "groups": [{"name": "Thunderscans"}],
            "publishedAtRelative": "2d ago",
            "seriesTitle": "The Forgotten Field",
        },
        _ctx(),
    )
    assert release is not None
    # REL-01: non-empty string, parseable as ISO 8601 date-time.
    assert release.publish_date
    parsed = datetime.fromisoformat(release.publish_date.replace("Z", "+00:00"))
    delta_seconds = (datetime.now(UTC) - parsed).total_seconds()
    # "2d ago" => ~172800s ago, with wide tolerance for clock drift on slow CI.
    assert 2 * 86400 - 60 <= delta_seconds <= 2 * 86400 + 60


def test_to_release_prefers_absolute_publishedAt_over_relative() -> None:
    """An explicit ``publishedAt`` ISO string wins; the relative form is ignored."""
    source = ComixSource()
    release = source._to_release(
        "mr3m0",
        "the-forgotten-field",
        {
            "id": "9938735",
            "chapter": "23",
            "lang": "en",
            "publishedAt": "2026-05-15T10:30:00Z",
            "publishedAtRelative": "2d ago",  # would override; absolute MUST win
        },
        _ctx(),
    )
    assert release is not None
    assert release.publish_date == "2026-05-15T10:30:00Z"


def test_to_release_publish_date_empty_when_no_date_signal() -> None:
    """No absolute and no relative → empty string. REL-01 violation is then
    surfaced loudly by the contract layer (schemathesis), not silently masked
    by the source layer fabricating a date."""
    source = ComixSource()
    release = source._to_release(
        "mr3m0",
        "the-forgotten-field",
        {"id": "9938735", "chapter": "23", "lang": "en"},
        _ctx(),
    )
    assert release is not None
    assert release.publish_date == ""


def test_to_release_relative_garbage_falls_back_to_empty() -> None:
    """``publishedAtRelative`` that doesn't parse must NOT crash; the field
    stays empty so the contract layer surfaces it (no silent fabrication)."""
    source = ComixSource()
    release = source._to_release(
        "mr3m0",
        "the-forgotten-field",
        {
            "id": "9938735",
            "chapter": "23",
            "lang": "en",
            "publishedAtRelative": "yesterday",  # unparseable
        },
        _ctx(),
    )
    assert release is not None
    assert release.publish_date == ""


def test_to_release_carries_required_REL01_fields_with_relative_date() -> None:
    """End-to-end shape check: every REL-01 required field is populated when
    only the relative-date signal is available (the live-smoke scenario)."""
    source = ComixSource()
    release = source._to_release(
        "mr3m0",
        "the-forgotten-field",
        {
            "id": "9938735",
            "chapter": "23",
            "lang": "en",
            "groups": [{"name": "Thunderscans"}],
            "publishedAtRelative": "3mos ago",
            "seriesTitle": "The Forgotten Field",
        },
        _ctx(),
    )
    assert release is not None
    # REL-01: guid, title, sourceKey, downloadHandle, publishDate all populated.
    assert release.guid
    assert release.title
    assert release.source_key == "comix"
    assert release.download_handle
    assert release.publish_date
    assert release.chapter_number == Decimal("23")
    assert release.scanlation_group == "Thunderscans"


# ─────────────────────── Issue #31: supports_recent = False ──────────────────


def test_comix_declares_supports_recent_false() -> None:
    """ComixSource MUST advertise ``supports_recent = False`` so ``/caps``
    callers can branch on the field instead of guessing from an empty array."""
    assert ComixSource.supports_recent is False


@pytest.mark.asyncio
async def test_caps_route_reports_comix_supports_recent_false(
    client: httpx.AsyncClient,
) -> None:
    """``GET /caps`` surfaces ``supportsRecent: false`` for Comix (Issue #31).

    The contract requires this field (openapi.yaml: SourceCap.required
    includes ``supportsRecent``), so the field is always present; the
    value is what changed from ``true`` (the framework default) to ``false``.
    """
    resp = await client.get("/caps")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    comix = next((s for s in sources if s["key"] == "comix"), None)
    assert comix is not None, "comix not advertised in /caps"
    assert comix["supportsRecent"] is False, (
        f"comix must advertise supportsRecent=false (Issue #31); got "
        f"{comix['supportsRecent']!r}. Full cap: {comix}"
    )


@pytest.mark.asyncio
async def test_recent_route_returns_empty_array_for_comix(
    client: httpx.AsyncClient,
) -> None:
    """``GET /recent?sources=comix`` returns an empty releases array honestly
    (Issue #31). The empty array + ``supportsRecent: false`` in /caps is the
    spec-conformant outcome; the per-source isolation guarantees a 200."""
    resp = await client.get("/recent", params={"sources": "comix", "limit": "5"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("releases") == []
    # No warning either — an unsupported recent feed is an advertised gap,
    # not a per-call failure. Clients should branch on /caps.supportsRecent.
    assert payload.get("warnings") == []
