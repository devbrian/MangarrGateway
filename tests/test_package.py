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


# ─────────────────────────── describe_non_image (#238) ──────────────────────────


def test_describe_non_image_empty_body() -> None:
    assert package.describe_non_image(b"") == "0 bytes (empty body)"


def test_describe_non_image_classifies_html_challenge() -> None:
    # A Cloudflare-style challenge page — the realistic MangaBall #238 non-image body.
    body = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    out = package.describe_non_image(body)
    assert "sniff=html" in out
    assert f"{len(body)} bytes" in out
    assert "<!DOCTYPE html>" in out  # head preview is printable-verbatim


def test_describe_non_image_classifies_html_with_leading_whitespace() -> None:
    body = b"\n\n   <html><body>520 origin error</body></html>"
    assert "sniff=html" in package.describe_non_image(body)


def test_describe_non_image_classifies_json_error_body() -> None:
    body = b'{"error":"rate limited","retryAfter":60}'
    assert "sniff=json" in package.describe_non_image(body)


def test_describe_non_image_classifies_plain_text() -> None:
    assert "sniff=text" in package.describe_non_image(b"520: Web server returned error")


def test_describe_non_image_classifies_gzip_magic() -> None:
    assert "sniff=gzip" in package.describe_non_image(b"\x1f\x8b\x08\x00rest")


def test_describe_non_image_classifies_unknown_binary() -> None:
    # A truncated PNG header is binary, not text/html/json/gzip.
    out = package.describe_non_image(_png_bytes()[:20])
    assert "sniff=binary" in out


def test_describe_non_image_escapes_nonprintable_head_and_truncates() -> None:
    body = b"\x00\x01\x02\xff" + b"A" * 100
    out = package.describe_non_image(body)
    # Non-printable head bytes are \xNN-escaped (log/JSON-safe) and long bodies elide.
    assert "\\x00" in out and "\\x01" in out and "\\xff" in out
    assert "…" in out
    assert f"{len(body)} bytes" in out


def test_describe_non_image_escapes_quotes_and_backslashes_in_preview() -> None:
    # A JSON error body's literal " and \ must be backslash-escaped so they cannot
    # break the surrounding head="..." quoting (#238 CodeRabbit).
    body = b'{"error": "C:\\\\fail"}'
    out = package.describe_non_image(body)
    head_field = out.split('head="', 1)[1]
    assert head_field.endswith('"')
    inner = head_field[:-1]
    # Every literal body quote is escaped to \" and every backslash doubled, so
    # stripping the escape sequences leaves no bare " or \ that could break quoting.
    stripped = inner.replace('\\"', "").replace("\\\\", "")
    assert '"' not in stripped and "\\" not in stripped
    assert inner.count('\\"') == body.count(b'"')  # all three body quotes escaped


# ──────────────────────────────────── write_cbz ─────────────────────────────────


def test_write_cbz_is_zip_stored_zero_padded_reading_order(tmp_path: Path) -> None:
    pages = [(f"page-{i}.jpg", f"img{i}".encode()) for i in range(1, 11)]  # 10 pages
    final = tmp_path / "out.cbz"

    package.write_cbz(pages, final, job_id="j_test")

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

    package.write_cbz(pages, final, job_id="j_test")

    with zipfile.ZipFile(final) as zf:
        # 2 pages → width max(2, 1) == 2 → zero-padded 01/02.
        assert zf.read("01.png") == pages[0][1]
        assert zf.read("02.png") == pages[1][1]


def test_write_cbz_preserves_original_extension(tmp_path: Path) -> None:
    pages = [("first.png", b"a"), ("second.jpeg", b"b"), ("third", b"c")]
    final = tmp_path / "out.cbz"

    package.write_cbz(pages, final, job_id="j_test")

    with zipfile.ZipFile(final) as zf:
        names = zf.namelist()
    assert names[0].endswith(".png")
    assert names[1].endswith(".jpeg")
    assert names[2].endswith(".jpg")  # no extension → default .jpg


def test_write_cbz_contains_no_comicinfo_or_xml(tmp_path: Path) -> None:
    # PKG-04: page images ONLY — no metadata file.
    pages = [("a.jpg", b"a"), ("b.jpg", b"b")]
    final = tmp_path / "out.cbz"

    package.write_cbz(pages, final, job_id="j_test")

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
        package.write_cbz(pages, final, job_id="j_test")  # type: ignore[arg-type]

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


def test_compute_output_path_fallback_discriminator_disambiguates_unknown() -> None:
    """Issue #9: two different sources/chapters with the same title must not
    collide inside ``manga-unknown/`` when no mangaId is provided.

    The source-stable chapter id is folded into the filename as a short hash so
    distinct discriminators yield distinct filenames; the bucket folder remains
    ``manga-unknown/`` (D-24 fallback)."""
    path_a = package.compute_output_path(
        "/data/manga", None, "Chapter 1", fallback_discriminator="uuid-aaa"
    )
    path_b = package.compute_output_path(
        "/data/manga", None, "Chapter 1", fallback_discriminator="uuid-bbb"
    )
    assert path_a.parent == Path("/data/manga/manga-unknown")
    assert path_b.parent == Path("/data/manga/manga-unknown")
    assert path_a != path_b
    # Same discriminator → deterministic path (idempotent re-grabs).
    again = package.compute_output_path(
        "/data/manga", None, "Chapter 1", fallback_discriminator="uuid-aaa"
    )
    assert again == path_a


def test_compute_output_path_discriminator_ignored_when_mangaid_known() -> None:
    """When ``manga_id`` is set the discriminator is NOT applied — the mangaId
    already disambiguates and the legacy filename layout is preserved."""
    with_disc = package.compute_output_path(
        "/data/manga", 178, "Chapter 1", fallback_discriminator="uuid-aaa"
    )
    without = package.compute_output_path("/data/manga", 178, "Chapter 1")
    assert with_disc == without


# ───────────────── compute_output_path: per-series folder tier (#16) ─────────────


def test_compute_output_path_mangaid_present_ignores_manga_title() -> None:
    """Tier 1 regression: a present ``manga_id`` keeps the ``manga-{id}`` folder
    byte-for-byte; a supplied ``manga_title`` does NOT change it."""
    path = package.compute_output_path(
        "/data/manga", 178, "Chapter 1", manga_title="Solo Leveling"
    )
    assert path == Path("/data/manga/manga-178/Chapter 1.cbz")


def test_compute_output_path_per_series_folder_from_title() -> None:
    """Tier 2 (new): mangaId absent + known title → ``manga-{sanitized title}/``,
    and the filename STILL carries the #9 chapter-id hash."""
    path = package.compute_output_path(
        "/data/manga",
        None,
        "Chapter 1",
        manga_title="Pokemon Pixiv Oneshots",
        fallback_discriminator="uuid-aaa",
    )
    assert path.parent == Path("/data/manga/manga-Pokemon Pixiv Oneshots")
    # #9 chapter-id hash still folded into the filename (manga_id is None branch).
    assert path.stem.startswith("Chapter 1-")
    # Distinct discriminator → distinct filename in the SAME per-series folder.
    other = package.compute_output_path(
        "/data/manga",
        None,
        "Chapter 1",
        manga_title="Pokemon Pixiv Oneshots",
        fallback_discriminator="uuid-bbb",
    )
    assert other.parent == path.parent
    assert other != path


def test_compute_output_path_per_series_folder_is_stable() -> None:
    """HDL-02: same (manga_title, discriminator) → identical Path on repeat calls."""
    args = ("/data/manga", None, "Chapter 1")
    kwargs = {"manga_title": "Solo Leveling", "fallback_discriminator": "uuid-aaa"}
    first = package.compute_output_path(*args, **kwargs)  # type: ignore[arg-type]
    second = package.compute_output_path(*args, **kwargs)  # type: ignore[arg-type]
    assert first == second


@pytest.mark.parametrize("manga_title", [None, "", "../../"])
def test_compute_output_path_unusable_title_falls_back_to_unknown(
    manga_title: str | None,
) -> None:
    """Tier 3 (fallback): mangaId absent + None/empty/dots-only title (sanitizes to
    empty) → ``manga-unknown/`` (today's behavior preserved)."""
    path = package.compute_output_path(
        "/data/manga", None, "Chapter 1", manga_title=manga_title
    )
    assert path.parent == Path("/data/manga/manga-unknown")


def test_compute_output_path_malicious_title_cannot_escape_root() -> None:
    """T-q7x-01 / SEC-02: a traversal ``manga_title`` is reduced to one safe folder
    component and cannot escape the output root."""
    path = package.compute_output_path(
        "/data/manga", None, "Chapter 1", manga_title="../../etc/passwd"
    )
    assert ".." not in path.parts
    assert path.parent.name.startswith("manga-")
    # The folder component carries no path separator.
    assert "/" not in path.parent.name and "\\" not in path.parent.name


# ──────────── compute_output_path: falsy/non-positive manga_id (Bug A1) ──────────


@pytest.mark.parametrize("manga_id", [0, -1, -42])
def test_compute_output_path_nonpositive_mangaid_buckets_under_title(
    manga_id: int,
) -> None:
    """Bug A1: ``manga_id <= 0`` (notably the ``0`` placeholder) is NOT a real id —
    it must NOT mint a ``manga-0`` (or ``manga--1``) folder. It falls through to the
    per-series title bucket exactly like ``None`` and STILL gets the #9 hash."""
    path = package.compute_output_path(
        "/data/manga",
        manga_id,
        "Chapter 1",
        manga_title="Some Series",
        fallback_discriminator="uuid-aaa",
    )
    assert path.parent == Path("/data/manga/manga-Some Series")
    assert f"manga-{manga_id}" not in {p for p in path.parts}
    # #9 collision hash applied on the id-less path (regression: 0 used to skip it).
    assert path.stem.startswith("Chapter 1-")


@pytest.mark.parametrize("manga_id", [0, -1])
def test_compute_output_path_nonpositive_mangaid_unusable_title_unknown(
    manga_id: int,
) -> None:
    """Bug A1: ``manga_id <= 0`` with no usable title falls to ``manga-unknown/``
    (tier 3) and STILL applies the #9 collision hash — never ``manga-0``."""
    path = package.compute_output_path(
        "/data/manga", manga_id, "Chapter 1", fallback_discriminator="uuid-aaa"
    )
    assert path.parent == Path("/data/manga/manga-unknown")
    assert path.stem.startswith("Chapter 1-")


@pytest.mark.parametrize("manga_id", [0, -5])
def test_compute_output_path_nonpositive_mangaid_two_series_no_collision(
    manga_id: int,
) -> None:
    """Bug A1 amplifies Bug B (over-concentration): with the OLD code every
    ``manga_id=0`` grab landed in ``manga-0/`` with a bare (hashless) filename, so
    two series sharing a chapter title collided. Now distinct discriminators yield
    distinct paths even at ``manga_id=0``."""
    a = package.compute_output_path(
        "/data/manga", manga_id, "Chapter 1", fallback_discriminator="series-a"
    )
    b = package.compute_output_path(
        "/data/manga", manga_id, "Chapter 1", fallback_discriminator="series-b"
    )
    assert a != b


def test_compute_output_path_positive_mangaid_unchanged() -> None:
    """Bug A1 regression: a POSITIVE ``manga_id`` keeps the exact legacy layout —
    ``manga-{id}`` folder, NO #9 hash, ``manga_title`` ignored."""
    path = package.compute_output_path(
        "/data/manga",
        178,
        "Chapter 1",
        manga_title="Solo Leveling",
        fallback_discriminator="uuid-aaa",
    )
    assert path == Path("/data/manga/manga-178/Chapter 1.cbz")


# ───────────────── compute_output_path: separator neutralization (Bug A2) ────────


def test_compute_output_path_title_separators_neutralized_not_dropped() -> None:
    """Bug A2: a title with separators NEUTRALIZES them (``_``) instead of dropping
    the leading segments. ``/a/nonymous]`` must NOT become ``nonymous]``."""
    path = package.compute_output_path("/data/manga", 7, "/a/nonymous]")
    assert path == Path("/data/manga/manga-7/_a_nonymous].cbz")
    # Reproduce the EXACT reported scenario (manga_id=0 placeholder + slashed group),
    # asserting the old malformed ``/data/manga/manga-0/nonymous].cbz`` is gone.
    repro = package.compute_output_path(
        "/data/manga", 0, "/a/nonymous]", manga_title="Some Series"
    )
    assert repro.parent == Path("/data/manga/manga-Some Series")
    assert repro.stem.startswith("_a_nonymous]")
    assert "manga-0" not in {p for p in repro.parts}


# ───────────────── staging_temp_glob: per-job scoping (Bug B) ────────────────────


@pytest.mark.parametrize(
    ("output_format", "expected_suffix"),
    [("cbz", ".cbz.tmp"), ("cbt", ".cbt.tmp"), ("folder", ".folder.tmp")],
)
def test_staging_temp_glob_is_job_scoped(
    output_format: str, expected_suffix: str
) -> None:
    """Bug B: the cleanup glob is pinned to the OWNING job_id + format suffix so it
    can never match a sibling job's temp."""
    pattern = package.staging_temp_glob("j_abc", output_format=output_format)
    assert pattern == f"j-j_abc-*{expected_suffix}"
    # Wildcard only between the per-job prefix and the format suffix → never a bare
    # ``*.cbz.tmp`` that would sweep siblings.
    assert not pattern.startswith("*")


def test_staging_temp_glob_unknown_format_defaults_cbz() -> None:
    assert package.staging_temp_glob("j_x", output_format="weird").endswith(".cbz.tmp")


def test_write_cbz_staging_temp_is_job_scoped(tmp_path: Path) -> None:
    """Bug B: the in-flight ``write_cbz`` staging temp is named with the owning
    job_id, so a per-job glob matches it while a different job's glob does NOT."""
    final = tmp_path / "out.cbz"
    captured: list[str] = []
    real_replace = package.os.replace

    def _spy(src: str, dst: object) -> None:
        # Snapshot the staging-temp name the moment before atomic publish.
        captured.extend(p.name for p in tmp_path.glob("*.cbz.tmp"))
        real_replace(src, dst)  # type: ignore[arg-type]

    monkey = pytest.MonkeyPatch()
    monkey.setattr(package.os, "replace", _spy)
    try:
        package.write_cbz([("a.jpg", b"x")], final, job_id="j_OWNER")
    finally:
        monkey.undo()

    assert len(captured) == 1
    temp_name = captured[0]
    assert Path(tmp_path, temp_name).match(
        package.staging_temp_glob("j_OWNER", output_format="cbz")
    )
    # A different job's per-job glob must NOT match this temp.
    assert not Path(tmp_path, temp_name).match(
        package.staging_temp_glob("j_OTHER", output_format="cbz")
    )
