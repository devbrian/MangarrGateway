"""Per-request source context — rate-limited + retried HTTP (SRC-02/SRC-03).

The framework injects a ``SourceContext`` into a source hook. It draws the ONE
shared transport from the :class:`SessionManager` (never builds a client — R1),
gates outbound calls through the per-source ``AsyncLimiter`` at the CALL SITE
(CLAUDE.md aiolimiter caveat — not a transport hook), and wraps each request in a
tenacity retry (exp backoff + jitter; retry transport errors / 5xx; STOP and raise
``SourceError`` on 401/403/404). Sources may also emit soft ``warn()`` warnings (D-14).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import tenacity

from .errors import SourceError

if TYPE_CHECKING:
    from aiolimiter import AsyncLimiter

    from ..handles.store import HandleStore
    from .ratelimit import RateLimiter
    from .session import SessionManager

# Permanent (non-retryable) upstream statuses — STOP, do not retry (Pattern 3).
_PERMANENT_STATUSES = (401, 403, 404)


def _is_retryable(exc: BaseException) -> bool:
    """Retry transport errors and 5xx responses; never permanent 4xx (Pattern 3).

    ``raise_for_status`` turns a 5xx into ``httpx.HTTPStatusError`` AFTER the
    permanent-4xx gate has already converted 401/403/404 into ``SourceError``, so
    any ``HTTPStatusError`` reaching here with a >=500 status is genuinely transient.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class SourceContext:
    """Framework-owned HTTP + warning context handed to a source hook."""

    def __init__(
        self,
        *,
        source_key: str,
        rate_limit_per_minute: int,
        session: SessionManager,
        ratelimiter: RateLimiter,
        handle_store: HandleStore,
    ) -> None:
        self._source_key = source_key
        self._session = session
        # Shared across search + (future) download paths (SRC-03).
        self._limiter: AsyncLimiter = ratelimiter.for_source(
            source_key, rate_limit_per_minute
        )
        self._handle_store = handle_store
        self._warnings: list[tuple[str, str]] = []

    @property
    def handle_store(self) -> HandleStore:
        """The app-scoped handle store a source mints into (HDL-01)."""
        return self._handle_store

    @property
    def warnings(self) -> list[tuple[str, str]]:
        """Soft warnings emitted by the source during this request (D-14)."""
        return self._warnings

    def warn(self, code: str, message: str) -> None:
        """Emit a soft warning alongside returned releases (D-14, partial success)."""
        self._warnings.append((code, message))

    @tenacity.retry(
        wait=tenacity.wait_exponential_jitter(initial=0.5, max=8),
        stop=tenacity.stop_after_attempt(4),
        retry=tenacity.retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        """GET ``url`` with ``params`` → parsed JSON, rate-limited + retried.

        Gates the limiter at the call site (CLAUDE.md). Raises ``SourceError`` on a
        permanent 4xx (no retry); lets transport errors / 5xx bubble to tenacity.
        """
        async with self._limiter:  # gate at CALL SITE (CLAUDE.md aiolimiter caveat)
            resp = await self._session.transport.request("GET", url, params=params)
        if resp.status_code in _PERMANENT_STATUSES:
            raise SourceError("source_unavailable", f"upstream {resp.status_code}")
        resp.raise_for_status()  # 5xx → HTTPStatusError → retried by _is_retryable
        result: dict[str, Any] = resp.json()
        return result

    @tenacity.retry(
        wait=tenacity.wait_exponential_jitter(initial=0.5, max=8),
        stop=tenacity.stop_after_attempt(4),
        retry=tenacity.retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get_bytes(self, url: str) -> bytes:
        """GET ``url`` → raw response bytes, retried like ``get_json`` but NOT limited.

        Image bytes are served by the at-home node host (``uploads.mangadex.org``),
        NOT ``api.mangadex.org``: they are not counted against the per-source API
        budget, so this deliberately does NOT acquire ``self._limiter`` (D-31 /
        Pitfall 3). The per-job ``asyncio.Semaphore`` (Plan 03) is the real ceiling.
        Raises ``SourceError`` on a permanent 4xx; lets transport errors / 5xx bubble
        to tenacity. Bytes are returned unchanged: never recompressed (PKG-04).
        """
        resp = await self._session.transport.request("GET", url)
        if resp.status_code in _PERMANENT_STATUSES:
            raise SourceError("source_unavailable", f"upstream {resp.status_code}")
        resp.raise_for_status()  # 5xx → HTTPStatusError → retried by _is_retryable
        return resp.content
