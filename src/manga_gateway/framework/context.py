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

    from .ratelimit import RateLimiter
    from .session import SessionManager

# Permanent (non-retryable) upstream statuses — STOP, do not retry (Pattern 3).
_PERMANENT_STATUSES = (401, 403, 404)


class SourceContext:
    """Framework-owned HTTP + warning context handed to a source hook."""

    def __init__(
        self,
        *,
        source_key: str,
        rate_limit_per_minute: int,
        session: SessionManager,
        ratelimiter: RateLimiter,
    ) -> None:
        self._source_key = source_key
        self._session = session
        # Shared across search + (future) download paths (SRC-03).
        self._limiter: AsyncLimiter = ratelimiter.for_source(
            source_key, rate_limit_per_minute
        )
        self._warnings: list[tuple[str, str]] = []

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
        retry=tenacity.retry_if_exception_type(httpx.TransportError),
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
        resp.raise_for_status()  # 5xx → httpx.HTTPStatusError; tenacity won't retry it
        result: dict[str, Any] = resp.json()
        return result
