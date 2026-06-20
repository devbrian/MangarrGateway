"""Parametrized live external-tracker-links smoke (Phase 13, D-08 / R7).

Runs once per registered source. For every source whose
``LiveSmokeProfile.expected_external_links`` is NON-EMPTY, this asserts that at
least one release returned by a live ``POST /api/v1/search`` for the profile's
``default_query`` carries an ``externalLinks`` object that is a SUPERSET of the
profile's pinned ``{canonical_key: exact_id}`` map — i.e. every pinned key is
present and equal to the pinned bare ID/slug.

Sources with an EMPTY ``expected_external_links`` (the default) are SKIPPED, not
failed: a source that exposes no tracker links (or whose links are blocked /
not yet pinned) must not turn the nightly red. This is the D-08 freezing gate
(R7): proving end-to-end, against the real sites, that every capable source
returns the correct CANONICAL tracker IDs. D-08 deliberately accepts the
brittleness of exact-ID pins as an early-warning signal — a site correcting or
drifting an ID surfaces here and is repinned.

The pinned values are bare IDs/slugs only (never URLs); requiring an exact
match therefore also exercises the production no-``http`` invariant (T-13-03):
a real site emitting a full URL or a wrong ID fails the superset check.
"""

from __future__ import annotations

import pytest

from ._helpers import live_client_for
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile

pytestmark = [
    pytest.mark.live,
    pytest.mark.parametrize("source_key", REGISTERED_KEYS),
]


async def test_external_links_match_pinned_ids(
    source_key: str, profile: LiveSmokeProfile
) -> None:
    """≥1 live release's ``externalLinks`` ⊇ the profile's pinned tracker IDs (D-08).

    SKIPS the source when ``profile.expected_external_links`` is empty (the
    source exposes no/blocked links, or is not yet pinned — e.g. comix before
    its first green nightly, or mangaball/kagane). Otherwise:

    * 200 status for ``POST /search`` of ``profile.default_query``.
    * At least one returned release carries an ``externalLinks`` object whose
      ``{key: value}`` pairs are a SUPERSET of the pinned map (every pinned key
      present and byte-equal to the pinned bare ID/slug).
    """
    expected = profile.expected_external_links
    if not expected:
        pytest.skip(
            f"{source_key}: no expected_external_links pinned — "
            "source exposes no/blocked tracker links or is not yet frozen (D-08)"
        )

    # Decoupled external-links canary (USER DECISION 2026-06-19): some sources'
    # ``default_query`` titles (tuned for the DOWNLOAD smoke) legitimately expose
    # no tracker links live, so the external-links assertion uses a dedicated
    # ``expected_external_links_query`` when present. This is the ONLY live test
    # that diverges from ``default_query``.
    links_query = profile.expected_external_links_query or profile.default_query

    async with live_client_for(profile) as client:
        resp = await client.post(
            "/api/v1/search",
            json={
                "type": "chapter",
                "query": links_query,
                "sources": [source_key],
            },
        )
        assert resp.status_code == 200, (
            f"{source_key}: POST /search failed: {resp.status_code} {resp.text[:400]}"
        )

        payload = resp.json()
        releases = payload.get("releases") or []
        assert releases, (
            f"{source_key}: search for {links_query!r} returned no "
            f"releases — cannot verify externalLinks: {payload}"
        )

        # Collect every release's externalLinks (None → {}) for the assertion
        # message and the superset scan.
        observed = [(r.get("externalLinks") or {}) for r in releases]
        matched = any(
            all(links.get(key) == value for key, value in expected.items())
            for links in observed
        )
        assert matched, (
            f"{source_key}: no release's externalLinks ⊇ pinned "
            f"{expected!r} for query {links_query!r}. "
            f"Observed externalLinks across {len(releases)} releases: {observed}"
        )
