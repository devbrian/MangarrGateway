"""Framework-internal typed source exception (SRCH-03, D-14).

A source hook raises :class:`SourceError` carrying a contract ``code``; the fan-out
orchestrator catches it and maps it to a ``SourceWarning`` (never building the
envelope itself). Kept separate from the top-level ``errors.py`` (which owns the
HTTP/contract error envelope) so sources import this without pulling FastAPI —
mirrors the ``RateLimited`` constructor-stores-attribute shape in ``errors.py``.
"""

from __future__ import annotations


class SourceError(Exception):
    """A source failure carrying a contract warning ``code`` (D-14).

    ``code`` is one of the contract error/warning codes (e.g. ``source_unavailable``);
    the fan-out reads ``.code`` to build the ``SourceWarning``. ``status`` carries the
    upstream HTTP status (or ``None`` when the failure was not a response-status
    failure) so callers can branch on the exact status — e.g. the engine's
    stale-baseUrl recovery checks ``e.status == 403`` instead of substring-matching
    the rendered message (WR-07).
    """

    def __init__(
        self, code: str, message: str = "", *, status: int | None = None
    ) -> None:
        self.code = code
        self.status = status
        super().__init__(message or code)
