"""Filename sanitization (D-08, SEC-02, T-01-06).

Source-scraped strings (mangaTitle/chapter) arrive untrusted in Phase 2+ and are
composed into gateway-computed output paths. A malicious name (e.g. ``../../etc``)
must never traverse outside the output root. This helper is real but unused until
Phase 2 — it is the one kept protection of the safe-by-design stance (D-08).
"""

from __future__ import annotations

import re

# Characters illegal/dangerous across the common filesystems we target. Includes
# BOTH separators (``/`` and ``\``) so they are NEUTRALIZED to ``_`` in place
# rather than used to split the value (which silently dropped data — issue
# output-path-group-slash-filenotfound: ``AC/DC`` → ``DC``, ``/a/nonymous]`` →
# ``nonymous]``). The result therefore never contains a separator.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# A run of two-or-more dots (a ``..`` traversal segment, or any longer dot run).
_DOT_RUN = re.compile(r"\.{2,}")
_DOTS_ONLY = re.compile(r"^\.+$")
# At least one alphanumeric word character — a candidate with NONE (e.g. a pure
# traversal value ``../../`` that neutralizes to all-``_``) carries no usable name
# and falls back, preserving the pre-fix ``manga-unknown`` tier-3 behavior for
# separator/dots-only junk (output-path-group-slash-filenotfound).
_HAS_WORD = re.compile(r"[^\W_]")


def sanitize_filename(name: str, *, fallback: str = "untitled") -> str:
    """Return a safe single path component derived from ``name``.

    NEUTRALIZES directory separators and traversal sequences (it does NOT drop
    leading path segments), removes illegal characters, and collapses
    whitespace. Group/title data is preserved with separators substituted:
    ``AC/DC`` → ``AC_DC``, ``/a/nonymous]`` → ``_a_nonymous]``, ``[Anonymous]``
    unchanged. The result NEVER contains ``/``, ``\\``, or a usable ``..``
    segment, so it cannot escape its parent directory (SEC-02 / D-08 / T-01-06).
    """
    # NEUTRALIZE separators + illegal chars in place (no split) so embedded path
    # data is preserved as a single safe component. ``_ILLEGAL`` already covers
    # ``/`` and ``\``, so every separator becomes ``_`` and none survives.
    candidate = _ILLEGAL.sub("_", name)
    # Collapse any ``..``+ dot-run so a traversal segment (e.g. ``../../etc`` →
    # ``.._.._etc`` after separator-neutralization, or a bare ``..``) can never
    # remain a usable parent-dir reference. A single ``.`` is left intact.
    candidate = _DOT_RUN.sub("_", candidate)
    candidate = candidate.strip(" .")  # no leading/trailing dots or spaces
    candidate = re.sub(r"\s+", " ", candidate).strip()
    # Empty, pure-dots, or all-noise (no alphanumeric word char, e.g. ``____`` from
    # a neutralized ``../../``) → the caller's fallback. This keeps a pure-traversal
    # value out of the per-series folder tier (it stays ``manga-unknown``).
    if not candidate or _DOTS_ONLY.match(candidate) or not _HAS_WORD.search(candidate):
        return fallback
    return candidate
