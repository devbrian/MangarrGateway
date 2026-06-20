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

import asyncio
import inspect
import json
import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

import httpx
import tenacity

from ..metrics.collector import get_collector
from .decrypt import decrypt
from .errors import SourceError

_log = logging.getLogger("manga_gateway")

# 260620-4im: the in-flight image proxy is TASK-LOCAL, not a shared ``ctx`` field.
# One ``SourceContext`` is shared by up to ``image_fetch_concurrency`` concurrent
# page tasks (the engine runs ``_one`` under an ``asyncio.TaskGroup``), so a plain
# ``self._active_proxy`` would let parallel ``fetch_image_via_pool`` calls clobber
# each other — a page could egress through a sibling's proxy, or through the DIRECT
# (banned) transport after a sibling's ``finally`` cleared it. A ``ContextVar`` is
# copied per task at ``create_task`` time, so each page's set/reset stays isolated
# to its own await chain (incl. the inner ``_send_emitting`` read). Module-level
# (NOT per instance) so frequent job contexts don't leak ContextVars.
# ``_sticky_proxy`` stays a plain ``ctx`` field — per-JOB shared state, by design.
_ACTIVE_IMAGE_PROXY: ContextVar[PooledProxy | None] = ContextVar(
    "active_image_proxy", default=None
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiolimiter import AsyncLimiter

    from ..handles.store import HandleStore
    from ..models.caps import AntibotLevel
    from ..models.search import ExternalLinks
    from .antibot import AntiBotSolver
    from .enum_cache import CacheKey, Enumeration, EnumerationCache
    from .health import SourceHealth
    from .proxy_pool import PooledProxy, ProxyPool
    from .ratelimit import RateLimiter
    from .session import SessionManager
    from .session_prep import SessionPrep

# Permanent (non-retryable) upstream statuses — STOP, do not retry (Pattern 3).
_PERMANENT_STATUSES = (401, 403, 404)

# Phase-13 best-effort budget for external-tracker link resolution (D-03). 5.0s
# comfortably covers a normal detail GET + one short tenacity backoff while leaving
# ~25s of the 30s per-source fan-out budget for the (load-bearing) chapter
# enumeration; kept under the tenacity ``max=8`` backoff so a hung link fetch can
# never become the binding cost. A slow/hung fetch trips ``asyncio.timeout`` and is
# swallowed to ``None`` (chapters return unaffected, R6).
_EXTERNAL_LINKS_TIMEOUT_S = 5.0

# limiter-wait metric threshold (Open Question 2): only emit a ``limiter-wait``
# event when the acquire actually blocked longer than this — a token-bucket
# acquire that returns immediately (~0ms) would otherwise flood the store.
_LIMITER_WAIT_EMIT_THRESHOLD_MS = 5.0


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


def is_waf_block(resp: httpx.Response) -> bool:
    """True when ``resp`` is a WAF "malicious payload" false-positive block (D-03).

    The query-rewrite sibling of :func:`is_cf_challenge` / :func:`is_csrf_failure`:
    mangaball.net sits behind a WAF that returns a 403 carrying
    ``{"error":"Malicious payload detected", … "code":403}`` for ANY search POST whose
    ``search_input`` contains a SQL-injection-flavoured token (the literal word
    "System" is the one live-confirmed trigger). The word is extremely common in
    manhwa titles, so the block is a false positive the source can recover from by
    stripping the trigger token and retrying — hence a distinct, catchable signal
    rather than a terminal ``source_unavailable``.

    Cheap and defensive: the status guard fires FIRST (a 200 carrying the marker words
    or any 5xx is NOT a WAF block), then a literal ``b"Malicious payload"`` substring
    match against the raw body (no JSON parse — tolerant of body/escaping variations).
    A plain 403 (no marker) and a CSRF-failure 403 both return False, so they stay on
    their existing terminal/CSRF-reconcile paths untouched.

    mangaball-scoped: it is the ONLY source that returns the ``Malicious payload``
    body, so no other source can ever trigger this predicate.
    """
    if resp.status_code != 403:
        return False
    return b"Malicious payload" in resp.content


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
        cloudflare_challenge_optional: bool = False,
        decrypt_scheme: str | None = None,
        decrypt_config: dict[str, Any] | None = None,
        source_health: SourceHealth | None = None,
        session_prep: SessionPrep | None = None,
        expected_pages: int | None = None,
        candidates_enumerated: int | None = None,
        enum_cache: EnumerationCache | None = None,
        retry_attempts: int = 4,
        use_download_transport: bool = False,
        proxy_pool: ProxyPool | None = None,
        image_via_proxy_pool: bool = False,
        image_proxy_max_attempts: int = 3,
    ) -> None:
        self._source_key = source_key
        self._session = session
        # debug pool-starves-search-cooldown (2026-06-17): the DOWNLOAD path
        # (engine._build_context passes use_download_transport=True) routes its
        # manifest + image fetches through session.download_transport — a SEPARATE
        # connection pool — so a download backlog can never exhaust the pool the
        # search fan-out needs. The search/recent path leaves this False and uses
        # session.transport. Default False keeps every existing caller on the search
        # transport (which itself falls back to the single shared pool when no
        # distinct download transport was wired into the SessionManager).
        self._use_download_transport = use_download_transport
        # 260606-lyb Change 1: per-context tenacity attempt budget. A class-level
        # ``@tenacity.retry`` decorator is evaluated ONCE at import and cannot read
        # ``self``, so each HTTP method drives its retryer from this value at call
        # time (see ``_retrying``). Default 4 preserves the historic budget for the
        # download/job contexts and EVERY caller that does not pass it; the SEARCH
        # path (search.py / recent.py) builds contexts with ``retry_attempts=2`` so
        # a down source fails fast on a repeat search (1 retry, not 3).
        self._retry_attempts = retry_attempts
        # Shared across search + (future) download paths (SRC-03).
        self._limiter: AsyncLimiter = ratelimiter.for_source(
            source_key, rate_limit_per_minute
        )
        self._handle_store = handle_store
        self._warnings: list[tuple[str, str]] = []
        # Phase-4 anti-bot seams (default-off so MangaDex/test sites stay minimal).
        self._solver = solver
        self._antibot = antibot
        # On-demand clearance (debug pooltimeout-recurrence): when True, the cf half
        # of ``_clearance_kwargs`` attaches only ALREADY-HELD clearance on the first
        # attempt (``solve_if_missing=False``) and defers a fresh solve to the
        # challenge-triggered D-35 reconcile, so a source with an intermittent CF
        # challenge self-heals either way. Default False keeps every eager
        # ``cloudflare*`` source unchanged.
        self._cloudflare_challenge_optional = cloudflare_challenge_optional
        self._decrypt_scheme = decrypt_scheme
        self._decrypt_config = decrypt_config
        self._source_health = source_health
        # Phase-7 session-prep seam (D-01, default-off → MangaDex/Comix unchanged).
        # Contributes a PHPSESSID cookie + X-CSRF-Token into the SAME per-request
        # header dict the cf_clearance half builds (D-02/D-04 union path).
        self._session_prep = session_prep
        # #83/IN-03 manifest-integrity hint. On the DOWNLOAD path the engine builds
        # this context with ``expected_pages`` = the resolved record's declared page
        # count, so a source's ``fetch_manifest`` can assert the extracted manifest
        # length matches what search declared. ``None`` on the SEARCH path (search
        # never resolves a manifest) and for sources that do not opt in — a source
        # reads ``ctx.expected_pages`` only if it wants the check (MangaBall does).
        self.expected_pages = expected_pages
        # 260605-e9a deliverable 5: a title-search source reports how many of its
        # ≤5 title candidates it actually deep-enumerated (fanned a chapter list
        # out for). The route reads ``ctx.candidates_enumerated`` after ``search``
        # and threads it onto the per-source ``source-result`` metric event.
        # ``None`` for sources that do not deep-enumerate.
        self.candidates_enumerated = candidates_enumerated
        # Phase-13 per-request external-links scratch map (D-01/D-02). A FREE source
        # (Comix/MangaDex/Atsumaru) stashes the raw per-series tracker-link dict it
        # already fetched during ``search`` here, keyed by series id, so its
        # zero-HTTP ``fetch_external_links`` reads it back with no second fetch.
        # ``ctx`` is per-request (search.py builds a fresh ctx per source), so this
        # mirrors the per-request public ``expected_pages``/``candidates_enumerated``
        # attrs above — it is scratch state, never cross-request shared.
        self.external_links_raw: dict[str, dict[str, Any]] = {}
        # Phase-9 enumeration-cache seam (CACHE-01, default-off → MangaDex/Comix and
        # every non-app unit test stay byte-for-byte unchanged, matching the
        # solver/session_prep default-None seams). ONE process-wide EnumerationCache
        # is threaded in by the POST /search route; the recent + download paths leave
        # it None (recent is never cached, download resolves one handle — CACHE-05).
        self._enum_cache = enum_cache
        # 260620-4im image proxy pool seam (default-off → every existing caller AND the
        # search path byte-for-byte unchanged). ``_proxy_pool`` is the shared pool;
        # ``_image_via_proxy_pool`` is THIS source's opt-in flag (only an opted-in
        # download ctx routes ``fetch_image`` through the pool). The sticky/active proxy
        # live HERE because ``ctx`` is per-job (engine._build_context), mirroring the
        # per-job ``_PREFERRED_ZONE_ATTR`` sidecar — never cross-job shared.
        self._proxy_pool = proxy_pool
        self._image_via_proxy_pool = image_via_proxy_pool
        self._image_proxy_max_attempts = image_proxy_max_attempts
        # The job's sticky proxy (reused across pages) — per-job shared state, correct
        # to live on the (per-job) ctx. The proxy currently IN FLIGHT is NOT here: it is
        # the module-level ``_ACTIVE_IMAGE_PROXY`` ContextVar so concurrent page tasks
        # sharing this ctx don't clobber each other's routing (see the note above).
        self._sticky_proxy: PooledProxy | None = None

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
            # On-demand clearance: for an intermittent-challenge source on the FIRST
            # attempt (not a forced D-35 reconcile), peek the held clearance only —
            # never block on a fresh solve. If no challenge fires, the request just
            # succeeds with no solve; if one does, ``_request_response`` detects it
            # (``is_cf_challenge``) and retries with ``force_resolve=True``, which
            # drives a real solve through the ``else`` branch below. Eager sources
            # (``cloudflare_challenge_optional=False``) keep ``solve_if_missing=True``.
            solve_if_missing = not (
                self._cloudflare_challenge_optional and not force_resolve
            )
            clearance = await self._call_solver(
                force_resolve=force_resolve, solve_if_missing=solve_if_missing
            )
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

    async def _call_solver(
        self, *, force_resolve: bool, solve_if_missing: bool = True
    ) -> Any:
        """Call ``get_clearance``, passing the internal escalation kwargs the solver
        supports.

        ``force_resolve`` (D-35 re-solve) and ``solve_if_missing`` (on-demand peek —
        ``False`` means "serve only already-held clearance, do NOT block on a fresh
        solve") are internal kwargs kept OFF the ``AntiBotSolver`` Protocol so the
        public seam does not churn (D-41). Each is forwarded ONLY when the chosen
        solver declares it, via ``inspect.signature`` — a solver that accepts neither
        (the ``NoopSolver`` default) is called with ``source_key`` only.
        ``solve_if_missing=True`` (the default) is never forwarded, so eager callers
        stay byte-for-byte unchanged.
        """
        assert self._solver is not None
        get = self._solver.get_clearance
        params = inspect.signature(get).parameters
        kwargs: dict[str, Any] = {}
        if force_resolve and "force_resolve" in params:
            kwargs["force_resolve"] = True
        if not solve_if_missing and "solve_if_missing" in params:
            kwargs["solve_if_missing"] = False
        return await get(self._source_key, **kwargs)

    async def _decrypt(self, body: bytes) -> bytes:
        """Route ``body`` through the framework decrypt seam (D-39; None = identity).

        Async by contract since 04-04: a registered scheme may be either sync or async,
        so the seam is awaited even though ``None`` and sync schemes pass through
        synchronously.
        """
        return await decrypt(self._decrypt_scheme, body, self._decrypt_config or {})

    @staticmethod
    def _parse_json_object(body: bytes) -> dict[str, Any]:
        """Parse ``body`` to a JSON object, raising a typed error on a non-object.

        IN-01: the ``get_json``/``post_json``/``get_json_plain`` return type is
        ``dict[str, Any]``, but ``json.loads`` of a top-level array/scalar body
        would satisfy that annotation at runtime and only blow up later as an
        ``AttributeError`` inside ``body.get(...)``. Validate the decoded type here
        so a malformed envelope surfaces as a typed ``SourceError`` instead.
        """
        result = json.loads(body)
        if not isinstance(result, dict):
            raise SourceError(
                "source_unavailable",
                f"non-object JSON body (got {type(result).__name__})",
            )
        return result

    @staticmethod
    def _parse_json_array(body: bytes) -> list[Any]:
        """Parse ``body`` to a top-level JSON array, raising on a non-array.

        The list-bodied twin of :meth:`_parse_json_object` (IN-01): the
        ``get_json_array`` return type is ``list[Any]``, but ``json.loads`` of a
        top-level object/scalar body would satisfy that annotation at runtime and
        only blow up later as a ``TypeError`` when the source iterates the rows.
        Validate the decoded type here so a malformed body surfaces as a typed
        ``SourceError`` instead. Used for bare-array endpoints like mangadot's
        ``GET /api/manga/{id}/chapters/list`` which return ``[{…}, …]`` rather
        than the ``{data:[…]}`` envelope every other JSON method expects.
        """
        result = json.loads(body)
        if not isinstance(result, list):
            raise SourceError(
                "source_unavailable",
                f"non-array JSON body (got {type(result).__name__})",
            )
        return result

    # ─────────────────────────── enumeration cache ───────────────────────────

    async def cached_enumerate(
        self, key: CacheKey, fetch_fn: Callable[[], Awaitable[Enumeration]]
    ) -> Enumeration:
        """Layer-2 chapter-feed enumeration through the injected cache (CACHE-01/03).

        A thin pass-through: with ``enum_cache=None`` (the default seam) this is a bare
        ``await fetch_fn()`` — byte-for-byte the pre-seam path, no caching. With a cache
        injected a HIT returns the cached :class:`Enumeration` WITHOUT re-running
        ``fetch_fn`` (and therefore without acquiring ``self._limiter`` — the limiter
        lives inside ``fetch_fn``, one level deeper than the cache check, so a HIT costs
        no upstream call and no token, CACHE-03).
        """
        if self._enum_cache is None:
            return await fetch_fn()
        return await self._enum_cache.cached_enumerate(key, fetch_fn)

    async def cached_resolve(
        self, key: CacheKey, fetch_fn: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Layer-1 title/query → series-id resolution through the cache (D-01).

        Same HIT / miss / ``None``-default semantics as :meth:`cached_enumerate`; the
        limiter stays inside ``fetch_fn`` (CACHE-03).
        """
        if self._enum_cache is None:
            return await fetch_fn()
        return await self._enum_cache.cached_resolve(key, fetch_fn)

    async def resolve_external_links(
        self,
        series_id: str,
        parse_fn: Callable[[], Awaitable[ExternalLinks | None]],
    ) -> ExternalLinks | None:
        """Best-effort, resolve-once-per-``(sourceKey, seriesId)`` tracker links
        (D-01/D-03).

        The single framework owner of the best-effort contract so sources stay
        parse-only (SRC-01/02): ``parse_fn`` (the source's ``fetch_external_links``)
        runs inside an :func:`asyncio.timeout` bound at
        :data:`_EXTERNAL_LINKS_TIMEOUT_S`, and ALL exceptions — including
        ``TimeoutError`` — are swallowed to ``None`` so a slow/hung/failing link fetch
        never fails or stalls the search (chapter releases return unaffected, R6).

        Mirrors :meth:`cached_resolve`: keyed on ``(sourceKey, "extlinks:"+seriesId)``
        via :meth:`cached_resolve_key`, so a repeat search for the same series is a
        HIT with no re-parse/no HTTP/no token (R4). With ``enum_cache=None`` (the
        default seam) ``cached_resolve`` is the bare pass-through, so the wrapper still
        applies the timeout + swallow even with no cache wired.
        """

        async def _guarded() -> ExternalLinks | None:
            try:
                async with asyncio.timeout(_EXTERNAL_LINKS_TIMEOUT_S):
                    return await parse_fn()
            except Exception:  # noqa: BLE001 — best-effort: links are advisory (D-03)
                return None

        key = self.cached_resolve_key(f"extlinks:{series_id}", [])
        return cast("ExternalLinks | None", await self.cached_resolve(key, _guarded))

    def cache_replace(self, key: CacheKey, enum: Enumeration) -> None:
        """Overwrite the Layer-2 window after a completeness-driven deeper refetch
        (CACHE-04). A no-op when ``enum_cache`` is absent/disabled."""
        if self._enum_cache is None:
            return
        self._enum_cache.cache_replace(key, enum)

    def cached_enumerate_key(
        self, series_id: str, languages: list[str], *, extra: Any = None
    ) -> CacheKey:
        """Build the canonical Layer-2 cache key for THIS source (T-09-01).

        Delegates to :meth:`EnumerationCache.enum_key` so the source cannot
        accidentally key on ``type``/``chapter`` — only
        ``(source_key, series_id, sorted(languages)[, extra])``. ``extra`` carries
        a source-supplied discriminator (e.g. MangaDex's ``(stop_floor, offset)``
        window, CR-01); language-agnostic sources pass ``languages=[]`` (IN-02).
        """
        from .enum_cache import EnumerationCache  # noqa: PLC0415

        return EnumerationCache.enum_key(
            self._source_key, series_id, languages, extra=extra
        )

    def cached_resolve_key(
        self, normalized_query: str, languages: list[str], *, extra: Any = None
    ) -> CacheKey:
        """Build the canonical Layer-1 cache key for THIS source (D-01/T-09-01).

        ``extra`` carries a source-supplied discriminator (e.g. the interactive
        candidate count on MangaDex/Comix, WR-01) appended to the key tuple."""
        from .enum_cache import EnumerationCache  # noqa: PLC0415

        return EnumerationCache.resolve_key(
            self._source_key, normalized_query, languages, extra=extra
        )

    # ─────────────────────────── HTTP ───────────────────────────

    def _retrying(self) -> tenacity.AsyncRetrying:
        """A FRESH tenacity retryer per call, honoring the per-context attempt budget.

        260606-lyb Change 1: the retry POLICY is byte-for-byte the historic
        class-level decorator (exp backoff + jitter; retry transport errors / 5xx via
        :func:`_is_retryable`; reraise the final exception) — the ONLY change is
        ``stop_after_attempt(self._retry_attempts)`` reading the per-instance budget
        instead of the hardcoded ``4``. A decorator is import-time and cannot read
        ``self``, so each HTTP method builds a retryer here and drives its body closure
        through it. A new instance per call keeps tenacity's internal attempt state
        un-shared across concurrent requests.
        """
        return tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential_jitter(initial=0.5, max=8),
            stop=tenacity.stop_after_attempt(self._retry_attempts),
            retry=tenacity.retry_if_exception(_is_retryable),
            reraise=True,
        )

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        """GET ``url`` with ``params`` → parsed JSON, rate-limited + retried.

        Gates the limiter at the call site (CLAUDE.md). For a ``cloudflare*`` source it
        injects clearance (D-40) and reconciles a challenge 403 with a single re-solve +
        retry (D-35); a permanent 4xx raises ``SourceError`` (no retry); transport
        errors / 5xx bubble to tenacity. The plaintext body is decrypted (D-39) before
        parse, and health is fed on the terminal outcome (D-36) — the parse runs INSIDE
        the failure-feeding ``try`` so a malformed-shape ``200`` (a non-object body →
        ``SourceError`` from :meth:`_parse_json_object`) also trips ``_feed_failure``.
        """

        async def _attempt() -> dict[str, Any]:
            try:
                body = await self._request_bytes(
                    url, params=params, limited=True, op="get_json"
                )
                result = self._parse_json_object(body)
            except SourceError:
                self._feed_failure()
                raise
            self._feed_success()
            return result

        return await self._retrying()(_attempt)

    async def get_json_array(self, url: str, **params: Any) -> list[Any]:
        """GET ``url`` with ``params`` → parsed top-level JSON ARRAY, like
        :meth:`get_json` but parsing a bare list body rather than an object.

        Some endpoints return a bare top-level JSON array (mangadot's
        ``GET /api/manga/{id}/chapters/list`` → ``[{…}, …]``) which
        :meth:`_parse_json_object` rejects. This method is byte-for-byte the
        ``get_json`` transport path — the SAME call-site rate limit
        (``_request_bytes(..., limited=True)``), clearance injection (D-40),
        challenge-403 single re-solve + retry (D-35), tenacity retry, decrypt
        seam (D-39; identity for plaintext sources), and health feed (D-36) —
        differing ONLY in the final parse (:meth:`_parse_json_array`, which
        raises ``SourceError`` on a non-array body). The parse runs INSIDE the
        failure-feeding ``try`` (byte-for-byte parity with :meth:`get_json`) so a
        non-array ``200`` body also trips ``_feed_failure``. Additive: the existing
        object-bodied call paths (comix/mangadex/mangaball) are untouched.
        """

        async def _attempt() -> list[Any]:
            try:
                body = await self._request_bytes(
                    url, params=params, limited=True, op="get_json_array"
                )
                result = self._parse_json_array(body)
            except SourceError:
                self._feed_failure()
                raise
            self._feed_success()
            return result

        return await self._retrying()(_attempt)

    async def post_json(self, url: str, *, data: dict[str, Any]) -> dict[str, Any]:
        """POST ``data`` as a form body → parsed JSON, rate-limited + retried.

        The form-POST twin of :meth:`get_json` for PHP/Laravel/Django backends whose
        API is entirely ``POST /api/v1/...`` with ``application/x-www-form-urlencoded``
        bodies (MangaBall, RECON §"Endpoint map"). httpx form-encodes ``data=dict``
        automatically. Routed through the SAME ONE shared transport, call-site limiter,
        tenacity retry, credential merge (the session-prep CSRF token + cookie ride the
        per-request headers, D-02/D-04), CSRF-403 refresh-once-and-retry (D-03), the
        permanent-4xx STOP, and ``_feed_success``/``_feed_failure`` health calls (D-36)
        as ``get_json`` — including the parse running INSIDE the failure-feeding
        ``try`` so a non-object ``200`` body also trips ``_feed_failure``. Sources add
        ZERO networking glue (SRC-02).
        """

        async def _attempt() -> dict[str, Any]:
            try:
                body = await self._request_bytes(
                    url,
                    params=None,
                    limited=True,
                    method="POST",
                    data=data,
                    op="post_json",
                )
                result = self._parse_json_object(body)
            except SourceError as exc:
                # 260620-5yq: a ``waf_blocked`` is a recoverable soft signal the source
                # absorbs (sanitize-and-retry), so it records NO source-health failure —
                # every other code feeds health byte-for-byte as before. Always re-raise
                # so the source still sees the signal (tenacity does not retry a
                # SourceError, so it surfaces after exactly ONE transport call).
                if exc.code != "waf_blocked":
                    self._feed_failure()
                raise
            self._feed_success()
            return result

        return await self._retrying()(_attempt)

    async def post_json_body(
        self,
        url: str,
        *,
        body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST a JSON request body → parsed JSON object, rate-limited + retried.

        The JSON-body twin of :meth:`post_json` (which form-encodes
        ``application/x-www-form-urlencoded``). Some modern JSON-REST backends require
        ``Content-Type: application/json`` request bodies AND query params on the same
        POST, and a per-call custom header — Kagane's Komga-derived ``/api/v2`` API is
        the first: ``POST /api/v2/search/series?page=&size=&sort=`` with a JSON body,
        and the download-path book-token POST needs an ``x-integrity-token`` header
        (live recon 2026-06-09). ``body`` is sent via httpx ``json=`` (httpx sets the
        ``application/json`` content type); ``params`` rides the query string;
        ``headers`` are merged ON TOP of the framework's credential headers (D-02/D-40)
        so a per-call header (e.g. ``x-integrity-token``) composes with the injected
        ``cf_clearance`` cookie + UA without the source touching the transport.

        Routed through the SAME ONE shared transport, call-site limiter, tenacity retry,
        clearance injection + challenge-403 single re-solve (D-35), the permanent-4xx
        STOP, the decrypt seam (D-39; identity for plaintext sources), and the
        ``_feed_success``/``_feed_failure`` health calls (D-36) as ``get_json`` —
        including the parse running INSIDE the failure-feeding ``try`` so a non-object
        ``200`` body also trips ``_feed_failure``. Sources add ZERO networking glue
        (SRC-02): clearance, rate-limit, retry, and health all stay in the framework.
        """

        async def _attempt() -> dict[str, Any]:
            try:
                raw = await self._request_bytes(
                    url,
                    params=params,
                    limited=True,
                    method="POST",
                    json_body=body,
                    extra_headers=headers,
                    op="post_json_body",
                )
                result = self._parse_json_object(raw)
            except SourceError:
                self._feed_failure()
                raise
            self._feed_success()
            return result

        return await self._retrying()(_attempt)

    async def get_json_plain(self, url: str, **params: Any) -> dict[str, Any]:
        """Identical to :meth:`get_json` but BYPASSES the decrypt seam (D-39).

        Some ``cloudflare+encrypted`` sources mix plaintext + encrypted endpoints —
        Comix's ``/api/v1/manga`` (search) and ``/api/v1/manga/{hid}/chapter-indexes``
        are plaintext while ``/api/v1/manga/{hid}/chapters`` and
        ``/api/v1/chapters/{id}`` are encrypted (live recon, Plan 04-04). The source
        chooses this method per-call when the endpoint is plaintext; everything else
        (clearance injection, rate limit, retry, 403 reconciliation, health feed —
        including the parse running INSIDE the failure-feeding ``try`` so a non-object
        ``200`` body also trips ``_feed_failure``) stays identical to ``get_json``.
        """

        async def _attempt() -> dict[str, Any]:
            try:
                body = await self._request_bytes(
                    url, params=params, limited=True, decrypt=False, op="get_json_plain"
                )
                result = self._parse_json_object(body)
            except SourceError:
                self._feed_failure()
                raise
            self._feed_success()
            return result

        return await self._retrying()(_attempt)

    async def get_bytes(self, url: str, *, limited: bool = False) -> bytes:
        """GET ``url`` → decrypted response bytes, retried like ``get_json``.

        Image bytes are served by the at-home node host (``uploads.mangadex.org``),
        NOT ``api.mangadex.org``: they are not counted against the per-source API
        budget, so by default this does NOT acquire ``self._limiter`` (D-31 /
        Pitfall 3). The per-job ``asyncio.Semaphore`` (Plan 03) is the real ceiling
        for image fetch. Callers that issue API/page GETs which DO count against the
        per-source budget (e.g. WeebCentral's metadata detail GET, WR-03) pass
        ``limited=True`` so the call is gated by the same per-minute rate limiter as
        the source's other traffic. Raises ``SourceError`` on a permanent 4xx; lets
        transport errors / 5xx bubble to tenacity. Bytes are decrypted (D-39) but
        never recompressed (PKG-04).
        """

        async def _attempt() -> bytes:
            try:
                body = await self._request_bytes(
                    url, params=None, limited=limited, op="get_bytes"
                )
            except SourceError:
                self._feed_failure()
                raise
            self._feed_success()
            return body

        return await self._retrying()(_attempt)

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

        async def _attempt() -> bytes:
            try:
                body = await self._request_bytes(
                    url, params=None, limited=False, decrypt=False, op="get_bytes_plain"
                )
            except SourceError:
                self._feed_failure()
                raise
            self._feed_success()
            return body

        return await self._retrying()(_attempt)

    async def get_bytes_plain_with_headers(
        self, url: str
    ) -> tuple[bytes, httpx.Headers]:
        """Source-agnostic header-surfacing twin of :meth:`get_bytes_plain`.

        For sources that run a per-source POST-fetch decode keyed by a RESPONSE
        header, the framework must surface the headers alongside the bytes — but the
        decode itself lives entirely in the source, so the framework stays agnostic of
        any header name. Returns ``(resp.content, resp.headers)``: the raw fetched bytes
        plus the (case-insensitive) ``httpx.Headers``.

        Everything else mirrors :meth:`get_bytes_plain` exactly — the same ``_retrying``
        wrapper, the same ``_feed_failure``/``_feed_success`` health feed, NOT
        rate-limited (``limited=False``), and the SAME ``"get_bytes_plain"`` op label so
        metrics rollups for image fetch do not fork. The framework decrypt seam stays
        opted out (D-39): ``self._decrypt`` is NEVER run on this path, identical to
        :meth:`get_bytes_plain`.
        """

        async def _attempt() -> tuple[bytes, httpx.Headers]:
            try:
                resp = await self._request_response(
                    url, params=None, limited=False, op="get_bytes_plain"
                )
            except SourceError:
                self._feed_failure()
                raise
            self._feed_success()
            return resp.content, resp.headers

        return await self._retrying()(_attempt)

    async def fetch_image_via_pool(
        self, fetch: Callable[[], Awaitable[bytes]]
    ) -> bytes:
        """Run ``fetch`` through the proxy pool (sticky + rotate + cooldown).

        The framework wrapper the engine puts around an opted-in source's
        ``fetch_image`` (260620-4im). When no pool is wired OR this source did not opt
        in (``image_fetch_via_proxy_pool``) — and ALWAYS on the search path, which is
        never given a pool — this is a transparent ``await fetch()``: ``_active_proxy``
        is never set, so transport routing stays byte-for-byte today (the regression
        contract).

        With a pool active it orchestrates, per call: pick the job's sticky proxy (or
        ``acquire`` a fresh one), set ``_active_proxy`` so every inner ``ctx.get_bytes``
        (incl. the source's per-page zone-retry) egresses through it, and run ``fetch``.
        On a fetch failure (``SourceError`` incl. upstream 403, or any
        ``httpx.HTTPError`` — proxy 407 / ConnectTimeout / connect error) the proxy is
        ``mark_failed``'d and a DIFFERENT proxy is acquired (excluding the ones already
        tried THIS page), up to ``image_proxy_max_attempts`` total; the first success
        becomes the new sticky. All attempts failing re-raises the LAST failure
        unchanged (the terminal image-fetch surface the engine already understands); no
        proxy available at all raises a clear terminal
        ``SourceError("source_unavailable", …)`` (T-4im-03). Logs the chosen proxy
        identity (host:port) only — NEVER creds (T-4im-01).
        """
        if self._proxy_pool is None or not self._image_via_proxy_pool:
            return await fetch()  # regression-safe passthrough (non-opted / search)

        tried: set[str] = set()
        last_exc: BaseException | None = None
        for _ in range(self._image_proxy_max_attempts):
            # Reuse the sticky proxy unless it was already tried-and-failed THIS page.
            sticky = self._sticky_proxy
            if sticky is not None and sticky.selection_key not in tried:
                proxy: PooledProxy | None = sticky
            else:
                proxy = self._proxy_pool.acquire(exclude=tried)
            if proxy is None:
                break  # every proxy excluded / on cooldown → terminal below
            tried.add(proxy.selection_key)
            # Set the in-flight proxy task-locally so concurrent page tasks sharing
            # this ctx route independently; ``reset`` in ``finally`` restores it.
            token = _ACTIVE_IMAGE_PROXY.set(proxy)
            try:
                result = await fetch()
            except (SourceError, httpx.HTTPError) as exc:
                last_exc = exc
                self._proxy_pool.mark_failed(proxy)
                if (
                    self._sticky_proxy is not None
                    and self._sticky_proxy.selection_key == proxy.selection_key
                ):
                    self._sticky_proxy = None  # drop a now-bad sticky
                _log.info(
                    "image proxy %s failed — rotating to a different proxy",
                    proxy.identity,
                )
                continue
            finally:
                _ACTIVE_IMAGE_PROXY.reset(token)
            self._sticky_proxy = proxy  # first success becomes the new sticky
            _log.debug("image proxy %s served the fetch", proxy.identity)
            return result

        if last_exc is not None:
            raise last_exc  # the terminal image-fetch surface the engine understands
        raise SourceError(
            "source_unavailable",
            "no image proxy available (all proxies on cooldown)",
        )

    async def _request_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        limited: bool,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        op: str = "request",
    ) -> httpx.Response:
        """Single request → validated ``httpx.Response`` (clearance + 403 reconcile).

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
        ``application/x-www-form-urlencoded``. Returns the validated response (after the
        STOP gate + ``raise_for_status``); callers decide whether to decrypt the body.
        """
        resp = await self._send(
            url,
            params=params,
            limited=limited,
            force_resolve=False,
            method=method,
            data=data,
            json_body=json_body,
            extra_headers=extra_headers,
            op=op,
        )
        # Issue #172: do NOT pre-gate on ``status_code == 403``. ``is_cf_challenge``
        # recognizes Cloudflare 503 interstitials as challenges too, and it carries
        # its own status awareness (early-returns False for any status ∉ {403, 503}
        # and only inspects the body after the status+server checks, so it is cheap
        # and safe on the 2xx happy path). A hard-coded 403 gate let a 503 CF
        # challenge skip this forced-re-solve branch and burn tenacity retries
        # against the same stale cookie/UA without recovering.
        cf_stale = self._is_cloudflare and is_cf_challenge(resp)
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
                json_body=json_body,
                extra_headers=extra_headers,
                op=op,
            )
            # debug comix-recent-403 follow-up: distinguish "the re-solve could not
            # clear Cloudflare" from a plain post-refresh 403. When a CF challenge
            # SURVIVES the forced re-solve, the solver failed to earn a fresh
            # cf_clearance — almost always because headless Chromium is now
            # fingerprint-blocked by the host's Cloudflare. Emit a distinct WARN so
            # this is diagnosable from logs immediately, without waiting for the
            # source cooldown / the generic "upstream 403" warning to surface.
            if cf_stale and is_cf_challenge(resp):
                _log.warning(
                    "Cloudflare challenge SURVIVED a forced re-solve "
                    "(source=%s op=%s status=%d) — the solver could not earn a "
                    "fresh cf_clearance; headless Chromium may be blocked "
                    "(check GATEWAY_CLOUDFLARE_HEADLESS=false). "
                    "Source enters cooldown.",
                    self._source_key,
                    op,
                    resp.status_code,
                )
        if resp.status_code in _PERMANENT_STATUSES:
            # D-03 (260620-5yq): a WAF "malicious payload" 403 is a recoverable
            # false positive the SOURCE owns (strip the trigger token + retry), so
            # mint a DISTINCT ``waf_blocked`` code BEFORE the generic terminal raise.
            # This runs AFTER the CF/CSRF reconcile branches above (both False for the
            # WAF body), so their behaviour is untouched; every other 403/401/404 still
            # raises the unchanged ``source_unavailable`` (mangaball-scoped — it is the
            # only source returning the ``Malicious payload`` body).
            if is_waf_block(resp):
                raise SourceError(
                    "waf_blocked",
                    "upstream WAF block (sanitizable query)",
                    status=resp.status_code,
                )
            raise SourceError(
                "source_unavailable",
                f"upstream {resp.status_code}",
                status=resp.status_code,
            )
        resp.raise_for_status()  # 5xx → HTTPStatusError → retried by _is_retryable
        return resp

    async def _request_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        limited: bool,
        decrypt: bool = True,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        op: str = "request",
    ) -> bytes:
        """Single request → optionally-decrypted body (delegates to
        :meth:`_request_response`).

        The validated-response machinery (clearance injection, the two independent
        403-reconcile branches, the permanent-4xx STOP, ``raise_for_status``) now lives
        in :meth:`_request_response`; this method adds ONLY the decrypt-or-not branch.
        Returns DECRYPTED bytes (D-39) when ``decrypt`` is True (the default);
        ``*_plain`` opts out for plaintext endpoints. Byte-for-byte equivalent to the
        pre-refactor path for every caller.
        """
        resp = await self._request_response(
            url,
            params=params,
            limited=limited,
            method=method,
            data=data,
            json_body=json_body,
            extra_headers=extra_headers,
            op=op,
        )
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
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        op: str = "request",
    ) -> httpx.Response:
        """Inject credentials (D-02/D-40) and issue ONE request (gated iff ``limited``).

        ``method`` defaults to ``"GET"`` (the unchanged search/image path). For
        ``method="POST"`` the ``data=dict`` form body is attached and httpx
        form-encodes it as ``application/x-www-form-urlencoded`` (A2).

        Metrics seam (OBS-01/02/05, RESEARCH Pattern 2): this is the ONE http choke
        point, so the awaited transport call is wrapped in ``perf_counter`` and an
        additive ``emit_http`` (+ ``emit_limiter_wait`` for the limiter acquire).
        The emit is STRICTLY additive — it never changes control flow, exception
        propagation, or timing of the request: a ``None`` collector short-circuits
        to a no-op, and ``_emit_http`` swallows any collector-side error so a
        metric failure can never break search/download (T-08-15).
        """
        kwargs = await self._clearance_kwargs(force_resolve=force_resolve)
        # Pass ``params`` to httpx ONLY when NON-EMPTY. httpx treats ``params=`` as
        # the COMPLETE query and REPLACES any query already present on ``url`` — so
        # an EMPTY dict (``get_json``/``get_json_plain(url)`` forward ``**params``
        # == ``{}``) would STRIP the URL's own query string. This is a latent
        # footgun that bit comix search (debug ``comix-search-api-403``): the source
        # replays a fully-formed, JS-pre-signed URL carrying the ``_=`` request
        # token in its query, and ``params={}`` wiped the entire query (token and
        # all) → ``{"message":"Missing token."}`` 403. Existing callers build their
        # query via a real (non-empty) ``params`` dict on a query-LESS ``url``, so
        # skipping the empty-dict case is byte-for-byte unchanged for them while
        # letting a caller GET a pre-signed URL verbatim.
        if params:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        if json_body is not None:
            # httpx serializes ``json=`` to a body + sets ``Content-Type:
            # application/json`` (the JSON-body POST path, post_json_body).
            kwargs["json"] = json_body
        if extra_headers:
            # Merge per-call headers ON TOP of the credential headers (D-02/D-40) so a
            # source-supplied header (e.g. ``x-integrity-token``) composes with the
            # injected ``cf_clearance`` cookie + UA. The clearance headers win on a
            # name clash (they are the credential of record); ``extra_headers`` here
            # carry only source-specific, non-credential keys.
            merged = dict(extra_headers)
            merged.update(kwargs.get("headers") or {})
            kwargs["headers"] = merged
        # ``attempt`` rides ``force_resolve``: a forced re-solve is the D-35
        # attempt-2 retry; tenacity's own retries each re-enter ``_send`` and emit
        # their own http event, so per-attempt counts come for free.
        attempt = 2 if force_resolve else 1
        if limited:
            wait_start = time.perf_counter()
            async with self._limiter:  # gate at CALL SITE (aiolimiter caveat)
                wait_ms = (time.perf_counter() - wait_start) * 1000.0
                self._emit_limiter_wait(wait_ms)
                return await self._send_emitting(
                    method, url, op=op, attempt=attempt, **kwargs
                )
        return await self._send_emitting(method, url, op=op, attempt=attempt, **kwargs)

    async def _send_emitting(
        self,
        method: str,
        url: str,
        *,
        op: str,
        attempt: int,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue the ONE transport request, emitting an additive ``http`` event.

        Behaviour-neutral: on success returns the response unchanged after emitting
        an outcome CLASSIFIED by status; on exception emits ``outcome="error"`` then
        re-raises the ORIGINAL exception untouched. A ``None`` collector makes both
        emits no-ops.

        Outcome classification mirrors the request-middleware WR-04 split (and the
        contract ``client_error`` value): ``>=500 -> "error"``,
        ``>=400 -> "client_error"``, else ``"ok"``. This success path is reached
        BEFORE the caller's ``raise_for_status()`` (``_send_with_clearance`` raises
        only AFTER ``_send`` returns), so a 4xx/5xx transport response DOES land here
        and was previously mis-emitted as ``"ok"`` — a 401/404/500 from the upstream
        is now visible in the failures ring / error_rate per-call, not just at the
        request rollup. (Even where tenacity/raise_for_status would later convert a
        5xx to the except branch, this classification is correct + harmless.)
        """
        start = time.perf_counter()
        # Transport pick (three-way). 260620-4im: while a proxied ``fetch_image`` is in
        # flight (the task-local ``_ACTIVE_IMAGE_PROXY`` is set), every inner
        # ``ctx.get_bytes`` — including the source's own per-page zone-retries —
        # egresses through the SAME proxy (proxy OUTER, source-logic INNER). The read
        # is task-local so concurrent page fetches never cross-route. Otherwise route to
        # the download pool on the download path, else the search pool (debug
        # pool-starves-search-cooldown). All share ONE identity (clearance rides
        # per-request headers), so this only picks which connection pool/egress.
        active_proxy = _ACTIVE_IMAGE_PROXY.get()
        if active_proxy is not None:
            assert self._proxy_pool is not None  # set iff a proxy is active
            transport = self._proxy_pool.transport_for(active_proxy)
        elif self._use_download_transport:
            transport = self._session.download_transport
        else:
            transport = self._session.transport
        try:
            resp = await transport.request(method, url, **kwargs)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            self._emit_http(
                op=op,
                method=method,
                url=url,
                status=None,
                outcome="error",
                duration_ms=duration_ms,
                attempt=attempt,
                error=repr(exc),
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000.0
        # Classify by status (mirrors middleware WR-04 + contract client_error).
        if resp.status_code >= 500:
            outcome = "error"
        elif resp.status_code >= 400:
            outcome = "client_error"
        else:
            outcome = "ok"
        self._emit_http(
            op=op,
            method=method,
            url=url,
            status=resp.status_code,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
            error=None,
        )
        return resp

    @staticmethod
    def _emit_http(
        *,
        op: str,
        method: str,
        url: str,
        status: int | None,
        outcome: str,
        duration_ms: float,
        attempt: int,
        error: str | None,
    ) -> None:
        """No-op-safe, failure-isolated ``emit_http`` (T-08-14/T-08-15)."""
        collector = get_collector()
        if collector is None:
            return
        try:
            collector.emit_http(
                op=op,
                method=method,
                url=url,
                status=status,
                outcome=outcome,
                duration_ms=duration_ms,
                attempt=attempt,
                error=error,
            )
        except Exception:  # noqa: BLE001 — a metric failure must never break a request
            pass

    @staticmethod
    def _emit_limiter_wait(wait_ms: float) -> None:
        """Emit ``limiter-wait`` only when the acquire actually blocked (Open Q2)."""
        if wait_ms < _LIMITER_WAIT_EMIT_THRESHOLD_MS:
            return
        collector = get_collector()
        if collector is None:
            return
        try:
            collector.emit_limiter_wait(duration_ms=wait_ms)
        except Exception:  # noqa: BLE001 — a metric failure must never break a request
            pass

    def _feed_failure(self) -> None:
        if self._source_health is not None:
            self._source_health.record_failure()

    def _feed_success(self) -> None:
        if self._source_health is not None:
            self._source_health.record_success()
