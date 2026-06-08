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

Issue #32 (2026-05-31): aligned the Comix call shape with production
(``ComixSource.fetch_manifest``) — ``wait_for=None`` so the drift
comparator and the live download path drive the extractor identically.
The previous mismatch was a red herring (the bug was inside the JS
extractor itself, not the Python-side wait), but symmetric call sites
keep "drift" focused on real upstream/extractor changes rather than
harness-vs-prod divergence.

Issue #166 (2026-06-07): the Comix image CDN periodically ROTATES the
host (``{sub}.wowpic{N}.store``) and the leading ``/iN/`` shard segment
of every page URL — observed ``/si`` → ``/i3`` (commit d6bd242) → ``/i4``
(this issue), THREE times, each forcing a manual fixture refresh. That
rotation is provably noise: production already treats the host + segment
as untrusted (the SSRF allowlist ``_COMIX_CDN_HOST_RE`` / ``_COMIX_CDN_PATH_RE``
in ``src/manga_gateway/sources/comix.py`` wildcards exactly those parts —
"the segment carries no trust"), so real downloads were never affected by
the rotation; only this strict drift comparator re-flagged it.

To stop each shard rotation re-flagging, both the captured and the fixture
URL sets are passed through ``_normalize_comix_cdn_url`` (below) before the
set-equality compare. It collapses ONLY the rotating host + ``/iN/`` segment
of a Comix-CDN-shaped URL to fixed placeholders, while keeping the signed
token and the per-page filename/ordinal STRICT — those are the real signal.
A genuine change (token, ordinal/filename, page count, path structure, or a
URL that is not Comix-CDN-shaped at all) still flags. This REFINES — does not
abandon — D-60's flag-only intent: drift on anything that actually matters
still fails the test for a human to review. The normalization is scoped to
the Comix CDN shape (anything else passes through verbatim) so it cannot
silently swallow drift on other fixtures/sources. Comix is the only source
with ``fixture_drift_paths`` today.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from ._helpers import live_client_for
from .conftest import REGISTERED_KEYS
from .profiles._base import LiveSmokeProfile

pytestmark = [
    pytest.mark.live,
    pytest.mark.parametrize("source_key", REGISTERED_KEYS),
]

# Issue #166 — Comix-CDN page-URL normalizer for the drift comparator.
#
# Mirrors the production SSRF allowlist shape (``_COMIX_CDN_HOST_RE`` +
# ``_COMIX_CDN_PATH_RE`` in src/manga_gateway/sources/comix.py): host
# ``{sub}.wowpic{N}.store`` and the leading ``/iN/`` short shard segment are
# the parts Comix rotates and that production treats as untrusted. The signed
# token (``[A-Za-z0-9_-]{16,}``) and the per-page ``NN.ext`` filename are the
# real signal and are CAPTURED, not wildcarded — they stay strict in the
# compare. Only URLs matching this exact shape are rewritten; everything else
# returns verbatim, so genuine drift on any other URL form still flags.
_COMIX_CDN_URL_RE = re.compile(
    r"^https://[a-z0-9-]+\.wowpic\d+\.store"  # rotating host (noise)
    r"/[a-z0-9]{2,4}"  # rotating /iN/ shard segment (noise)
    r"/(?P<token>[A-Za-z0-9_-]{16,})"  # signed token (STRICT signal)
    r"/(?P<page>\d+\.(?:webp|jpg|jpeg|png))$",  # page ordinal/filename (STRICT)
    re.IGNORECASE,
)


def _normalize_comix_cdn_url(url: object) -> object:
    """Collapse the rotating Comix CDN host + ``/iN/`` segment to placeholders.

    Issue #166: the Comix image CDN periodically rotates its host and leading
    ``/iN/`` shard segment (``/si`` → ``/i3`` → ``/i4`` so far), each rotation
    re-flagging this comparator even though production already treats those
    parts as untrusted noise. This normalizes ONLY those rotating parts of a
    Comix-CDN-shaped URL, keeping the signed token and per-page filename strict.

    Non-Comix-CDN inputs (any URL that does not match ``_COMIX_CDN_URL_RE``, or
    a non-string) are returned UNCHANGED, so the normalization cannot silently
    swallow genuine drift on other fixtures/sources.
    """
    if not isinstance(url, str):
        return url
    m = _COMIX_CDN_URL_RE.match(url)
    if m is None:
        return url
    return f"comix-cdn://{m.group('token')}/{m.group('page')}"


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
    * Issue #166: both sides are passed through ``_normalize_comix_cdn_url``
      first so a pure host/``/iN/``-shard CDN rotation is NOT flagged, while
      token/ordinal/page-count/path-structure drift STILL flags. Length
      parity is asserted on the RAW lists (pre-normalization) so duplicate-
      count drift still surfaces.
    """
    if not profile.fixture_drift_paths:
        pytest.skip(f"{source_key}: no fixture drift paths declared")

    # Comix-specific imports (only loaded once the test will actually run —
    # MangaDex skips above).
    from manga_gateway.sources.comix import (
        _CHAPTER_PAGES_EXTRACT_JS,
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

        # Issue #32: mirror ``ComixSource.fetch_manifest`` exactly — same
        # ``wait_for=None`` (the JS extractor's Step-1 scaffold wait does
        # the readiness check) and same 60s ceiling. Any deviation between
        # this call site and production reintroduces the harness-vs-prod
        # divergence the original issue chased.
        captured = await solver.fetch_via_browser(
            chapter_url,
            extract=_CHAPTER_PAGES_EXTRACT_JS,
            wait_for=None,
            timeout=60.0,
        )

        assert isinstance(captured, list) and captured, (
            f"{source_key}: solver.fetch_via_browser returned no URLs "
            f"for {chapter_url!r} — either Cloudflare escalated or the "
            f"chapter page structure changed"
        )

        # Issue #166: normalize the rotating Comix CDN host + /iN/ segment
        # before the set compare so a pure shard rotation isn't flagged as
        # drift. len(captured) (raw) is still used for the length-parity check.
        captured_set: set[object] = {_normalize_comix_cdn_url(u) for u in captured}

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

            # Same normalization on the fixture side — apples-to-apples
            # (Issue #166). A real token/ordinal/path-structure change still
            # survives normalization and flags below.
            expected_set: set[object] = {_normalize_comix_cdn_url(u) for u in expected}
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
                f"missing={sorted(map(str, missing))!r} "
                f"extra={sorted(map(str, extra))!r} "
                f"(captured {len(captured)} URLs, fixture has "
                f"{len(expected)})"
            )
