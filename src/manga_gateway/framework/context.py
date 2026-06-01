"""Per-request source context — rate-limited + retried HTTP (SRC-02/SRC-03).

The framework injects a ``SourceContext`` into a source hook. It draws the ONE
shared transport from the :class:`SessionManager` (never builds a client — R1),
gates outbound calls through the per-source ``AsyncLimiter`` at the CALL SITE
(CLAUDE.md aiolimiter caveat — not a transport hook), and wraps each request in a
tenacity retry (exp backoff + jitter; retry transport errors / 5xx; STOP and raise
``SourceError`` on 401/403/404). Sources may also emit soft ``warn()`` warnings (D-14).

Phase 4 (Comix) threads four anti-bot seams through this context WITHOUT a second
client or any change to the MangaDex path (R1):

* D-40 clearance injection — for a ``cloudflare*`` source with a present solver, the
  captured ``cf_clearance`` cookie + the EXACT UA it was issued for are injected per
  request as a single ``headers=`` override (the cookies ride a per-request ``Cookie``
  header alongside ``User-Agent`` — NOT the httpx ``cookies=`` kwarg, deprecated in
  httpx 0.28 and which would pin clearance onto the R1-shared client's jar). A
  mismatched UA silently invalidates the cookie (Pitfall 1), so the two stay coupled.
* D-35 403 reconciliation — for ``cloudflare*`` sources ONLY, a 403 carrying CF
  challenge markers triggers a single forced re-solve + one retry, branching BEFORE
  the permanent-4xx STOP gate; a non-challenge 403, a second challenge, or any
  MangaDex 403 stays terminal (Pitfall 2). Bounded: at most one re-solve (T-04-07).
* D-39 decrypt seam — a declared ``decrypt_scheme`` routes the response body through
  ``framework.decrypt`` before return; ``None`` is identity pass-through.
* D-36 health feed — a terminal failure calls ``source_health.record_failure()``; a
  clean success calls ``record_success()`` (the breaker driving dynamic ``/caps``).
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any

import httpx
import tenacity

from .decrypt import decrypt
from .errors import SourceError

if TYPE_CHECKING:
    from aiolimiter import AsyncLimiter

    from ..handles.store import HandleStore
    from ..models.caps import AntibotLevel
    from .antibot import AntiBotSolver
    from .health import SourceHealth
    from .ratelimit import RateLimiter
    from .session import SessionManager

# Permanent (non-retryable) upstream statuses — STOP, do not retry (Pattern 3).
_PERMANENT_STATUSES = (401, 403, 404)


def is_cf_challenge(resp: httpx.Response) -> bool:
    """True when ``resp`` looks like a Cloudflare interstitial challenge (D-35).

    Heuristic (RESEARCH Code Examples / A4 — exact markers pinned by the D-43 smoke):
    a 403/503 served by ``cloudflare`` carrying either the ``cf-mitigated`` header or a
    ``challenge-platform``/``cf_chl`` body marker. A plain 403 (no CF fingerprint) is
    NOT a challenge — it stays a permanent STOP (Pitfall 2).
    """
    if resp.status_code not in (403, 503):
        return False
    server = resp.headers.get("server", "").lower()
    if "cloudflare" not in server:
        return False
    if "cf-mitigated" in resp.headers:
        return True
    body = resp.content
    return b"challenge-platform" in body or b"cf_chl" in body


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
        solver: AntiBotSolver | None = None,
        antibot: AntibotLevel = "none",
        decrypt_scheme: str | None = None,
        decrypt_config: dict[str, Any] | None = None,
        source_health: SourceHealth | None = None,
    ) -> None:
        self._source_key = source_key
        self._session = session
        # Shared across search + (future) download paths (SRC-03).
        self._limiter: AsyncLimiter = ratelimiter.for_source(
            source_key, rate_limit_per_minute
        )
        self._handle_store = handle_store
        self._warnings: list[tuple[str, str]] = []
        # Phase-4 anti-bot seams (default-off so MangaDex/test sites stay minimal).
        self._solver = solver
        self._antibot = antibot
        self._decrypt_scheme = decrypt_scheme
        self._decrypt_config = decrypt_config
        self._source_health = source_health

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

    # ─────────────────────────── anti-bot helpers ───────────────────────────

    @property
    def _is_cloudflare(self) -> bool:
        """True for a ``cloudflare`` / ``cloudflare+encrypted`` source with a solver."""
        return self._antibot.startswith("cloudflare") and self._solver is not None

    async def _clearance_kwargs(self, *, force_resolve: bool) -> dict[str, Any]:
        """Resolve clearance and build the per-request ``headers=`` kwarg (header-only).

        D-40: inject ``clearance.cookies`` serialized into a single per-request
        ``Cookie`` request header, alongside the EXACT UA the cookie was issued for
        (Pitfall 1 — a mismatched UA silently invalidates the cookie, so the two stay
        coupled on the SAME request). The httpx ``cookies=`` kwarg is deliberately NOT
        used: it is deprecated in httpx 0.28 ("set cookies on the client instance") and
        the only client here is the R1-shared one — pinning ``cf_clearance`` onto its
        jar would leak it onto MangaDex + every future source and break the per-request
        UA coupling. A manual ``Cookie`` header reproduces the exact wire bytes httpx's
        ``cookies=`` emitted, and the shared jar (empty for these hosts) never
        overwrites it. Returns an empty dict when there is no solver, no cloudflare
        gate, or a ``None`` clearance — so the MangaDex path stays byte-for-byte
        unchanged.
        """
        if not self._is_cloudflare:
            return {}
        assert self._solver is not None  # guarded by _is_cloudflare
        clearance = await self._call_solver(force_resolve=force_resolve)
        if clearance is None:
            return {}
        headers = {"User-Agent": clearance.user_agent}
        cookie_header = "; ".join(
            f"{name}={value}" for name, value in clearance.cookies.items()
        )
        if cookie_header:
            headers["Cookie"] = cookie_header
        return {"headers": headers}

    async def _call_solver(self, *, force_resolve: bool) -> Any:
        """Call ``get_clearance``, passing the internal ``force_resolve`` path if the
        solver supports it.

        ``force_resolve`` is an internal escalation kwarg kept OFF the ``AntiBotSolver``
        Protocol so the public seam does not churn (D-41). A solver that does not accept
        it (the ``NoopSolver`` default) is called with ``source_key`` only.
        """
        assert self._solver is not None
        get = self._solver.get_clearance
        if force_resolve and "force_resolve" in inspect.signature(get).parameters:
            return await get(self._source_key, force_resolve=True)  # type: ignore[call-arg]
        return await get(self._source_key)

    async def _decrypt(self, body: bytes) -> bytes:
        """Route ``body`` through the framework decrypt seam (D-39; None = identity).

        Async by contract since 04-04: a registered scheme may be either sync or async,
        so the seam is awaited even though ``None`` and sync schemes pass through
        synchronously.
        """
        return await decrypt(self._decrypt_scheme, body, self._decrypt_config or {})

    # ─────────────────────────── HTTP ───────────────────────────

    @tenacity.retry(
        wait=tenacity.wait_exponential_jitter(initial=0.5, max=8),
        stop=tenacity.stop_after_attempt(4),
        retry=tenacity.retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        """GET ``url`` with ``params`` → parsed JSON, rate-limited + retried.

        Gates the limiter at the call site (CLAUDE.md). For a ``cloudflare*`` source it
        injects clearance (D-40) and reconciles a challenge 403 with a single re-solve +
        retry (D-35); a permanent 4xx raises ``SourceError`` (no retry); transport
        errors / 5xx bubble to tenacity. The plaintext body is decrypted (D-39) before
        parse, and health is fed on the terminal outcome (D-36).
        """
        try:
            body = await self._request_bytes(url, params=params, limited=True)
        except SourceError:
            self._feed_failure()
            raise
        result: dict[str, Any] = json.loads(body)
        self._feed_success()
        return result

    @tenacity.retry(
        wait=tenacity.wait_exponential_jitter(initial=0.5, max=8),
        stop=tenacity.stop_after_attempt(4),
        retry=tenacity.retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get_json_plain(self, url: str, **params: Any) -> dict[str, Any]:
        """Identical to :meth:`get_json` but BYPASSES the decrypt seam (D-39).

        Some ``cloudflare+encrypted`` sources mix plaintext + encrypted endpoints —
        Comix's ``/api/v1/manga`` (search) and ``/api/v1/manga/{hid}/chapter-indexes``
        are plaintext while ``/api/v1/manga/{hid}/chapters`` and
        ``/api/v1/chapters/{id}`` are encrypted (live recon, Plan 04-04). The source
        chooses this method per-call when the endpoint is plaintext; everything else
        (clearance injection, rate limit, retry, 403 reconciliation, health feed)
        stays identical to ``get_json``.
        """
        try:
            body = await self._request_bytes(
                url, params=params, limited=True, decrypt=False
            )
        except SourceError:
            self._feed_failure()
            raise
        result: dict[str, Any] = json.loads(body)
        self._feed_success()
        return result

    @tenacity.retry(
        wait=tenacity.wait_exponential_jitter(initial=0.5, max=8),
        stop=tenacity.stop_after_attempt(4),
        retry=tenacity.retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get_bytes(self, url: str) -> bytes:
        """GET ``url`` → decrypted response bytes, retried like ``get_json`` but NOT
        rate-limited.

        Image bytes are served by the at-home node host (``uploads.mangadex.org``),
        NOT ``api.mangadex.org``: they are not counted against the per-source API
        budget, so this deliberately does NOT acquire ``self._limiter`` (D-31 /
        Pitfall 3). The per-job ``asyncio.Semaphore`` (Plan 03) is the real ceiling.
        Raises ``SourceError`` on a permanent 4xx; lets transport errors / 5xx bubble
        to tenacity. Bytes are decrypted (D-39) but never recompressed (PKG-04).
        """
        try:
            body = await self._request_bytes(url, params=None, limited=False)
        except SourceError:
            self._feed_failure()
            raise
        self._feed_success()
        return body

    @tenacity.retry(
        wait=tenacity.wait_exponential_jitter(initial=0.5, max=8),
        stop=tenacity.stop_after_attempt(4),
        retry=tenacity.retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get_bytes_plain(self, url: str) -> bytes:
        """Identical to :meth:`get_bytes` but BYPASSES the decrypt seam (D-39).

        Mirrors :meth:`get_json_plain` for the bytes path: some
        ``cloudflare+encrypted`` sources have plaintext CDN payloads even though
        their API responses are encrypted (Comix: ``/api/v1/chapters/{id}`` is
        encrypted but the resolved ``https://{cdn}.store/si/{token}/{NN}.webp``
        CDN serves plaintext WebP). Without this opt-out, routing a binary blob
        through the source's registered ``decrypt_scheme`` would corrupt the
        bytes. Everything else (clearance injection, retry, 403 reconciliation,
        health feed) stays identical to :meth:`get_bytes`.
        """
        try:
            body = await self._request_bytes(
                url, params=None, limited=False, decrypt=False
            )
        except SourceError:
            self._feed_failure()
            raise
        self._feed_success()
        return body

    async def _request_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        limited: bool,
        decrypt: bool = True,
    ) -> bytes:
        """Single GET → optionally-decrypted body, with clearance + 403 reconciliation.

        Branches BEFORE the permanent-4xx gate (Pitfall 2): for ``cloudflare*`` sources
        ONLY, a challenge 403 forces ONE re-solve + retry; everything else (a
        non-challenge 403, a second challenge after re-solve, or any MangaDex 403) hits
        the unchanged strict 401/403/404 STOP. Returns the DECRYPTED bytes (D-39) when
        ``decrypt`` is True (the default); ``get_json_plain`` opts out for the source's
        plaintext endpoints.
        """
        resp = await self._send(
            url, params=params, limited=limited, force_resolve=False
        )
        if self._is_cloudflare and resp.status_code == 403 and is_cf_challenge(resp):
            # D-35: stale clearance → ONE forced re-solve + single retry (T-04-07).
            resp = await self._send(
                url, params=params, limited=limited, force_resolve=True
            )
        if resp.status_code in _PERMANENT_STATUSES:
            raise SourceError(
                "source_unavailable",
                f"upstream {resp.status_code}",
                status=resp.status_code,
            )
        resp.raise_for_status()  # 5xx → HTTPStatusError → retried by _is_retryable
        if not decrypt:
            return resp.content
        return await self._decrypt(resp.content)

    async def _send(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        limited: bool,
        force_resolve: bool,
    ) -> httpx.Response:
        """Inject clearance (D-40) and issue ONE GET (gated iff ``limited``)."""
        kwargs = await self._clearance_kwargs(force_resolve=force_resolve)
        if params is not None:
            kwargs["params"] = params
        if limited:
            async with self._limiter:  # gate at CALL SITE (aiolimiter caveat)
                return await self._session.transport.request("GET", url, **kwargs)
        return await self._session.transport.request("GET", url, **kwargs)

    def _feed_failure(self) -> None:
        if self._source_health is not None:
            self._source_health.record_failure()

    def _feed_success(self) -> None:
        if self._source_health is not None:
            self._source_health.record_success()
