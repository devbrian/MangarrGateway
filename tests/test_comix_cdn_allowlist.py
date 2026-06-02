"""Regression: Comix CDN SSRF allowlist tolerates the rotating path segment.

Debug session ``comix-malformed-manifest`` (2026-06-02): Comix rotated the
page-image CDN path segment from ``/si/`` to ``/i3/`` (live src observed:
``https://jloo.wowpic5.store/i3/bEqPbYfoMT0GmyXlE2KfoBZAzoUdauw/01.webp``). The
extractor regex + ``_is_allowed_image_url`` SSRF allowlist were pinned to the
``/si/`` literal → zero live page URLs matched → empty manifest →
``SourceError("source_unavailable", "malformed chapter manifest")``.

The fix WILDCARDS the leading path segment to a short alphanumeric run
(``[a-z0-9]{2,4}``) while keeping the real security anchors: the
``*.wowpic{N}.store`` HOST pin, the token/filename shape, HTTPS-only, anchored
path (no traversal), and image-only extensions. These tests pin that contract so
a future tightening that re-breaks ``/i3/`` (or loosens a guard) fails loudly.
"""

from __future__ import annotations

import pytest

from manga_gateway.sources.comix import _is_allowed_image_url

_TOKEN = "bEqPbYfoMT0GmyXlE2KfoBZAzoUdauw"  # 31 chars, live-observed

# (url, expected, reason)
_ACCEPT_CASES: list[tuple[str, str]] = [
    (
        f"https://jloo.wowpic5.store/i3/{_TOKEN}/01.webp",
        "live 2026-06-02 /i3/ segment — THE regression",
    ),
    (
        f"https://jdpw.wowpic1.store/si/{_TOKEN}/01.webp",
        "historical /si/ segment still accepted (no fixture churn)",
    ),
    (
        f"https://jloo.wowpic5.store/i3/{_TOKEN}/12.jpg",
        "jpg extension on the rotated segment",
    ),
    (
        f"https://jloo.wowpic9.store/zz/{_TOKEN}/07.png",
        "future-rotation-proof: an unseen 2-char segment is accepted",
    ),
]

_REJECT_CASES: list[tuple[str, str]] = [
    (
        f"https://evil.example.com/i3/{_TOKEN}/01.webp",
        "off-host (not *.wowpic{N}.store) — SSRF anchor holds",
    ),
    (
        f"https://jloo.notwowpic.store/i3/{_TOKEN}/01.webp",
        "host must be the wowpic shard, not a lookalike",
    ),
    (
        "https://jloo.wowpic5.store/i3/../../etc/passwd",
        "path traversal rejected by the anchored token/filename shape",
    ),
    (
        f"https://jloo.wowpic5.store/i3/{_TOKEN}/01.svg",
        "non-image extension rejected (extension guard intact)",
    ),
    (
        f"http://jloo.wowpic5.store/i3/{_TOKEN}/01.webp",
        "non-HTTPS scheme rejected",
    ),
    (
        "https://jloo.wowpic5.store/i3/short/01.webp",
        "sub-16-char token rejected (token shape intact)",
    ),
    (
        f"https://jloo.wowpic5.store/{_TOKEN}/01.webp",
        "missing the leading segment rejected",
    ),
    (
        f"https://jloo.wowpic5.store/toolongsegment/{_TOKEN}/01.webp",
        "an over-long leading segment ([a-z0-9]{2,4} bound) rejected",
    ),
]


@pytest.mark.parametrize(
    "url",
    [c[0] for c in _ACCEPT_CASES],
    ids=[c[1] for c in _ACCEPT_CASES],
)
def test_allowlist_accepts(url: str) -> None:
    assert _is_allowed_image_url(url) is True


@pytest.mark.parametrize(
    "url",
    [c[0] for c in _REJECT_CASES],
    ids=[c[1] for c in _REJECT_CASES],
)
def test_allowlist_rejects(url: str) -> None:
    assert _is_allowed_image_url(url) is False
