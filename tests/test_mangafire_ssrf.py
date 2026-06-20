"""Unit tests for ``mangafire._is_allowed_image_url`` — host-agnostic SSRF allowlist.

MangaFire page-image URLs ride a per-content-varying CDN host (``o48.mfcdn1.xyz``,
etc.), so the host is NEVER pinned. The allowlist enforces only the STABLE
invariants (D-10): ``https`` + public-host shape + ``/mf/`` path namespace + image
extension + no ``..`` traversal. The ``#scr_{offset}`` scramble fragment is stripped
BEFORE the path is matched, so a malicious fragment can never smuggle a path past the
regex. These tests pin the accept/reject matrix.
"""

from __future__ import annotations

import pytest

from manga_gateway.sources.mangafire import _is_allowed_image_url


@pytest.mark.parametrize(
    "url",
    [
        # Well-formed mfcdn{N} CDN-zone /mf/ image URL (the happy path); the host
        # prefix varies per content (nw8/l1n/k99/m3z/o48) but the mfcdnN.xyz family is
        # fixed (WR-02).
        "https://o48.mfcdn1.xyz/mf/abcdef0123/h/p.jpg",
        "https://nw8.mfcdn2.xyz/mf/deadbeef/h/01.webp",
        "https://l1n.mfcdn3.xyz/mf/deadbeef/h/01.jpg",
        "https://o48.mfcdn1.xyz/mf/abcdef0123/h/p.png",
        "https://o48.mfcdn1.xyz/mf/abcdef0123/h/p.jpeg",
        # Belt-and-suspenders: a benign #scr_{offset} fragment is stripped first,
        # so the underlying /mf/ image still ACCEPTS.
        "https://o48.mfcdn1.xyz/mf/abcdef0123/h/p.jpg#scr_2",
    ],
)
def test_allows_wellformed_mf_image_urls(url: str) -> None:
    assert _is_allowed_image_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # Public but non-mfcdn host: a syntactically valid hostname that is NOT a
        # MangaFire CDN zone is now rejected (WR-02 — was previously accepted).
        "https://another-cdn99.example.net/mf/deadbeef/h/01.webp",
        "https://evil.com/mf/x/p.jpg",
        "https://mangafire.to/mf/x/p.jpg",  # main site host is not an image CDN zone
        "https://o48.mfcdn1.xyz.evil.com/mf/x/p.jpg",  # suffix-smuggle look-alike
        # Internal / metadata hosts (SSRF target).
        "https://metadata.google.internal/mf/x/p.jpg",
        "https://foo.local/mf/x/p.jpg",
        "https://service.localhost/mf/x/p.jpg",
        # Bare dotless host (no public-TLD shape).
        "https://localhost/mf/x/p.jpg",
        # Non-https scheme.
        "http://o48.mfcdn1.xyz/mf/x/p.jpg",
        # Path outside the /mf/ namespace.
        "https://o48.mfcdn1.xyz/other/x/p.jpg",
        "https://o48.mfcdn1.xyz/secret.jpg",
        # Non-image extension.
        "https://o48.mfcdn1.xyz/mf/x/p.txt",
        "https://o48.mfcdn1.xyz/mf/x/p.php",
        # Literal traversal segment.
        "https://o48.mfcdn1.xyz/mf/../etc/passwd.jpg",
        # Fragment-smuggled path: the REAL path (/secret.txt) is rejected even though
        # the fragment looks like a valid /mf/ image — the fragment is stripped first.
        "https://o48.mfcdn1.xyz/secret.txt#scr_/mf/page.jpg",
        "https://o48.mfcdn1.xyz/mf/secret.php#.jpg",
    ],
)
def test_rejects_ssrf_and_offshape_urls(url: str) -> None:
    assert _is_allowed_image_url(url) is False
