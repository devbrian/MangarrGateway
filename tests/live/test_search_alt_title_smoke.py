"""Parametrized live alt-title ``POST /api/v1/search`` smoke (#139).

The offline tests prove the alt-title-aware candidate prune wiring
(``_search_series``/``_split_alt`` → ``prune_candidates(keys=...)``) over fixtures
and fakes. THIS adds the live dimension: a search for a source's real NATIVE/ALT
title must resolve — over the real source — to the correct series, proving
alt-title matching works end-to-end (Cloudflare cleared, real search ranking,
real ``altTitles`` payload).

Profile-driven: a source opts in by setting ``alt_title_query`` (and the
``alt_title_expected_substring`` that at least one returned release's ``title``
must contain) in ``tests/live/profiles/{source}.py``. Sources with no
confidently-known live native query leave both ``None`` and are SKIPPED.

Reuses ``live_client_for`` + ``check_response_conforms`` exactly like
``test_search_smoke.py`` (D-54 / CTRT-01 live).
"""

from __future__ import annotations

import pytest

from ._helpers import check_response_conforms, live_client_for
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile

pytestmark = [
    pytest.mark.live,
    pytest.mark.parametrize("source_key", REGISTERED_KEYS),
]


async def test_search_alt_title_resolves(
    source_key: str, profile: LiveSmokeProfile
) -> None:
    """POST /search for the source's native/alt title resolves to the right series.

    Skipped for sources whose profile leaves ``alt_title_query`` at ``None`` (no
    confidently-known live native title — see the mangadot/mangaball profile
    docstrings). For sources WITH an alt query, asserts:

    * 200 status.
    * ``releases`` array length ≥ 1 (the native query resolved to something).
    * NO ``warnings[]`` entry with ``sourceKey == source_key`` (the source
      fanned out cleanly for the native query).
    * At least one returned release's ``title`` contains
      ``profile.alt_title_expected_substring`` (case-insensitive) — proving the
      alt/native query resolved to the RIGHT series, not just any hit.
    * D-54: response body conforms to ``ReleaseListResponse``.
    """
    if profile.alt_title_query is None:
        pytest.skip(
            f"{source_key}: no confidently-known live native/alt-title query "
            "(profile.alt_title_query is None) — alt-title live smoke pending a "
            "verified native query for this source (#139)"
        )

    expected = profile.alt_title_expected_substring
    assert expected, (
        f"{source_key}: alt_title_query is set but alt_title_expected_substring "
        "is empty — both must be populated together"
    )

    async with live_client_for(profile) as client:
        resp = await client.post(
            "/api/v1/search",
            json={
                "type": "chapter",
                "query": profile.alt_title_query,
                "sources": [source_key],
            },
        )
        assert resp.status_code == 200, (
            f"{source_key}: alt-title POST /search failed: "
            f"{resp.status_code} {resp.text[:400]}"
        )

        payload = resp.json()
        releases = payload.get("releases") or []
        assert len(releases) >= 1, (
            f"{source_key}: alt-title search for {profile.alt_title_query!r} "
            f"returned no releases: {payload}"
        )

        warnings = payload.get("warnings") or []
        source_warnings = [w for w in warnings if w.get("sourceKey") == source_key]
        assert not source_warnings, (
            f"{source_key}: alt-title search emitted source-scoped warning(s): "
            f"{source_warnings}"
        )

        # The native/alt query resolved to the RIGHT series: at least one release
        # carries the expected English substring (case-insensitive).
        needle = expected.casefold()
        matching = [
            r for r in releases if needle in str(r.get("title") or "").casefold()
        ]
        assert matching, (
            f"{source_key}: alt-title search for {profile.alt_title_query!r} "
            f"returned {len(releases)} releases but none had a title containing "
            f"{expected!r} (case-insensitive): "
            f"{[r.get('title') for r in releases]}"
        )

        # D-54 / CTRT-01 live: response conforms to ReleaseListResponse.
        check_response_conforms("/search", "POST", resp)
