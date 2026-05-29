"""CBZ packaging primitives (03-02 Task 2).

Covers the testable, blocking-by-design units the Plan 03 engine composes via
``asyncio.to_thread``:

* ``is_valid_image`` Pillow validate-only; accepts real images, rejects
  HTML/truncated blobs (Pitfall 6 / T-03-03).
* ``write_cbz`` ZIP_STORED, zero-padded reading-order entries, EXACT original
  bytes (no recompression, PKG-04), NO ComicInfo.xml, staging to atomic publish (D-26).
* ``compute_output_path`` ``{outputRoot}/manga-{mangaId}/{title}.cbz`` with the
  title AND the mangaId slug run through ``sanitize_filename`` and a
  ``manga-unknown`` fallback (D-24/D-25 / T-03-04 traversal guard).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from manga_gateway.jobs import package


def _png_bytes(color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buf, format="JPEG")
    return buf.getvalue()


# ─────────────────────────────── is_valid_image ─────────────────────────────────


def test_is_valid_image_accepts_real_png_and_jpeg() -> None:
    assert package.is_valid_image(_png_bytes()) is True
    assert package.is_valid_image(_jpeg_bytes()) is True


def test_is_valid_image_rejects_html_error_body() -> None:
    # Pitfall 6: a 200 HTML error page must NOT pass as an image.
    html = b"<!DOCTYPE html><html><body>403 Forbidden</body></html>"
    assert package.is_valid_image(html) is False


def test_is_valid_image_rejects_truncated_blob() -> None:
    truncated = _png_bytes()[:20]  # header only, body cut off
    assert package.is_valid_image(truncated) is False


def test_is_valid_image_rejects_empty() -> None:
    assert package.is_valid_image(b"") is False


# ──────────────────────────────────── write_cbz ─────────────────────────────────


def test_write_cbz_is_zip_stored_zero_padded_reading_order(tmp_path: Path) -> None:
    pages = [(f"page-{i}.jpg", f"img{i}".encode()) for i in range(1, 11)]  # 10 pages
    final = tmp_path / "out.cbz"

    package.write_cbz(pages, final)

    with zipfile.ZipFile(final) as zf:
        names = zf.namelist()
        # Pitfall 5: 10 pages → width 2 → 01..10, lexically sorted == reading order.
        assert names == [f"{i:02d}.jpg" for i in range(1, 11)]
        assert names == sorted(names)
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_STORED


def test_write_cbz_entries_have_exact_original_bytes(tmp_path: Path) -> None:
    # PKG-04: originals written unchanged (no recompression).
    pages = [("a.png", _png_bytes("red")), ("b.png", _png_bytes("green"))]
    final = tmp_path / "out.cbz"

    package.write_cbz(pages, final)

    with zipfile.ZipFile(final) as zf:
        # 2 pages → width max(2, 1) == 2 → zero-padded 01/02.
        assert zf.read("01.png") == pages[0][1]
        assert zf.read("02.png") == pages[1][1]


def test_write_cbz_preserves_original_extension(tmp_path: Path) -> None:
    pages = [("first.png", b"a"), ("second.jpeg", b"b"), ("third", b"c")]
    final = tmp_path / "out.cbz"

    package.write_cbz(pages, final)

    with zipfile.ZipFile(final) as zf:
        names = zf.namelist()
    assert names[0].endswith(".png")
    assert names[1].endswith(".jpeg")
    assert names[2].endswith(".jpg")  # no extension → default .jpg


def test_write_cbz_contains_no_comicinfo_or_xml(tmp_path: Path) -> None:
    # PKG-04: page images ONLY — no metadata file.
    pages = [("a.jpg", b"a"), ("b.jpg", b"b")]
    final = tmp_path / "out.cbz"

    package.write_cbz(pages, final)

    with zipfile.ZipFile(final) as zf:
        names = [n.lower() for n in zf.namelist()]
    assert not any(n.endswith(".xml") for n in names)
    assert not any("comicinfo" in n for n in names)


def test_write_cbz_atomic_on_mid_write_failure(tmp_path: Path) -> None:
    # Force a failure DURING the zip write (a non-bytes payload) and assert no final
    # file and no leftover staging temp (D-26).
    final = tmp_path / "out.cbz"
    pages = [("a.jpg", b"a"), ("b.jpg", object())]  # type: ignore[list-item]

    # object() is not bytes-like → zipfile.writestr raises mid-write (TypeError),
    # exercising the BaseException cleanup branch (D-26) without a blind assert.
    with pytest.raises(TypeError):
        package.write_cbz(pages, final)  # type: ignore[arg-type]

    assert not final.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name != final.name]
    assert leftovers == []


# ─────────────────────────────── compute_output_path ────────────────────────────


def test_compute_output_path_layout_and_sanitization() -> None:
    path = package.compute_output_path(
        "/data/manga", 178, "Solo Leveling - Chapter 1 (en) [Team Lumikha]"
    )
    assert path == Path(
        "/data/manga/manga-178/Solo Leveling - Chapter 1 (en) [Team Lumikha].cbz"
    )


def test_compute_output_path_sanitizes_traversal_in_title() -> None:
    # T-03-04 / D-25: a malicious title cannot escape its parent directory.
    path = package.compute_output_path("/data/manga", 5, "../../etc/passwd")
    parts = path.parts
    assert ".." not in parts
    assert path.parent == Path("/data/manga/manga-5")
    assert path.name.endswith(".cbz")
    assert "/" not in path.stem and "\\" not in path.stem


def test_compute_output_path_unknown_manga_fallback() -> None:
    path = package.compute_output_path("/data/manga", None, "Some Title")
    assert path.parent == Path("/data/manga/manga-unknown")


def test_compute_output_path_sanitizes_manga_id_slug() -> None:
    # The mangaId slug is also run through sanitize_filename (D-24 guard); a weird id
    # value can never inject a path separator.
    path = package.compute_output_path("/data/manga", "../evil", "T")  # type: ignore[arg-type]
    assert ".." not in path.parts
    assert path.parent.name.startswith("manga-")


def test_compute_output_path_empty_title_falls_back() -> None:
    path = package.compute_output_path("/data/manga", 1, "   ")
    assert path.stem  # non-empty fallback applied
