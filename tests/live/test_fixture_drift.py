"""Parametrized live fixture-drift smoke (D-44 / D-60).

Inverts the D-44 recorder logic from ``tests/live/test_comix_live.py``
(deleted by this plan) into a comparator: capture the live payload via
the SAME ``live_client_for(profile)`` harness ``test_download_smoke.py``
uses (W-03 symmetry — single lifespan path, single solver instance per
test, single autouse warm interaction), then structurally compare the
captured page-URL list against the committed fixture.

D-60: drift is a FLAG-ONLY FAIL — the human refreshes the fixture
manually via a PR. Auto-PR / auto-commit is rejected.

Pitfall 6 mitigation: use SET-EQUALITY (and length parity) on the page
URL list, NOT byte-equality. Transient blips at the CDN (one URL
shuffled into a different ordinal, an extra short-lived ad URL hitting
the extractor for one frame) are NOT what D-60 is designed to flag —
the signal that matters is "the URL SET drifted", not "the JSON file
hashed differently".

Sources whose profile declares ``fixture_drift_paths = []`` (MangaDex
today) get a clean ``pytest.skip``. This keeps the parametrize-over-
REGISTERED_KEYS shape symmetric with the other smoke modules.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ._helpers import live_client_for
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile

pytestmark = [
    pytest.mark.live,
    pytest.mark.parametrize("source_key", REGISTERED_KEYS),
]

# Imported lazily inside the test so MangaDex (which skips before touching
# any Comix-specific symbol) does not require the Comix source module.
# Mirrors the lazy access pattern test_comix_live.py used for the
# extractor + wait_for selectors.


async def test_fixture_drift(
    source_key: str, profile: LiveSmokeProfile, tmp_path: Path
) -> None:
    """Capture the live page-URL list and structurally compare to the fixture.

    * No-op skip when ``profile.fixture_drift_paths`` is empty (MangaDex).
    * For Comix: drive ``solver.fetch_via_browser`` through the SAME
      ``live_client_for`` harness ``test_download_smoke.py`` uses so the
      autouse ``_restore_real_cloudflare_warm`` interacts with ONE solver
      per test (W-03 lock).
    * Compare SET equality on the captured URL list against each declared
      fixture (Pitfall 6 — transient ordinal jitter is NOT drift).
    """
    if not profile.fixture_drift_paths:
        pytest.skip(f"{source_key}: no fixture drift paths declared")

    # Comix-specific imports (only loaded once the test will actually run —
    # MangaDex skips above).
    from manga_gateway.sources.comix import (
        _CHAPTER_PAGES_EXTRACT_JS,
        _CHAPTER_PAGES_WAIT_FOR,
        ComixSource,
    )

    chapter_url = (
        f"{ComixSource.base_url}/title/mr3m0-the-forgotten-field/9001596-chapter-20"
    )

    async with live_client_for(profile, tmp_path=tmp_path) as client:
        # W-03 LOCK: access the running solver via the ASGI transport's
        # app.state, NOT a fresh CloudflareSolver(...). This keeps the
        # autouse _restore_real_cloudflare_warm interacting with ONE
        # solver instance — a direct instantiation would bypass app
        # state and double-launch Patchright. The httpx 0.28 ASGITransport
        # exposes its app via the (private) ``_transport.app`` attribute;
        # there is no public httpx accessor in this version.
        solver = client._transport.app.state.solver

        captured = await solver.fetch_via_browser(
            chapter_url,
            extract=_CHAPTER_PAGES_EXTRACT_JS,
            wait_for=_CHAPTER_PAGES_WAIT_FOR,
            timeout=45.0,
        )

        assert isinstance(captured, list) and captured, (
            f"{source_key}: solver.fetch_via_browser returned no URLs "
            f"for {chapter_url!r} — either Cloudflare escalated or the "
            f"chapter page structure changed"
        )

        captured_set: set[Any] = set(captured)

        for fixture_path in profile.fixture_drift_paths:
            assert fixture_path.exists(), (
                f"{source_key}: declared fixture path does not exist: {fixture_path}"
            )
            raw = await asyncio.to_thread(fixture_path.read_text, encoding="utf-8")
            expected = json.loads(raw)

            if not isinstance(expected, list):
                # search_the_forgotten_field.json carries a different
                # shape — skip the URL-set compare for non-list fixtures;
                # the existence check above is the meaningful drift
                # signal for those (we know they exist and parse).
                continue

            expected_set: set[Any] = set(expected)
            missing = expected_set - captured_set
            extra = captured_set - expected_set
            # D-60 / Pitfall 6: set-equality alone misses duplicate-count
            # drift. The docstring above promises "set-equality AND length
            # parity" — assert length parity first so a multiplicity change
            # (e.g. fixture has [A, B, B], capture has [A, A, B]) surfaces
            # as a clear count diff, not a silently-equal set
            # (CodeRabbit PR #33 review).
            assert len(captured) == len(expected), (
                f"D-60 drift on {fixture_path.name} ({source_key}): "
                f"URL multiplicity changed — captured {len(captured)} URLs, "
                f"fixture has {len(expected)}"
            )
            assert captured_set == expected_set, (
                f"D-60 drift on {fixture_path.name} ({source_key}): "
                f"missing={sorted(missing)!r} extra={sorted(extra)!r} "
                f"(captured {len(captured)} URLs, fixture has "
                f"{len(expected)})"
            )
