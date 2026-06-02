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
    from .session_prep import SessionPrep

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


def is_csrf_failure(resp: httpx.Response) -> bool:
    """True when ``resp`` is a stale-CSRF-token rejection (D-03, MangaBall seam).

    The httpx-form-POST analog of :func:`is_cf_challenge`: a 403 whose body carries
    a CSRF-validation marker means the gateway's held token went stale (the token
    is session-bound, no assumed TTL — RECON §"Session / CSRF bootstrap"), so the
    session-prep provider should refresh + retry ONCE before the permanent-4xx STOP.
    A plain 403 (no marker) is NOT a CSRF failure — it stays terminal (D-03: the CF
    and CSRF branches are independent; a non-marker 403 retries on neither path).

    WR-04: the marker phrasing is not yet live-verified, so match defensively —
    case-insensitive, and tolerant of escaping/casing/localized wording — by
    requiring a lowercased ``csrf`` co-occurring with ``token`` or ``validation``.
    This catches the JSON-escaped / differently-cased / whitespace-padded variants
    a lexical ``b"CSRF token validation failed"`` substring match would miss, while
    the two-term co-occurrence avoids matching unrelated 403 bodies that merely
    mention "csrf" in passing.
    """
    if resp.status_code != 403:
        return False
    body = resp.content.lower()
    return b"csrf" in body and (b"token" in body or b"validation" in body)


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
        session_prep: SessionPrep | None = None,
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
        # Phase-7 session-prep seam (D-01, default-off → MangaDex/Comix unchanged).
        # Contributes a PHPSESSID cookie + X-CSRF-Token into the SAME per-request
        # header dict the cf_clearance half builds (D-02/D-04 union path).
        self._session_prep = session_prep

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
        """Build the per-request ``headers=`` kwarg from BOTH credential seams.

        D-02/D-04 union: the cf_clearance half (D-40, browser) and the session-prep
        half (D-01, httpx CSRF) compose into ONE per-request ``headers`` dict and ONE
        ``Cookie`` header. Each half independently returns nothing when it does not
        apply, so:

        * MangaDex (``antibot="none"``, ``session_prep=None``) → ``{}`` (byte-for-byte
          unchanged);
        * Comix (``cloudflare*``, ``session_prep=None``) → the cf half only;
        * MangaBall (``antibot="none"``, ``session_prep="csrf-bootstrap"``) → the CSRF
          half only — but the UNION path runs (D-04 built now, not special-cased).

        D-40 / R1 discipline (extended to the CSRF cookie): cookies ride a single
        per-request ``Cookie`` request header, NEVER the httpx ``cookies=`` kwarg
        (deprecated in httpx 0.28, would pin credentials onto the R1-shared client's
        jar and leak them onto every other source). Both halves' cookies join into the
        SAME ``Cookie`` string with ``"; "``. The cf half also pins the EXACT UA the
        cookie was issued for (Pitfall 1 — a mismatched UA silently invalidates it).
        ``force_resolve`` forces a fresh solve (D-35) AND a session-prep refresh
        (D-03/D-05) on the retry path.
        """
        headers: dict[str, str] = {}
        cookie_parts: list[str] = []

        # cf_clearance half (D-40) — browser-issued cookie + its bound UA.
        if self._is_cloudflare:
            assert self._solver is not None  # guarded by _is_cloudflare
            clearance = await self._call_solver(force_resolve=force_resolve)
            if clearance is not None:
                headers["User-Agent"] = clearance.user_agent
                cookie_parts.extend(
                    f"{name}={value}" for name, value in clearance.cookies.items()
                )

        # session-prep half (D-01) — CSRF token header + session cookie.
        if self._session_prep is not None:
            creds = await self._call_session_prep(force_refresh=force_resolve)
            if creds is not None:
                headers["X-CSRF-Token"] = creds.csrf_token
                cookie_parts.extend(
                    f"{name}={value}" for name, value in creds.cookies.items()
                )

        if cookie_parts:
            headers["Cookie"] = "; ".join(cookie_parts)
        return {"headers": headers} if headers else {}

    async def _session_prep_active(self) -> bool:
        """True iff session-prep yields credentials for THIS source (WR-03).

        The shared ``CsrfBootstrap`` is threaded into every source's context, so
        ``self._session_prep is not None`` is true even for MangaDex/Comix. The
        provider returns ``None`` for an unconfigured key, so a non-``None``
        ``prepare(source_key)`` result is the precise signal that this source is a
        real csrf-bootstrap source — the analog of ``_is_cloudflare`` requiring a
        present solver. Called only on the CSRF-403 reconcile path; the credentials
        are cached by the provider, so this does not add a network round-trip on the
        subsequent forced refresh.
        """
        if self._session_prep is None:
            return False
        return await self._call_session_prep(force_refresh=False) is not None

    async def _call_session_prep(self, *, force_refresh: bool) -> Any:
        """Call ``prepare``, passing the internal ``force_refresh`` path if supported.

        ``force_refresh`` is an internal escalation kwarg kept OFF the ``SessionPrep``
        Protocol (mirror the antibot ``force_resolve`` discipline, D-41). A provider
        that does not accept it (``NoSessionPrep``) is called with ``source_key`` only.
        """
        assert self._session_prep is not None
        prepare = self._session_prep.prepare
        if force_refresh and "force_refresh" in inspect.signature(prepare).parameters:
            return await prepare(self._source_key, force_refresh=True)  # type: ignore[call-arg]
        return await prepare(self._source_key)

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
    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        """POST ``data`` as a form body → parsed JSON, rate-limited + retried.

        The form-POST twin of :meth:`get_json` for PHP/Laravel/Django backends whose
        API is entirely ``POST /api/v1/...`` with ``application/x-www-form-urlencoded``
        bodies (MangaBall, RECON §"Endpoint map"). httpx form-encodes ``data=dict``
        automatically. Routed through the SAME ONE shared transport, call-site limiter,
        tenacity retry, credential merge (the session-prep CSRF token + cookie ride the
        per-request headers, D-02/D-04), CSRF-403 refresh-once-and-retry (D-03), the
        permanent-4xx STOP, and ``_feed_success``/``_feed_failure`` health calls (D-36)
        as ``get_json``. Sources add ZERO networking glue (SRC-02).
        """
        try:
            body = await self._request_bytes(
                url, params=None, limited=True, method="POST", data=data
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
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> bytes:
        """Single request → optionally-decrypted body, with clearance + 403 reconcile.

        Branches BEFORE the permanent-4xx gate (Pitfall 2). Two INDEPENDENT reconcile
        branches (D-03), each forcing exactly ONE refresh + ONE retry:

        * ``cloudflare*`` sources ONLY: a challenge 403 (``is_cf_challenge``) forces one
          re-solve + retry (D-35, T-04-07);
        * session-prep sources ONLY: a CSRF-marker 403 (``is_csrf_failure``) forces one
          token refresh + retry (D-03/D-05).

        Everything else — a non-challenge/non-CSRF 403, a second 403 after a reconcile,
        or any MangaDex 403 — hits the unchanged strict 401/403/404 STOP. The forced
        retry passes ``force_resolve=True`` which refreshes WHICHEVER seam applies (the
        union in ``_clearance_kwargs``). ``method``/``data`` thread a form-POST through
        the SAME machinery as GET; httpx form-encodes ``data=dict`` as
        ``application/x-www-form-urlencoded``. Returns DECRYPTED bytes (D-39) when
        ``decrypt`` is True (the default); ``*_plain`` opts out for plaintext endpoints.
        """
        resp = await self._send(
            url,
            params=params,
            limited=limited,
            force_resolve=False,
            method=method,
            data=data,
        )
        cf_stale = (
            self._is_cloudflare and resp.status_code == 403 and is_cf_challenge(resp)
        )
        # WR-03: gate the CSRF branch on THIS source actually being a csrf-bootstrap
        # source — not merely on a shared provider being present. In app.py the one
        # shared CsrfBootstrap is threaded into EVERY source's context (incl.
        # MangaDex/Comix), so ``self._session_prep is not None`` is true for all of
        # them; gating on it alone would fire a forced refresh + retry on a
        # non-CSRF source's 403 that happens to contain the marker bytes, breaking
        # the "MangaDex/Comix byte-for-byte unchanged" invariant. ``prepare`` returns
        # ``None`` for an unconfigured key, so an active-credentials check couples the
        # branch to real csrf sources, mirroring how ``_is_cloudflare`` couples the
        # antibot level with solver presence. Evaluated ONLY inside the 403+marker
        # path so the non-CSRF happy path issues no extra prepare() call.
        csrf_stale = (
            self._session_prep is not None
            and resp.status_code == 403
            and is_csrf_failure(resp)
            and await self._session_prep_active()
        )
        if cf_stale or csrf_stale:
            # D-35 / D-03: stale credential → ONE forced refresh + single retry.
            resp = await self._send(
                url,
                params=params,
                limited=limited,
                force_resolve=True,
                method=method,
                data=data,
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
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Inject credentials (D-02/D-40) and issue ONE request (gated iff ``limited``).

        ``method`` defaults to ``"GET"`` (the unchanged search/image path). For
        ``method="POST"`` the ``data=dict`` form body is attached and httpx
        form-encodes it as ``application/x-www-form-urlencoded`` (A2).
        """
        kwargs = await self._clearance_kwargs(force_resolve=force_resolve)
        if params is not None:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        if limited:
            async with self._limiter:  # gate at CALL SITE (aiolimiter caveat)
                return await self._session.transport.request(method, url, **kwargs)
        return await self._session.transport.request(method, url, **kwargs)

    def _feed_failure(self) -> None:
        if self._source_health is not None:
            self._source_health.record_failure()

    def _feed_success(self) -> None:
        if self._source_health is not None:
            self._source_health.record_success()
