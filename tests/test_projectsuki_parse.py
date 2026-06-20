"""Unit tests for ProjectSuki's module-level parse helpers + SSRF allowlist.

Covers the source-agnostic plumbing that needs no ``SourceContext``:

* :func:`_is_allowed_image_url` — the T-07 SSRF allowlist on the extension-less
  ``/images/gallery/{bookId}/{hexHash}/{pageNumber}`` page path (accept incl. the
  ``0010`` padding; reject off-host / traversal / wrong namespace / non-https).
* :func:`_parse_search_results` — ``/book/{id}`` anchor extraction (dedupe, title
  from the title anchor, not the empty thumbnail anchor).
* :func:`_parse_book_chapters` — chapter rows + the ``#chapter-select`` dropdown
  fallback (chapterId from the ``/read`` href, number, subtitle, group, language).
* :func:`_date_to_iso` — RFC3339 normalization + fallback.

No network, no ctx — pure functions.
"""

from __future__ import annotations

from manga_gateway.sources.projectsuki import (
    _date_to_iso,
    _is_allowed_image_url,
    _parse_book_chapters,
    _parse_search_results,
)

# ─────────────────────────── _is_allowed_image_url (SSRF) ────────────────────────

_BASE_IMG = "https://projectsuki.com/images/gallery/180716/a1b2c3d4ef/001"


def test_is_allowed_image_url_accepts_page_one() -> None:
    assert _is_allowed_image_url(_BASE_IMG)
    # A ``?{ts}`` query is dropped by urlparse before the path match.
    assert _is_allowed_image_url(f"{_BASE_IMG}?1718900000")


def test_is_allowed_image_url_accepts_extension_less_wide_padding() -> None:
    """The page filename is ``00{n}`` with NO extension — 001, 0010, 0024 all pass."""
    host = "https://projectsuki.com/images/gallery/180716/a1b2c3d4ef"
    assert _is_allowed_image_url(f"{host}/0010?123")
    assert _is_allowed_image_url(f"{host}/0024")
    assert _is_allowed_image_url(
        "https://www.projectsuki.com/images/gallery/9/ABCDEF/7"
    )


def test_is_allowed_image_url_rejects_off_host() -> None:
    assert not _is_allowed_image_url(
        "https://evil.com/images/gallery/180716/a1b2c3d4ef/001"
    )


def test_is_allowed_image_url_rejects_non_https() -> None:
    assert not _is_allowed_image_url(
        "http://projectsuki.com/images/gallery/180716/a1b2c3d4ef/001"
    )


def test_is_allowed_image_url_rejects_wrong_namespace() -> None:
    # The thumbnail path (``/images/gallery/{id}/thumb``) is NOT a numeric page path.
    assert not _is_allowed_image_url(
        "https://projectsuki.com/images/gallery/180716/thumb"
    )
    # A same-origin path outside /images/gallery/ must fail.
    assert not _is_allowed_image_url("https://projectsuki.com/api/secret/001")
    # A non-hex hash segment must fail.
    assert not _is_allowed_image_url(
        "https://projectsuki.com/images/gallery/180716/not-hex-zz/001"
    )


def test_is_allowed_image_url_rejects_traversal() -> None:
    assert not _is_allowed_image_url(
        "https://projectsuki.com/images/gallery/../../etc/001"
    )
    # A traversal that normalizes out of the namespace is also rejected.
    assert not _is_allowed_image_url(
        "https://projectsuki.com/images/gallery/a/b/../../../../x/001"
    )


# ─────────────────────────── _parse_search_results ──────────────────────────────


_SEARCH_HTML = """
<html><body>
  <div class="results">
    <a class="inherit-color p-1" href="/book/180716">
      <img src="/images/gallery/180716/thumb?x=1">
    </a>
    <h4><a class="inherit-color" href="/book/180716">Solo Leveling</a></h4>
    <a class="inherit-color p-1" href="/book/42">
      <img src="/images/gallery/42/thumb">
    </a>
    <h4><a class="inherit-color" href="/book/42">Omniscient Reader</a></h4>
  </div>
</body></html>
"""


def test_parse_search_results_extracts_deduped_pairs() -> None:
    books = _parse_search_results(_SEARCH_HTML)
    assert books == [
        {"book_id": "180716", "title": "Solo Leveling"},
        {"book_id": "42", "title": "Omniscient Reader"},
    ]


def test_parse_search_results_empty_html() -> None:
    assert _parse_search_results("") == []
    assert _parse_search_results("<html><body>no books</body></html>") == []


# ─────────────────────────── _parse_book_chapters ───────────────────────────────


_BOOK_HTML = """
<html><body>
  <table>
    <tr class="row mx-0">
      <td class="col-5">
        <a href="/read/180716/ch-2/1" class="inherit-color d-flex" title="The Hunt">
          Chapter 2<span class="d-none d-lg-block text-truncate"> - The Hunt</span>
        </a>
      </td>
      <td><a href="/group/9">Asura Scans</a></td>
      <td>English</td>
      <td>2026-06-19</td>
    </tr>
    <tr class="row mx-0">
      <td class="col-5">
        <a href="/read/180716/ch-1p5/1" class="inherit-color d-flex" title="Side Story">
          Chapter 1.5<span> - Side Story</span>
        </a>
      </td>
      <td><a href="/group/9">Asura Scans</a></td>
      <td>English</td>
      <td>2026-06-18</td>
    </tr>
  </table>
</body></html>
"""


def test_parse_book_chapters_rows() -> None:
    chapters = _parse_book_chapters(_BOOK_HTML, "180716")
    by_id = {c["chapter_id"]: c for c in chapters}
    assert set(by_id) == {"ch-2", "ch-1p5"}
    ch2 = by_id["ch-2"]
    assert ch2["number"] == "2"
    assert ch2["subtitle"] == "The Hunt"
    assert ch2["group"] == "Asura Scans"
    assert ch2["language"] == "English"
    assert ch2["date_raw"] == "2026-06-19"
    assert by_id["ch-1p5"]["number"] == "1.5"  # decimal preserved


def test_parse_book_chapters_dropdown_fallback() -> None:
    """When the page has no rows, the #chapter-select dropdown back-fills chapters."""
    html = """
    <html><body>
      <select id="chapter-select">
        <option value="ch-2">2 - The Hunt - Asura Scans</option>
        <option value="ch-1">1 - Awakening - Asura Scans</option>
      </select>
    </body></html>
    """
    chapters = _parse_book_chapters(html, "180716")
    by_id = {c["chapter_id"]: c for c in chapters}
    assert set(by_id) == {"ch-2", "ch-1"}
    assert by_id["ch-2"]["number"] == "2"
    assert by_id["ch-2"]["subtitle"] == "The Hunt"
    assert by_id["ch-2"]["group"] == "Asura Scans"


def test_parse_book_chapters_empty() -> None:
    assert _parse_book_chapters("", "180716") == []
    assert _parse_book_chapters("<html><body>nope</body></html>", "180716") == []


# ─────────────────────────────── _date_to_iso ───────────────────────────────────


def test_date_to_iso_iso_and_common_formats() -> None:
    assert _date_to_iso("2026-06-19") == "2026-06-19T00:00:00+00:00"
    assert _date_to_iso("Jun 19, 2026") == "2026-06-19T00:00:00+00:00"
    assert _date_to_iso("19 June 2026") == "2026-06-19T00:00:00+00:00"


def test_date_to_iso_falls_back_to_none() -> None:
    assert _date_to_iso(None) is None
    assert _date_to_iso("") is None
    assert _date_to_iso("2 days ago") is None
    assert _date_to_iso(12345) is None
