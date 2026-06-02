"""Framework-owned httpx session-prep seam (D-01).

The lightweight, browser-free twin of the anti-bot clearance seam
(:mod:`framework.antibot`). Where ``AntiBotSolver`` drives a stealth browser to
capture a ``cf_clearance`` cookie + the UA it was bound to, ``SessionPrep`` does
a plain httpx HTML GET to capture a CSRF token + the session cookie a
PHP/Laravel/Django form-POST backend hands out, then exposes them for per-request
header injection (the WIRING lives in :mod:`framework.context`).

Lifecycle mirrors the antibot seam SHAPE — construct → acquire (GET HTML) →
refresh-on-403 — not its browser internals:

* :class:`SessionCredentials` is the ``Clearance`` analog: it carries the harvested
  ``cookies`` (incl. ``PHPSESSID``) and the ``csrf_token``.
* :class:`SessionPrep` is the ``AntiBotSolver`` analog Protocol; its single public
  method is ``prepare(source_key)``.
* :class:`NoSessionPrep` is the ``NoopSolver`` analog default — returns ``None`` for
  every key so sources declaring ``session_prep = None`` (MangaDex/Comix) stay
  byte-for-byte unchanged.
* :class:`CsrfBootstrap` is the concrete impl: lazy-acquire the credentials on
  first use, cache per source key, and re-GET on ``force_refresh=True`` (D-03/D-05
  refresh-on-403). The internal ``force_refresh`` keyword is kept OFF the public
  ``SessionPrep`` Protocol (mirror antibot's ``force_resolve`` discipline, D-41).

The harvested CSRF token feeds the **``X-CSRF-Token``** request header (the dash
form — MangaBall rejects the Django ``X-CSRFToken`` form; RECON §"Session / CSRF
bootstrap"); :meth:`SourceContext.post_json` does the wiring.

Security (T-07-01): the token and cookie VALUES are credentials — they live in
memory only, are never persisted to disk, and are NEVER logged (mirror the
antibot "never log the cookie value" discipline). Only the bootstrap *event* is
logged, with counts, never values.

R1: the bootstrap GET rides the ONE shared transport drawn from the
:class:`SessionManager`. This provider NEVER builds a second httpx client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .session import SessionManager

_log = logging.getLogger("manga_gateway")


@dataclass
class SessionCredentials:
    """Captured form-POST session credentials: cookies + the CSRF token.

    The :class:`~framework.antibot.Clearance` analog — ``cookies`` carries the
    session cookie (e.g. ``PHPSESSID``) the per-request ``Cookie`` header is built
    from, and ``csrf_token`` feeds the ``X-CSRF-Token`` header. Both are
    credentials: never log the values (T-07-01).
    """

    cookies: dict[str, str]
    csrf_token: str


@runtime_checkable
class SessionPrep(Protocol):
    """Resolves a per-source HTML/CSRF bootstrap into reusable credentials.

    The httpx-only analog of :class:`~framework.antibot.AntiBotSolver`. The single
    public method is ``prepare(source_key)``; an internal ``force_refresh`` escape
    hatch (used only on the CSRF-403 retry path) is deliberately kept OFF this
    Protocol so the public seam does not churn (mirror D-41).
    """

    async def prepare(self, source_key: str) -> SessionCredentials | None:
        """Return credentials for ``source_key``, or ``None`` if none are needed."""
        ...


class NoSessionPrep:
    """Default provider: no session bootstrap (the ``NoopSolver`` analog).

    Returns ``None`` for every key so a ``session_prep = None`` source
    (MangaDex/Comix) contributes no headers and is byte-for-byte unchanged.
    """

    async def prepare(self, source_key: str) -> None:
        return None


class _CsrfMetaParser(HTMLParser):
    """Stdlib HTML parser that captures the first ``meta[name=csrf-token]`` content.

    The meta-token parse is cheap (a single tag scan) so it runs inline; the large
    manifest HTML parse is a Plan 03 concern that offloads via ``asyncio.to_thread``.
    stdlib ``html.parser`` is used here to avoid a new dependency (lxml promotion is
    Plan 02); the seam stays consistent on one parser.
    """

    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.token is not None or tag != "meta":
            return
        attr = dict(attrs)
        if attr.get("name") == "csrf-token":
            content = attr.get("content")
            if content:
                self.token = content


def _parse_csrf_token(html: str) -> str | None:
    """Extract the ``meta[name=csrf-token]`` content from an HTML page (or None)."""
    parser = _CsrfMetaParser()
    parser.feed(html)
    return parser.token


class CsrfBootstrap:
    """Captures a CSRF token + session cookie via a plain httpx HTML GET (D-01).

    For each configured ``source_key`` it GETs the source's bootstrap HTML page
    through the ONE shared transport (R1 — never a second client), parses
    ``meta[name="csrf-token"]`` and harvests the ``PHPSESSID`` (and any other set)
    cookie, caches the result, and returns :class:`SessionCredentials`. A key NOT
    configured for csrf-bootstrap returns ``None`` (so MangaDex/Comix get an empty
    contribution). ``prepare(..., force_refresh=True)`` discards the cache and
    re-GETs the HTML page — the D-03/D-05 refresh-on-403 mechanism.

    ``force_refresh`` is the internal escalation kwarg kept OFF the
    :class:`SessionPrep` Protocol (mirror antibot's ``force_resolve``, D-41).
    """

    # Cookie names harvested from the bootstrap response. PHPSESSID is the
    # load-bearing session cookie; the analytics cookies (_ga*, __suvt) are
    # intentionally NOT harvested (irrelevant to the API session).
    _SESSION_COOKIE_NAMES: frozenset[str] = frozenset({"PHPSESSID"})

    def __init__(
        self,
        *,
        keys: Iterable[str],
        session: SessionManager,
        bootstrap_urls: Mapping[str, str],
    ) -> None:
        self._keys = frozenset(keys)
        self._session = session
        self._bootstrap_urls = dict(bootstrap_urls)
        self._cache: dict[str, SessionCredentials] = {}

    async def prepare(
        self, source_key: str, *, force_refresh: bool = False
    ) -> SessionCredentials | None:
        """Return cached or freshly-acquired credentials for ``source_key``.

        ``None`` for an unconfigured key (MangaDex/Comix). ``force_refresh`` skips
        the cache and re-GETs the bootstrap HTML page (D-03/D-05).
        """
        if source_key not in self._keys:
            return None
        if not force_refresh and source_key in self._cache:
            return self._cache[source_key]
        creds = await self._acquire(source_key)
        self._cache[source_key] = creds
        return creds

    async def _acquire(self, source_key: str) -> SessionCredentials:
        """GET the bootstrap HTML page → parse the token + harvest the cookie."""
        url = self._bootstrap_urls.get(source_key)
        if url is None:
            # Defensive: a configured key with no URL is a wiring bug, not a
            # runtime condition — surface it loudly (no credential value leak).
            raise KeyError(f"no bootstrap URL configured for source {source_key!r}")
        resp = await self._session.transport.request("GET", url)
        token = _parse_csrf_token(resp.text)
        if not token:
            raise ValueError(
                f"no meta[name=csrf-token] found on bootstrap page for {source_key!r}"
            )
        cookies = {
            name: value
            for name, value in resp.cookies.items()
            if name in self._SESSION_COOKIE_NAMES
        }
        # Never log the token or cookie VALUE (T-07-01) — counts only.
        _log.info(
            "CsrfBootstrap acquired session credentials for %s (%d cookies)",
            source_key,
            len(cookies),
        )
        return SessionCredentials(cookies=cookies, csrf_token=token)
