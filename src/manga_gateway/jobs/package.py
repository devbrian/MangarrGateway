"""CBZ packaging primitives (PKG-03/04, D-24/D-25/D-26).

The blocking, source-agnostic units the Plan 03 job engine composes: image
validation, output-path computation, and archive writing. Every function here is
**synchronous and BLOCKING by design** (Pillow decode, ``zipfile`` writes,
``os.replace``); the engine calls them via ``asyncio.to_thread`` so the event loop
never stalls (Pitfall 2). Do NOT add ``async``/event-loop calls here.

Guarantees:

* ``is_valid_image`` decodes the header via Pillow ``verify()`` only: it NEVER
  recompresses (PKG-04 originals) and rejects HTML-error-page / truncated bodies
  (Pitfall 6, T-03-03).
* ``write_cbz`` builds a ``ZIP_STORED`` archive (images are already compressed, so
  deflate is wasted CPU) in a staging temp, then ``os.replace`` publishes it
  atomically only on success (D-26); a failure mid-write removes the staging temp
  and leaves no partial CBZ (D-29). It writes page images ONLY: no metadata XML
  file is ever emitted (PKG-04). Mangarr adds metadata after hand-off.
* ``compute_output_path`` lays out ``{output_root}/manga-{slug}/{title}{suffix}``
  with the source-scraped title AND the folder slug run through ``util/sanitize.py``
  so neither can traverse out of the output root (SEC-02/D-24/D-25, T-03-04). The
  folder slug is a three-tier decision (issue #16): (1) ``manga-{mangaId}`` when a
  ``manga_id`` is present (UNCHANGED); (2) else ``manga-{sanitized manga_title}``
  when a resolved series title is available — per-series foldering for mangaId-less
  grabs; (3) else the flat ``manga-unknown/`` fallback (D-24). On every mangaId-less
  path a ``fallback_discriminator`` (typically the source-stable chapter id) is
  folded into the filename as a short hash so two different series with the same
  chapter title cannot collide (issue #9) — this still applies in the per-series
  folder case.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ..util.sanitize import sanitize_filename

# Default page extension when an at-home filename carries none.
_DEFAULT_EXT = ".jpg"
# Suffix per advertised output format (folder has no archive suffix).
_FORMAT_SUFFIX = {"cbz": ".cbz", "cbt": ".cbt"}


def is_valid_image(content: bytes) -> bool:
    """Return True iff ``content`` decodes as an image (validate-only, no recompress).

    ``Image.verify()`` parses the header/structure and raises on a non-image (an
    HTML error body, a truncated blob, or empty bytes), which is exactly the
    integrity check we want before packaging (Pitfall 6 / T-03-03). The original
    bytes are written into the CBZ unchanged: this function never transcodes (PKG-04).
    """
    if not content:
        return False
    try:
        Image.open(io.BytesIO(content)).verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        # Non-image / truncated / unidentifiable body — the exact failures Pillow
        # raises for a bad page; narrowed from a bare ``except`` (IN-01).
        return False


# Head bytes sniffed/previewed when a page fails ``is_valid_image`` (#238).
_SNIFF_HEAD_LEN = 48


def _classify_non_image(content: bytes) -> str:
    """Cheap magic-byte / leading-content class for bytes that aren't an image.

    Pure prefix inspection — NEVER decodes or fully parses the body. Distinguishes
    the realistic ``page N invalid`` causes from each other: a CDN/anti-bot HTML
    error page (``html``), a JSON/text error body (``json``/``text``, e.g. a bare
    ``520`` or a Cloudflare "Just a moment" snippet), a double-gzipped/compressed
    blob (``gzip``), or otherwise-unidentifiable bytes (``binary``).
    """
    if content[:2] == b"\x1f\x8b":  # gzip magic
        return "gzip"
    lead = content[:512].lstrip()[:64].lower()
    if lead.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml")) or (
        b"<html" in lead
    ):
        return "html"
    if lead[:1] in (b"{", b"["):
        return "json"
    sample = content[:256]
    printable = sum(1 for b in sample if 0x20 <= b <= 0x7E or b in (0x09, 0x0A, 0x0D))
    if sample and printable / len(sample) > 0.85:
        return "text"
    return "binary"


def describe_non_image(content: bytes, *, head_len: int = _SNIFF_HEAD_LEN) -> str:
    """Diagnostic one-liner for bytes that FAILED :func:`is_valid_image` (#238).

    Surfaces the byte length + a magic-byte/leading-content classification + a short
    printable preview of the head so a ``page N invalid`` failure is self-diagnosing
    from telemetry alone (CDN HTML error page vs anti-bot challenge vs truncation vs
    empty body) — WITHOUT recompressing or fully parsing the body. Non-printable
    head bytes are escaped ``\\xNN`` and printable ``"``/``\\`` are backslash-escaped,
    so the preview never breaks the surrounding ``head="..."`` quoting (#238).
    """
    n = len(content)
    if n == 0:
        return "0 bytes (empty body)"
    kind = _classify_non_image(content)
    head = content[:head_len]

    def _escape(b: int) -> str:
        if b == 0x5C:  # backslash
            return "\\\\"
        if b == 0x22:  # double quote
            return '\\"'
        return chr(b) if 0x20 <= b <= 0x7E else f"\\x{b:02x}"

    preview = "".join(_escape(b) for b in head)
    suffix = "…" if n > head_len else ""
    return f'{n} bytes, sniff={kind}, head="{preview}{suffix}"'


def compute_output_path(
    output_root: str,
    manga_id: int | None,
    title: str,
    *,
    output_format: str = "cbz",
    fallback_discriminator: str | None = None,
    manga_title: str | None = None,
) -> Path:
    """Compute the gateway-owned output path, traversal-safe (D-24/D-25, SEC-02).

    Layout: ``{output_root}/manga-{slug}/{sanitized title}{suffix}``. The folder
    ``slug``, the source-scraped ``title``, and the ``manga_title`` all pass through
    ``sanitize_filename`` so a malicious value (e.g. ``../../etc``) is reduced to a
    single safe path component and can never escape ``output_root`` (T-03-04).
    ``folder`` format yields a directory path (no suffix).

    Folder-slug tiers (issue #16, LOCKED Option A):

    1. ``manga_id`` present → ``manga-{sanitize(mangaId)}`` (UNCHANGED).
    2. ``manga_id`` is ``None`` but ``manga_title`` sanitizes to a NON-EMPTY string
       → ``manga-{that slug}``. ``fallback=""`` is the signal that an empty / dots-
       only / all-unsafe title means "no usable title" and falls through to tier 3.
    3. otherwise → the flat ``manga-unknown/`` bucket (D-24).

    Missing-mangaId filename hash (D-24, issue #9): on EVERY mangaId-less path
    (tiers 2 and 3 both have ``manga_id is None``) the optional
    ``fallback_discriminator`` — typically the source-stable chapter id — is folded
    into the filename as an 8-char SHA-256 prefix: ``{title}-{hash}{suffix}``. This
    prevents two unrelated series sharing a chapter title from clobbering each
    other, and is preserved even in the per-series folder (tier 2). When the
    discriminator is ``None`` the filename is left bare.
    """
    if manga_id is not None:
        slug = sanitize_filename(str(manga_id), fallback="unknown")
    else:
        # Tier 2: a non-empty sanitized series title buckets per-series; fallback=""
        # treats an empty/dots-only/all-unsafe title as "no usable title" → tier 3.
        # The title MUST pass through sanitize_filename (SEC-02/T-q7x-01 traversal).
        # Accepted theoretical case: a title sanitizing to a pure integer (e.g.
        # "2020") could share a dir with a real mangaId=2020 — benign on this
        # no-mangaId path; the #9 chapter-id filename hash still prevents file
        # clobber. Do NOT add complexity for it.
        title_slug = (
            sanitize_filename(manga_title, fallback="")
            if manga_title is not None
            else ""
        )
        slug = title_slug or "unknown"
    folder = f"manga-{slug}"
    name = sanitize_filename(title, fallback="untitled")
    if manga_id is None and fallback_discriminator:
        digest = hashlib.sha256(fallback_discriminator.encode("utf-8")).hexdigest()[:8]
        name = f"{name}-{digest}"
    suffix = _FORMAT_SUFFIX.get(output_format, "")  # folder -> "" (directory)
    return Path(output_root) / folder / f"{name}{suffix}"


def _entry_name(index: int, original: str, width: int) -> str:
    """Zero-padded reading-order entry name preserving the extension (Pitfall 5)."""
    ext = Path(original).suffix or _DEFAULT_EXT
    return f"{index:0{width}d}{ext}"


def write_cbz(pages: list[tuple[str, bytes]], final_path: Path) -> None:
    """Write a ZIP_STORED CBZ to ``final_path``, atomically (D-26, PKG-03/04).

    ``pages`` is ``[(original_filename, content), ...]`` in reading order. Entries
    are zero-padded to a width derived from the page count so they sort correctly in
    a manga reader (Pitfall 5), each keeping its original extension. Images are
    stored uncompressed (``ZIP_STORED``) and unchanged (PKG-04). The archive is built
    in a staging temp in the SAME directory, then ``os.replace`` publishes it
    atomically only on success; any failure removes the staging temp so no partial
    CBZ is ever published (D-26/D-29).

    Synchronous + blocking by design: call via ``asyncio.to_thread`` (Pitfall 2).
    """
    width = max(2, len(str(len(pages))))
    final_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(final_path.parent), suffix=".cbz.tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
            for i, (original, content) in enumerate(pages, start=1):
                zf.writestr(_entry_name(i, original, width), content)
        os.replace(tmp, final_path)  # atomic publish (D-26)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)  # no partial CBZ (D-29)
        raise


def write_cbt(pages: list[tuple[str, bytes]], final_path: Path) -> None:
    """Write a CBT (tar) archive atomically: ``write_cbz`` with a tar swap (Open Q3).

    Same ordering / zero-padding / staging->atomic-publish guarantees as
    ``write_cbz``; tar stores entries uncompressed in ``"w"`` mode, matching the
    ZIP_STORED rationale. Synchronous + blocking: call via ``asyncio.to_thread``.
    """
    width = max(2, len(str(len(pages))))
    final_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(final_path.parent), suffix=".cbt.tmp")
    os.close(fd)
    try:
        with tarfile.open(tmp, "w") as tf:
            for i, (original, content) in enumerate(pages, start=1):
                info = tarfile.TarInfo(name=_entry_name(i, original, width))
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        os.replace(tmp, final_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_folder(pages: list[tuple[str, bytes]], final_dir: Path) -> None:
    """Write zero-padded page images into ``final_dir`` (folder format, Open Q3).

    Builds into a staging sibling directory then ``os.replace`` swaps it into place
    so a partially-written folder is never published (D-26/D-29). An existing
    ``final_dir`` from a prior grab is removed first so ``os.replace`` does not fail
    on a populated target (Windows + non-empty POSIX), making ``folder`` re-grabs
    behave like CBZ/CBT re-grabs (WR-04). Synchronous + blocking: call via
    ``asyncio.to_thread`` — this runs inside that offloaded thread already.
    """
    width = max(2, len(str(len(pages))))
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(final_dir.parent), suffix=".folder.tmp"))
    try:
        for i, (original, content) in enumerate(pages, start=1):
            (staging / _entry_name(i, original, width)).write_bytes(content)
        # Clear any existing folder so the swap does not fail on a populated dir
        # (re-grab path, WR-04); CBZ/CBT publish over their target via os.replace.
        shutil.rmtree(final_dir, ignore_errors=True)
        os.replace(staging, final_dir)  # atomic dir swap (same FS)
    except BaseException:
        for child in staging.glob("*"):
            child.unlink(missing_ok=True)
        if staging.exists():
            staging.rmdir()
        raise
