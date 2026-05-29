"""Filename sanitization (D-08, SEC-02, T-01-06).

Source-scraped strings (mangaTitle/chapter) arrive untrusted in Phase 2+ and are
composed into gateway-computed output paths. A malicious name (e.g. ``../../etc``)
must never traverse outside the output root. This helper is real but unused until
Phase 2 — it is the one kept protection of the safe-by-design stance (D-08).
"""

from __future__ import annotations

import re

# Characters illegal/dangerous across the common filesystems we target.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOTS_ONLY = re.compile(r"^\.+$")


def sanitize_filename(name: str, *, fallback: str = "untitled") -> str:
    """Return a safe single path component derived from ``name``.

    Strips any directory separators and traversal sequences, removes illegal
    characters, and collapses whitespace. The result NEVER contains ``/``,
    ``\\``, or a ``..`` segment, so it cannot escape its parent directory.
    """
    # Take only the final component — defeats embedded "../" / absolute paths.
    candidate = name.replace("\\", "/").split("/")[-1]
    candidate = _ILLEGAL.sub("_", candidate)
    candidate = candidate.strip(" .")  # no leading/trailing dots or spaces
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate or _DOTS_ONLY.match(candidate):
        return fallback
    return candidate
