"""Anti-bot solver seam (BOT-01/BOT-02).

Kept tiny so the Patchright -> Camoufox escalation (Phase 4) is a config flip.
Phase 1 uses ``NoopSolver`` (MangaDex et al. need no challenge solving); Phase 4
fills ``CloudflareSolver`` with a real Patchright persistent-context solve behind a
bounded :class:`~manga_gateway.framework.solver_lifecycle.BrowserLifecycle`.

Option A (Plan 04-04 deviation, 2026-05-30) — browser-driven content fetch:
``CloudflareSolver`` exposes :meth:`fetch_via_browser` — a small primitive that
navigates a page in the warm context, optionally waits for a selector or JS
condition, and runs ``page.evaluate`` to read JSON-serializable data out of the
rendered DOM. Sources whose encrypted-API endpoints require a per-chapter token
that we cannot mint statically (Comix ``/api/v1/chapters/{id}`` requires ``_=``
minted by the same VM-obfuscated ``secure-*.js`` that does decryption) instead
let the live page's own JS do token-mint + API call + decrypt + image-tag render,
then read the result from the DOM. The httpx path remains the bulk image fetcher
(CLAUDE.md "image fetch is NEVER through the browser"); the browser only drives
the manifest resolution step.

Engine selection (#35 / #40 / comix-parallel-engine-probe): the underlying browser
is selected via the ``engine`` parameter (``"patchright"`` Chromium-based, default
since 2026-06-01; ``"camoufox"`` Firefox-based, opt-in). Patchright is the single
dev/CI/prod default — it is the only engine that runs concurrent Cloudflare
navigations on one warm context, so it makes ``fetch_concurrency > 1`` work.
Camoufox remains an opt-in fallback for hosts where Chromium's fingerprint is
flagged (historically the cloud-Linux datacenter runner, issue #35); it cannot run
parallel navigations (Camoufox-only failure modes such as issue #54: Playwright
Firefox handler crash on undefined pageError.location). Both engines back the SAME
``AntiBotSolver`` interface — the swap is a single constructor argument with
no rewrite (CLAUDE.md "keep the browser behind an interface so this is a
config flip"). The launch closure is selected at ``__init__`` and remains LAZY
(the heavy import happens only on first solve), so neither browser binary is
touched by the deterministic gate (D-42).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, runtime_checkable
from urllib.parse import urlparse

from ..metrics.collector import get_collector
from .solver_lifecycle import BrowserLifecycle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

_log = logging.getLogger("manga_gateway")

# Return type of a ``_browser_call`` interaction ``body`` — generic so each
# ``_once`` worker preserves its own result type (Any / list / the typed read)
# through the shared scaffolding helper without a cast (D-02).
_BodyResult = TypeVar("_BodyResult")

# Bounded poll budget for the paginated Next-walk (#146): after a JS-click of
# the Next control, re-run the extract up to ``_PAGINATE_POLL_TICKS`` times
# spaced ``_PAGINATE_POLL_INTERVAL`` seconds apart, watching for the rendered
# id-set to change. Exhausting the budget with no change is stop-condition B
# (no new rows) — a bounded, non-blocking guard against an infinite spin if a
# site renders an always-present Next that never actually advances. The poll
# uses ``asyncio.sleep`` (never a blocking sleep) so the event loop stays free.
_PAGINATE_POLL_TICKS = 20
_PAGINATE_POLL_INTERVAL = 0.1

# Default URL the solver navigates to for cf_clearance acquisition. Overridden
# by the application wiring (app.py lifespan) from the concrete source's
# metadata — the framework solver itself never names a host. The framework
# default exists only so unit tests can spin up a CloudflareSolver without
# naming a host. Pinned by Plan 04-04 live recon when the application supplies
# a real source.
_DEFAULT_CHALLENGE_URL = "https://example.invalid/"

# Supported anti-bot browser engines (#35). The selector lives on
# ``Settings.cloudflare_engine`` and is forwarded into ``CloudflareSolver``.
# Patchright is the default everywhere (dev + CI + prod) since 2026-06-01 — it is
# the only engine that runs concurrent CF navigations on one warm context
# (comix-parallel-engine-probe). Camoufox is the opt-in fallback for
# fingerprint-flagged hosts (#35 / #40 / #54).
AntibotEngine = Literal["patchright", "camoufox"]


def _belongs_to_host(cookie: dict[str, Any], host: str) -> bool:
    """True if ``cookie`` is bound to ``host`` (or a parent domain thereof).

    Patchright cookie dicts may carry ``domain`` either with or without a leading
    dot (``.example.com`` vs ``example.com``); both forms denote subdomain-
    inclusive binding. Cross-site cookies (e.g. ad-tracking on unrelated
    domains) are excluded.
    """
    if not host:
        return False
    raw = cookie.get("domain") or ""
    if not raw:
        return False
    domain = raw.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


def _emit_browser(
    *,
    op: str,
    url: str,
    outcome: str,
    duration_ms: float,
    attempt: int,
    error: str | None,
) -> None:
    """No-op-safe, failure-isolated ``emit_browser`` (#125, T-e9a-05).

    Mirrors ``solver_lifecycle._emit_solve`` / ``context._emit_http``: a ``None``
    collector short-circuits and any collector-side error is swallowed so a metric
    failure can NEVER break a browser fetch. ``source_key`` + ``request_id``
    self-attribute via the contextvars already bound by ``fanout._guarded`` (the
    comix ``_series_chapters`` browser read runs inside ``_run_one`` inside the
    fan-out's ``source_scope``). The url is redacted at the collector ingest
    boundary (same path as ``emit_http``).
    """
    collector = get_collector()
    if collector is None:
        return
    try:
        collector.emit_browser(
            op=op,
            url=url,
            outcome=outcome,
            duration_ms=duration_ms,
            attempt=attempt,
            error=error,
        )
    except Exception:  # noqa: BLE001 — a metric failure must never break a fetch
        pass


class BrowserFetchError(RuntimeError):
    """Raised when :meth:`CloudflareSolver.fetch_via_browser` cannot complete
    (navigation failure, ``wait_for`` timeout, evaluate exception).
    """


# Markers in Playwright/Camoufox exception messages that indicate the underlying
# Node driver process has died — typically because the bundled Firefox handler
# crashed (issue #54: ``coreBundle.js:49624`` reads ``pageError.location.url``
# without a null guard; Playwright 1.60.0 maintainer rejected the defensive
# fallback in PR #40982 so this is not getting fixed upstream). Once the driver
# is dead, every subsequent call against ANY existing ``BrowserContext`` /
# ``Page`` handle raises with one of these substrings — we detect by substring
# rather than exception type because Playwright wraps driver-protocol failures
# as plain ``Error``/``TargetClosedError`` instances whose class identity is not
# stable across the Patchright/Camoufox engines we support.
_DEAD_DRIVER_MARKERS: tuple[str, ...] = (
    "Connection closed while reading from the driver",
    "Target page, context or browser has been closed",
    "Target closed",
    "Browser closed",
    "Browser has been closed",
)


def _looks_like_dead_driver(exc: BaseException) -> bool:
    """True if ``exc`` or its ``__cause__`` chain carries a dead-driver marker.

    ``fetch_via_browser`` already wraps the underlying Playwright exception via
    ``raise BrowserFetchError(...) from exc``, so the marker substring lives on
    ``exc.__cause__`` rather than on the wrapper itself. We walk the chain to
    handle either case (and ``__context__`` is intentionally NOT walked — we
    only care about explicitly chained causes, not incidental ones).

    Guards against pathological self-referential cause cycles via an id-set.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        message = str(cur)
        if any(marker in message for marker in _DEAD_DRIVER_MARKERS):
            return True
        cur = cur.__cause__
    return False


def _looks_like_js_predicate(text: str) -> bool:
    """Classify a ``wait_for`` string as a JS predicate vs a CSS selector.

    Heuristic: a JS predicate either is an arrow function (``=>``) or contains
    a ``return`` statement. Everything else is treated as a CSS selector. The
    classifier is intentionally simple — sources pick the format deliberately,
    so a misclassification surfaces as a clear test failure rather than silent
    drift.
    """
    return "=>" in text or "return " in text


# Matches a ``limit=<digits>`` query param, capturing the ``?``/``&`` + key so
# only the numeric value is rewritten (group 1 is kept verbatim). Used by the
# generic ``route_limit_rewrite`` handler — the SUBSTRING that selects which
# requests to touch and the TARGET value both arrive from the caller (a source
# constant), so this framework helper carries no site-specific literal.
_LIMIT_PARAM_RE = re.compile(r"([?&]limit=)\d+")


def _build_limit_rewrite_route_handler(
    url_substring: str, target_limit: int
) -> Callable[[Any], Awaitable[None]]:
    """Build a Patchright ``page.route`` handler that rewrites a ``limit`` param.

    Requests whose URL CONTAINS ``url_substring`` have their ``limit=\\d+`` query
    value rewritten to ``target_limit`` and are continued with the new URL;
    every other request is continued untouched. The rewrite is scoped (only the
    caller-named substring's requests, only the ``limit`` value) and reflects no
    client input — ``url_substring``/``target_limit`` are source constants
    (T-eqm-01). ``page.route`` is the mechanism (NOT ``add_init_script``, which
    Patchright suppresses).
    """

    async def _handler(route: Any) -> None:
        req_url = route.request.url
        if url_substring in req_url:
            new_url = _LIMIT_PARAM_RE.sub(rf"\g<1>{target_limit}", req_url)
            await route.continue_(url=new_url)
        else:
            await route.continue_()

    return _handler


def _build_page_limit_rewrite_route_handler(
    url_substring: str, target_page: int, target_limit: int, page_param: str
) -> Callable[[Any], Awaitable[None]]:
    """Build a ``page.route`` handler that pins BOTH ``page`` and ``limit`` params.

    Mirrors :func:`_build_limit_rewrite_route_handler` but, on a URL containing
    ``url_substring``, ALSO rewrites the caller-named ``page_param=\\d+`` query
    value to ``target_page`` (in addition to the ``limit=\\d+`` → ``target_limit``
    rewrite). Used by the parallel-pagination fan-out: each fan-out page pins its
    own ``page`` so the SPA renders that page at the SAME ``limit`` discovery used
    (the load-bearing consistency point — discovery and fan-out must share the
    limit or P is mis-counted). Every other request is continued untouched. The
    rewrites reflect no client input — ``url_substring``/``target_*``/``page_param``
    are source constants (T-u7f-01). The ``limit`` rewrite reuses the shared
    :data:`_LIMIT_PARAM_RE`; the page param NAME is a caller argument (D-41 — no
    source literal; ``limit``/``page`` are generic HTTP params).
    """
    page_re = re.compile(rf"([?&]{re.escape(page_param)}=)\d+")

    async def _handler(route: Any) -> None:
        req_url = route.request.url
        if url_substring in req_url:
            new_url = _LIMIT_PARAM_RE.sub(rf"\g<1>{target_limit}", req_url)
            new_url = page_re.sub(rf"\g<1>{target_page}", new_url)
            await route.continue_(url=new_url)
        else:
            await route.continue_()

    return _handler


async def _evaluate_guarded(
    page: Any,
    script: str,
    timeout: float,  # noqa: ASYNC109 — per-evaluate budget (wait_for), not a cancel wrapper
) -> Any:
    """Run ``page.evaluate(script)`` under a timeout, translating failures.

    Mirrors the extract-call discipline in ``_fetch_via_browser_once``: wraps
    the evaluate in ``asyncio.wait_for`` and converts a TimeoutError or any
    other exception into a single :class:`BrowserFetchError` so the caller sees
    one type. Used by the paginated walk for the extract read, the next-button
    probe, and the JS-click.
    """
    try:
        return await asyncio.wait_for(page.evaluate(script), timeout=timeout)
    except TimeoutError as exc:
        raise BrowserFetchError(f"page.evaluate timed out after {timeout}s") from exc
    except Exception as exc:  # noqa: BLE001
        raise BrowserFetchError(f"page.evaluate failed: {exc}") from exc


def _attach_browser_event_loggers(page: Any, *, nav_url: str) -> None:
    """Wire ``page.on("pageerror" | "console" | "requestfailed")`` loggers.

    Diagnostic-only — gated by ``Settings.cloudflare_log_browser_events``
    (env: ``GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1``). Captures the JS throw
    site behind the Playwright Firefox driver crash investigated in debug
    session ``comix-pageerror-throw-site-54`` (issue #54): the upstream
    handler reads ``pageError.location.url`` without a null guard, so we
    log every uncaught error the page emits in the hope of correlating it
    with a stripped-location event right before the driver dies.

    Handler safety: every attribute read uses ``getattr(..., None)`` so the
    handler itself cannot crash on the very ``location=None`` pattern under
    investigation — a handler that throws would be both useless and a noise
    source. The console handler filters to ``msg.type in ("error", "warning")``
    so debug/log/info spam does not flood the log; the other two handlers
    capture every event unconditionally (they are already rare enough). Each
    line records BOTH the initial navigation ``url`` and ``page.url`` at event
    time so post-redirect events are unambiguous in the captured stream.

    Playwright 1.60.0 ``Page.on(...)`` expects SYNC callbacks — ``async def``
    handlers silently no-op, so these are plain ``def``.
    """

    def _on_pageerror(error: Any) -> None:
        try:
            _log.info(
                "browser-event pageerror nav_url=%s page_url=%s "
                "name=%s message=%s stack=%s",
                nav_url,
                getattr(page, "url", None),
                getattr(error, "name", None),
                getattr(error, "message", None),
                getattr(error, "stack", None),
            )
        except Exception:  # noqa: BLE001 — diagnostic handler must NEVER raise
            pass

    def _on_console(msg: Any) -> None:
        try:
            msg_type = getattr(msg, "type", None)
            if msg_type not in ("error", "warning"):
                return
            _log.info(
                "browser-event console nav_url=%s page_url=%s "
                "type=%s text=%s location=%s",
                nav_url,
                getattr(page, "url", None),
                msg_type,
                getattr(msg, "text", None),
                getattr(msg, "location", None),
            )
        except Exception:  # noqa: BLE001 — diagnostic handler must NEVER raise
            pass

    def _on_requestfailed(request: Any) -> None:
        try:
            _log.info(
                "browser-event requestfailed nav_url=%s page_url=%s "
                "url=%s method=%s failure=%s",
                nav_url,
                getattr(page, "url", None),
                getattr(request, "url", None),
                getattr(request, "method", None),
                getattr(request, "failure", None),
            )
        except Exception:  # noqa: BLE001 — diagnostic handler must NEVER raise
            pass

    # Playwright's Page.on registers SYNC callbacks (NOT awaitables) — async
    # handlers silently no-op. ``page.on`` itself is sync; not awaited.
    page.on("pageerror", _on_pageerror)
    page.on("console", _on_console)
    page.on("requestfailed", _on_requestfailed)


@dataclass
class Clearance:
    """Captured anti-bot clearance: cookies + the UA they were issued for."""

    cookies: dict[str, str]
    user_agent: str


@runtime_checkable
class AntiBotSolver(Protocol):
    """Resolves a per-source anti-bot challenge into a reusable clearance.

    Browser-primitive seam (off-Protocol, D-41). The concrete
    :class:`CloudflareSolver` ALSO exposes three browser-driven content-read
    primitives that are deliberately NOT members of this ``runtime_checkable``
    Protocol — adding them would flip ``isinstance(NoopSolver(), AntiBotSolver)``
    to ``False`` and break the conftest ``_FakeSolver`` (which satisfies the
    Protocol with only ``get_clearance``) plus the D-41 invariant asserted by
    ``test_source_health.py``. Sources detect them via ``hasattr`` instead:

    * ``async def fetch_via_browser(url, *, extract, wait_for=None,
      timeout=30.0) -> Any`` — one-shot goto→wait→evaluate DOM read.
    * ``async def fetch_via_browser_paginated(url, *, extract, wait_for=None,
      next_selector, route_limit_rewrite=None, max_pages=200, timeout=30.0)
      -> list`` — the same one-shot read PLUS an optional ``page.route()``
      limit-rewrite and an in-page Next-walk that accumulates deduped rows
      across pagination, all inside ONE page lifecycle (#146).
    * ``async def fetch_via_browser_parallel_pages(url, *, extract,
      wait_for=None, last_page_selector, page_param, route_rewrite,
      max_pages=200, timeout=30.0) -> list`` — discovers the page count P via
      the caller's "Last page" button + ``?page=N`` URL sync, then fans pages
      1..P out CONCURRENTLY (each pinned by its own ``page.route()`` ``page``+
      ``limit`` rewrite, bounded by the SAME ``_browser_lock`` semaphore), and
      merges deduped-by-``id`` in newest-first order. A page that fails or yields
      no rows fails closed (raises) — never a partial list (#222, spike 014).
    """

    async def get_clearance(self, source_key: str) -> Clearance | None:
        """Return clearance for ``source_key``, or ``None`` if none needed."""
        ...


class NoopSolver:
    """Default solver: no challenge to solve (BOT-01 Phase 1 default)."""

    async def get_clearance(self, source_key: str) -> None:
        return None


class CloudflareSolver:
    """Patchright/Camoufox-backed Cloudflare clearance solver (BOT-01/BOT-02).

    Drives a stealth-browser persistent context (D-34 cross-restart clearance) to
    solve the Cloudflare challenge, then captures the ``cf_clearance`` cookie + the
    EXACT ``navigator.userAgent`` of that SAME session (Pitfall 1 — the cookie is
    bound to its issuing UA) into a :class:`Clearance`. All browser work runs OFF
    the event loop via Playwright's native async API, behind a bounded
    :class:`BrowserLifecycle` (solve cap + single-flight + recycle + cleanup-on-all-
    paths, criterion #4).

    Engine selection (#35 / #40 / comix-parallel-engine-probe): ``engine=
    "patchright"`` (the default since 2026-06-01) uses Chromium with patched CDP
    leaks — it is the ONLY engine that can run concurrent Cloudflare navigations
    on one warm context, so it is the default that makes ``fetch_concurrency`` >
    1 work. ``engine="camoufox"`` (Firefox build, C++ fingerprint spoofing) is
    the opt-in fallback for hosts where Chromium's fingerprint is flagged
    (historically the cloud-Linux datacenter runner, issue #35); it CANNOT run
    parallel, so it must be paired with ``fetch_concurrency=1``. The choice
    affects ONLY which
    launch closure is wired into the lifecycle — the solve closure
    (cf_clearance polling) is engine-agnostic. The heavy import happens
    inside the launch closure so the deterministic gate never imports/launches
    a browser (D-42); tests inject a ``lifecycle`` whose ``launch``/``solve``
    drive a mocked browser instead.

    The internal keyword-only ``force_resolve`` on ``get_clearance`` is the D-35
    re-solve path; it is deliberately kept OFF the ``AntiBotSolver`` Protocol (D-41).
    """

    def __init__(
        self,
        *,
        user_data_dir: str = "cloudflare-userdata",
        headless: bool = True,
        solve_concurrency: int = 1,
        fetch_concurrency: int = 1,
        recycle_seconds: float | None = None,
        challenge_url: str = _DEFAULT_CHALLENGE_URL,
        challenge_urls: dict[str, str] | None = None,
        cloudflare_keys: Iterable[str] = (),
        # On-demand keys (``cloudflare_challenge_optional=True``) — ``warm()`` skips
        # them so an intermittent-challenge source is never eager-solved/force-disabled
        # at startup; it solves on-demand when a request hits a live challenge (debug
        # pooltimeout-recurrence). MangaFire is an on-demand patchright source
        # (260623-m5h): its search computes the vrf in-process and answers cold over
        # httpx, so it defer-solves Cloudflare instead of warming eagerly.
        on_demand_keys: Iterable[str] = (),
        engine: AntibotEngine = "patchright",
        log_browser_events: bool = False,
        proxy: dict[str, str] | None = None,
        lifecycle: BrowserLifecycle | None = None,
        warm_attempts: int = 1,
        warm_retry_seconds: float = 2.0,
    ) -> None:
        self._user_data_dir = user_data_dir
        self._headless = headless
        # PROXY-01 / #65: optional single static proxy, already built into the
        # Playwright dict shape by ``framework.proxy.build_proxy`` at the lifespan
        # boundary (the solver does NOT unpack SecretStr itself). Threaded into
        # BOTH launch closures so the CF-clearing browser egresses through the
        # same proxy as the httpx image fetch (cf_clearance is IP-bound). None ⇒
        # no ``proxy=`` kwarg passed at launch — today's behavior unchanged.
        self._proxy = proxy
        # Per-domain challenge URLs (#88): map ``source_key`` -> challenge URL so
        # each cloudflare-gated source solves against its OWN host and captures
        # only that host's cookies. ``challenge_url`` is RETAINED as the
        # fallback default for any cf key lacking an explicit ``challenge_urls``
        # entry (keeps single-CF tests + the _DEFAULT_CHALLENGE_URL placeholder
        # working). With exactly one entry this is byte-for-byte the old
        # single-domain behavior.
        self._challenge_url = challenge_url
        self._challenge_urls = dict(challenge_urls or {})
        self._cloudflare_keys = frozenset(cloudflare_keys)
        self._on_demand_keys = frozenset(on_demand_keys)
        self._engine: AntibotEngine = engine
        # #153: eager-warm retry budget. ``warm()`` retries each source up to
        # ``warm_attempts`` times (linear backoff ``warm_retry_seconds * attempt``)
        # before reporting it failed, absorbing the cold-start launch flake. The
        # default 1 keeps the historic single-attempt behavior for direct
        # construction; the lifespan wires the configured values.
        self._warm_attempts = warm_attempts
        self._warm_retry_seconds = warm_retry_seconds
        # #54 diagnostic: when True, attach pageerror/console/requestfailed
        # loggers to every page opened by ``_fetch_via_browser_once`` so the
        # JS throw site behind the Playwright Firefox handler crash can be
        # captured under nightly conditions. OFF by default — log volume
        # and per-page overhead are too high for production. Toggled via
        # ``GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1`` through Settings.
        self._log_browser_events = log_browser_events
        # Tests inject a lifecycle wrapping a MOCKED browser; production builds one
        # wrapping the real lazy-launch/solve closures. The launch closure is
        # selected by ``engine`` (Patchright default since 2026-06-01; Camoufox opt-in).
        # No real browser is touched until the first solve / explicit warm().
        launch = (
            self._launch_camoufox_context
            if engine == "camoufox"
            else self._launch_patchright_context
        )
        self._lifecycle = lifecycle or BrowserLifecycle(
            launch=launch,
            solve=self._solve_keyed,
            solve_concurrency=solve_concurrency,
            recycle_seconds=recycle_seconds,
        )
        self._playwright: Any = None  # the started playwright instance (real path)
        # Virtual framebuffer (Xvfb) handle for HEADED launches on a display-less
        # Linux host (servers / CI / datacenter). Headed Chromium clears
        # Cloudflare on datacenter IPs where headless is fingerprint-flagged
        # (comix-parallel-engine-probe); headed needs a display a server lacks, so
        # we start one lazily. None until the first headed Linux launch.
        self._virtual_display: Any = None
        # Bounds concurrent ``fetch_via_browser`` calls on the warm context.
        # An ``asyncio.Semaphore`` backs the gate; ``fetch_concurrency=1``
        # (the default) reproduces the historic ``asyncio.Lock`` shape
        # exactly — one nav at a time, the safe default for every engine.
        # Driven from ``Settings.cloudflare_fetch_concurrency`` so an ops-side
        # ``GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY=N`` bump needs no recompile.
        #
        # ENGINE CONSTRAINT (debug session ``comix-parallel-engine-probe``,
        # 2026-06-01): ``> 1`` is ONLY safe on ``engine="patchright"``
        # (Chromium), which runs N concurrent CF navigations on one shared warm
        # context cleanly. ``engine="camoufox"`` (Firefox) STALLS concurrent CF
        # navigations at goto-commit (the chapter-list DOM never renders), so
        # the cap MUST stay at 1 there. The failure is engine-specific, NOT a
        # Cloudflare per-IP burst limit — the earlier "CF burst" diagnosis
        # (issue #59 / resolved session ``comix-parallel-page-contention``) is
        # REFUTED. See ``config.py``'s ``cloudflare_fetch_concurrency`` comment.
        self._browser_concurrency = fetch_concurrency
        self._browser_lock: asyncio.Semaphore = asyncio.Semaphore(
            self._browser_concurrency
        )

    # ─────────────────────────── public seam (D-41) ───────────────────────────

    @property
    def engine(self) -> AntibotEngine:
        """The configured anti-bot browser engine (#35)."""
        return self._engine

    async def get_clearance(
        self,
        source_key: str,
        *,
        force_resolve: bool = False,
        solve_if_missing: bool = True,
    ) -> Clearance | None:
        """Return clearance for ``source_key`` (``None`` for non-cloudflare keys).

        ``force_resolve`` (internal, D-35) skips the held clearance and runs a fresh
        solve. ``solve_if_missing=False`` is the on-demand peek (debug
        pooltimeout-recurrence). Both are internal kwargs, NOT part of the
        ``AntiBotSolver`` Protocol (D-41).

        Unlike the AndroidSolver, this desktop solver keeps NO in-memory held cache —
        cf_clearance persists in the browser ``cloudflare-userdata`` dir and every
        solve re-navigates (short-circuiting on the on-disk cookie). There is therefore
        nothing to return cheaply for a peek, so ``solve_if_missing=False`` on a
        non-force call returns ``None`` and defers to the challenge-triggered
        force-resolve. (MangaFire sets ``cloudflare_challenge_optional`` — its search
        answers cold over httpx — so this on-demand peek path is live for patchright.)
        """
        if source_key not in self._cloudflare_keys:
            return None  # MangaDex et al. — no clearance needed
        if not force_resolve and not solve_if_missing:
            return None
        return await self._lifecycle.solve(source_key, force=force_resolve)

    async def warm(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> list[str]:
        """Best-effort eager solve at startup (D-33) + start the recycle watchdog.

        Called as a fire-and-forget task by the lifespan so a slow/failed solve never
        blocks startup. Returns the list of cloudflare source keys whose eager solve
        FAILED so the lifespan can disable ONLY those (D-33, per-domain isolation).

        #88 / PR#90 review: eager-warm ALL cloudflare-gated sources (LOCKED decision
        — no early break), each on its OWN per-domain challenge URL. A per-domain
        solve failure must isolate to THAT source — with several cloudflare domains
        now sharing one solver, one bad domain at startup must NOT take the healthy
        ones down. Each key's solve is therefore wrapped in its own try/except and
        only the failing keys are returned; a TOTAL failure (e.g. the browser never
        launches) naturally surfaces as every key failing.

        #153: each source is retried up to ``self._warm_attempts`` times with a
        linear backoff before being reported failed — a cold-deploy launch flake
        (the warm dies <1s but an on-demand warm works seconds later) is absorbed
        rather than latching the source disabled for the 12h ``/caps`` window. The
        underlying exception is logged with ``exc_info`` so the real cause (browser
        launch error / cold-profile race / CF challenge) is no longer swallowed.
        ``sleep`` is injectable so tests assert the retry without real waiting.
        """
        self._lifecycle.start_recycle_watchdog()
        failed: list[str] = []
        for key in self._cloudflare_keys:
            if key in self._on_demand_keys:
                # On-demand source: never eager-warm — solves only on a live challenge
                # (debug pooltimeout-recurrence).
                continue
            if not await self._warm_one(key, sleep=sleep):
                failed.append(key)
        return failed

    async def _warm_one(
        self,
        key: str,
        *,
        sleep: Callable[[float], Awaitable[None]],
    ) -> bool:
        """Eager-solve ONE source with bounded retry/backoff (#153).

        Returns ``True`` as soon as a solve succeeds, ``False`` once every attempt
        is exhausted. Every failed attempt is logged with ``exc_info`` so the real
        cause surfaces (criterion #1); the final give-up is logged once at WARNING.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self._warm_attempts + 1):
            try:
                await self.get_clearance(key)
            except Exception as exc:  # noqa: BLE001 — isolate per-domain warm failures
                last_exc = exc
                _log.warning(
                    "CloudflareSolver warm attempt %d/%d failed for source "
                    "%r: %r (D-33/#153)",
                    attempt,
                    self._warm_attempts,
                    key,
                    exc,
                    exc_info=True,
                )
                if attempt < self._warm_attempts:
                    await sleep(self._warm_retry_seconds * attempt)
                continue
            if attempt > 1:
                _log.info(
                    "CloudflareSolver warm recovered for source %r on "
                    "attempt %d/%d (#153)",
                    key,
                    attempt,
                    self._warm_attempts,
                )
            return True
        _log.warning(
            "CloudflareSolver warm exhausted %d attempt(s) for source %r "
            "— disabling (D-33/#153)",
            self._warm_attempts,
            key,
            exc_info=last_exc,
        )
        return False

    # ``timeout`` here is the per-call Playwright operation budget (goto/
    # wait_for/evaluate each receive ``timeout`` in ms), NOT a cancellation
    # wrapper. ``asyncio.timeout`` is the wrong tool: it would cancel
    # mid-evaluate and leak an open page, defeating the explicit
    # ``finally: page.close()`` discipline.
    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — see method comment above
    ) -> Any:
        """Navigate to ``url`` in the warm browser context, run ``extract`` in the
        rendered DOM, and return its JSON-serializable result (Option A primitive).

        Sources whose encrypted-API endpoints require a per-request token we cannot
        mint statically (Comix's ``_=`` page-list token is bound to the chapter id
        AND minted by the same VM-obfuscated ``secure-*.js`` that does decryption)
        use this primitive: navigate to the live page, let its own JS handle
        token-mint + API call + decrypt + image-tag render, then read the rendered
        DOM via ``page.evaluate``. The httpx path STILL fetches the image bytes
        (CLAUDE.md "image fetch is NEVER through the browser") — this primitive is
        only for the manifest-resolution step.

        Concurrency: bounded by ``_browser_lock`` (an ``asyncio.Semaphore``
        with a small cap, ``Settings.cloudflare_fetch_concurrency`` default 3).
        Multiple ``fetch_via_browser`` calls
        can run in parallel against the same warm persistent context — the
        Playwright driver multiplexes pages cleanly and a humans-have-tabs-
        open fingerprint is benign — but the cap still bounds the
        simultaneous-page footprint. Was a 1-wide ``Lock`` before debug
        session ``comix-search-timeout`` (turned the search fan-out's 20s
        budget into sum-of-individual-navs and broke comix /search under
        variance).

        Dead-driver recovery (#54): if the underlying Playwright/Camoufox Node
        driver process crashes mid-fetch (Firefox handler bug — see
        :data:`_DEAD_DRIVER_MARKERS`), the cached ``BrowserContext`` becomes a
        zombie handle and every subsequent ``new_page`` / ``evaluate`` fails
        with "Connection closed while reading from the driver". This method
        catches that signal, calls :meth:`BrowserLifecycle.recycle_now` to drop
        the dead context, and retries the fetch exactly ONCE against a freshly
        launched context. A second crash surfaces as ``BrowserFetchError`` —
        the retry-once cap prevents hot-loops on a persistently broken driver.

        Args:
            url: The full URL to navigate to. The warm browser already carries
                cf_clearance + Laravel session for ``comix.to`` (Pitfall 1).
            extract: A JS function body returning a JSON-serializable value. The
                framework wraps it with ``async () => { ...extract... }`` so the
                body can ``await`` and use ``return`` directly.
            wait_for: Optional pre-evaluate wait. If it looks like a CSS selector
                (does not contain ``=>`` or ``return``), it's passed to
                ``page.wait_for_selector``; otherwise it's treated as a JS
                predicate body and passed to ``page.wait_for_function``.
            timeout: Seconds budget for goto + wait_for + evaluate.

        Returns:
            Whatever the ``extract`` body returns (passed through Playwright's
            JSON-serialization).

        Raises:
            BrowserFetchError: navigation failed, ``wait_for`` timed out, the
                JS evaluate threw, or the page could not be opened. Wraps the
                underlying exception so the SourceContext sees one type. If a
                dead-driver crash was detected, this is raised only after the
                single retry attempt also failed.
        """
        try:
            return await self._fetch_via_browser_once(
                url, extract=extract, wait_for=wait_for, timeout=timeout, attempt=1
            )
        except BrowserFetchError as exc:
            if not _looks_like_dead_driver(exc):
                raise
            # Driver died — drop the cached zombie context and retry once
            # against a freshly launched one. ``recycle_now`` also clears the
            # held Clearance (the cookies + UA are bound to the dead session),
            # so the next solve will run a fresh Cloudflare warm — that's
            # correct, not a regression. NEVER log the underlying exception
            # at INFO+ here: it has already been wrapped + logged once at the
            # call site; double-logging dead-driver crashes pollutes the
            # signal in nightly triage.
            _log.warning(
                "fetch_via_browser: dead-driver signal detected — recycling "
                "browser context and retrying once (url=%s)",
                url,
            )
            await self._lifecycle.recycle_now()
            return await self._fetch_via_browser_once(
                url, extract=extract, wait_for=wait_for, timeout=timeout, attempt=2
            )

    async def _browser_call(
        self,
        url: str,
        *,
        op: str,
        attempt: int,
        body: Callable[[Any], Awaitable[_BodyResult]],
    ) -> _BodyResult:
        """Shared ``_once``-worker scaffolding for ALL browser primitives (D-02).

        Owns the envelope that every ``fetch_via_browser*`` worker repeated
        verbatim: acquire the ``_browser_lock`` Semaphore slot; get the warm
        context (wrap failure → :class:`BrowserFetchError`); open ONE page
        (wrap failure → :class:`BrowserFetchError`); attach the #54 diagnostic
        loggers when enabled; ``perf_counter`` the interaction; run the caller's
        divergent ``body(page)`` closure; emit EXACTLY ONE ``browser`` ring event
        (``outcome="error"`` before re-raising a ``BrowserFetchError``, else
        ``outcome="ok"`` and return the result); and ``page.close()`` on every
        path. ``op`` is the metrics-event op string AND is woven into the wrap
        messages so each primitive's errors stay self-identifying. ``body`` is
        the ONLY thing that diverges across the one-shot / paginated / parallel-pages
        workers — it receives the open page and returns the worker's result
        (raising :class:`BrowserFetchError` on its own failures).

        #125 / 260605-e9a: the single ``_emit_browser`` per call is
        self-attributed to the source/request via contextvars and is STRICTLY
        additive — it never changes control flow / exception propagation /
        timing. The retry-once wrapper (#54) lives in the public method, not
        here; ``attempt`` is passed through for the metrics emit.
        """
        async with self._browser_lock:
            try:
                ctx = await self._lifecycle.get_context()
            except Exception as exc:  # noqa: BLE001
                raise BrowserFetchError(
                    f"browser context unavailable for {op}: {exc}"
                ) from exc
            try:
                page = await ctx.new_page()
            except Exception as exc:  # noqa: BLE001
                raise BrowserFetchError(f"could not open page for {op}: {exc}") from exc
            # #54 diagnostic instrumentation: attach pageerror/console/
            # requestfailed loggers BEFORE the body runs so we capture early
            # errors raised during navigation itself, not just post-load. Gated
            # on the per-solver flag (default OFF). Handlers die with the page
            # at the ``finally: page.close()`` below — no separate cleanup
            # needed.
            if self._log_browser_events:
                _attach_browser_event_loggers(page, nav_url=url)
            browser_start = time.perf_counter()
            try:
                result = await body(page)
            except BrowserFetchError as exc:
                _emit_browser(
                    op=op,
                    url=url,
                    outcome="error",
                    duration_ms=(time.perf_counter() - browser_start) * 1000.0,
                    attempt=attempt,
                    error=repr(exc),
                )
                raise
            else:
                _emit_browser(
                    op=op,
                    url=url,
                    outcome="ok",
                    duration_ms=(time.perf_counter() - browser_start) * 1000.0,
                    attempt=attempt,
                    error=None,
                )
                return result
            finally:
                with contextlib.suppress(Exception):
                    await page.close()

    async def _fetch_via_browser_once(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — same justification as fetch_via_browser
        attempt: int = 1,
    ) -> Any:
        """One attempt of the goto + wait_for + evaluate sequence (no retry).

        The interaction body is identical to the original :meth:`fetch_via_browser`
        (pre-#54); the lock/context/page/metrics/close envelope is owned by the
        shared :meth:`_browser_call` helper (D-02). Split out so the public method
        can wrap a single retry around it after detecting a dead-driver crash —
        the retry simply re-enters this method against a freshly launched context.
        """
        timeout_ms = int(timeout * 1000)

        async def _body(page: Any) -> Any:
            await self._goto_and_wait(page, url, wait_for, timeout_ms)
            return await _evaluate_guarded(
                page, "async () => { " + extract + " }", timeout
            )

        return await self._browser_call(
            url, op="fetch_via_browser", attempt=attempt, body=_body
        )

    async def _goto_and_wait(
        self, page: Any, url: str, wait_for: str | None, timeout_ms: int
    ) -> None:
        """Shared goto + optional ``wait_for`` sequence (one place, three callers).

        Navigates with ``wait_until="commit"`` (returns as soon as the response
        is committed — issue #20; the caller's ``wait_for`` is the meaningful
        readiness signal, so blocking goto on ``domcontentloaded`` first just
        adds 1–2s of overhead), then routes ``wait_for`` via :meth:`_apply_wait`
        to ``wait_for_function`` (JS predicate) vs ``wait_for_selector`` (CSS
        selector). Each step is wrapped so the underlying Playwright exception
        surfaces as a single :class:`BrowserFetchError`. Called by all three
        ``_once`` workers so the goto/wait logic lives in exactly one place —
        the one-shot read's behavior is byte-for-byte unchanged.
        """
        try:
            await page.goto(url, wait_until="commit", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise BrowserFetchError(f"goto {url!r} failed: {exc}") from exc
        await self._apply_wait(page, wait_for, timeout_ms)

    async def _apply_wait(
        self, page: Any, wait_for: str | None, timeout_ms: int
    ) -> None:
        """Route a ``wait_for`` to ``wait_for_function`` vs ``wait_for_selector``.

        Split out of :meth:`_goto_and_wait` (D-02) so all browser primitives share
        one wait-classification path for navigation readiness.
        ``wait_for=None`` is a no-op. Classifies via :func:`_looks_like_js_predicate`
        (JS predicate → ``wait_for_function``; CSS selector → ``wait_for_selector``)
        and wraps any Playwright failure as a single :class:`BrowserFetchError`.
        """
        if wait_for is None:
            return
        try:
            if _looks_like_js_predicate(wait_for):
                await page.wait_for_function(wait_for, timeout=timeout_ms)
            else:
                await page.wait_for_selector(wait_for, timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise BrowserFetchError(f"wait_for {wait_for!r} failed: {exc}") from exc

    # ``timeout`` is the per-call Playwright operation budget (goto/wait_for and
    # each evaluate receive ``timeout``), NOT a cancellation wrapper — same
    # justification as ``fetch_via_browser``.
    async def fetch_via_browser_paginated(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        next_selector: str,
        route_limit_rewrite: tuple[str, int] | None = None,
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — see comment above
    ) -> list[Any]:
        """Navigate ``url`` and walk its in-page Next control, accumulating the
        full deduped row list across pagination (generic, source-agnostic; #146).

        Like :meth:`fetch_via_browser` this drives the warm browser context and
        reads JSON-serializable rows out of the rendered DOM via ``extract`` —
        but instead of a single read it (a) optionally installs a Patchright
        ``page.route()`` limit-rewrite handler BEFORE goto, and (b) after the
        first paint, repeatedly runs ``extract``, merges any rows with an unseen
        ``id``, then JS-clicks ``next_selector`` and polls until the rendered
        id-set changes — until Next is absent/disabled, a click adds no new
        rows, or ``max_pages`` is hit. The ENTIRE walk runs inside ONE
        ``_browser_lock`` acquisition / ONE page (one goto, one wait) — there is
        no extra navigation and no second Cloudflare-clearance cost.

        Every site-specific value (the ``next_selector``, the
        ``route_limit_rewrite`` substring + target, the ``extract`` body, the
        ``wait_for``) is a CALLER argument — this framework method names no
        source. Detected by sources via ``hasattr`` (off-Protocol, D-41).

        Dead-driver recovery mirrors :meth:`fetch_via_browser`: a
        :func:`_looks_like_dead_driver` ``BrowserFetchError`` triggers ONE
        ``recycle_now`` + retry against a fresh context (same retry-once cap).

        Args:
            url: The full series/list URL to navigate to.
            extract: A JS function body returning a JSON-serializable list of
                rows; the framework wraps it as ``async () => { ...extract... }``
                so the body can ``await`` and ``return``. Rows are deduped by
                their ``id`` key.
            wait_for: Optional pre-walk readiness wait (CSS selector or JS
                predicate, routed exactly like :meth:`fetch_via_browser`).
            next_selector: CSS selector for the in-page Next control. The walk
                stops when it is absent or disabled (``disabled`` property or
                ``aria-disabled``).
            route_limit_rewrite: Optional ``(url_substring, target_limit)`` — a
                ``page.route()`` handler rewrites the ``limit=\\d+`` query value
                to ``target_limit`` on requests whose URL contains
                ``url_substring`` (so the first paint already requests a larger
                page); ``None`` installs no route.
            max_pages: Infinite-loop guard — walking this many pages stops the
                loop and logs a warning. NOT a feature cap.
            timeout: Seconds budget applied to goto/wait_for and each evaluate.

        Returns:
            The merged, deduped list of rows across every walked page, in
            first-seen order.

        Raises:
            BrowserFetchError: navigation/wait/evaluate failed or the context
                could not be acquired (after the single dead-driver retry).
        """
        try:
            return await self._paginate_via_browser_once(
                url,
                extract=extract,
                wait_for=wait_for,
                next_selector=next_selector,
                route_limit_rewrite=route_limit_rewrite,
                max_pages=max_pages,
                timeout=timeout,
                attempt=1,
            )
        except BrowserFetchError as exc:
            if not _looks_like_dead_driver(exc):
                raise
            _log.warning(
                "fetch_via_browser_paginated: dead-driver signal detected — "
                "recycling browser context and retrying once (url=%s)",
                url,
            )
            await self._lifecycle.recycle_now()
            return await self._paginate_via_browser_once(
                url,
                extract=extract,
                wait_for=wait_for,
                next_selector=next_selector,
                route_limit_rewrite=route_limit_rewrite,
                max_pages=max_pages,
                timeout=timeout,
                attempt=2,
            )

    async def _paginate_via_browser_once(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None,
        next_selector: str,
        route_limit_rewrite: tuple[str, int] | None,
        max_pages: int,
        timeout: float,  # noqa: ASYNC109 — same justification as fetch_via_browser
        attempt: int,
    ) -> list[Any]:
        """One attempt of the paginated walk (route + goto + wait + Next-walk).

        The lock/context/page/metrics/close envelope is owned by the shared
        :meth:`_browser_call` helper (D-02); the divergent body installs the
        optional route handler BEFORE goto, navigates, and walks the Next
        control. Emits exactly one ``browser`` ring event
        (op=``fetch_via_browser_paginated``). Split out so the public method can
        wrap a single dead-driver retry around it.
        """
        timeout_ms = int(timeout * 1000)

        async def _body(page: Any) -> list[Any]:
            # Install the route limit-rewrite BEFORE goto so the first paint's
            # own requests are rewritten (route() must be in place before the
            # navigation issues any request).
            if route_limit_rewrite is not None:
                url_substring, target_limit = route_limit_rewrite
                handler = _build_limit_rewrite_route_handler(
                    url_substring, target_limit
                )
                try:
                    await page.route("**/*", handler)
                except Exception as exc:  # noqa: BLE001
                    raise BrowserFetchError(
                        f"page.route install failed: {exc}"
                    ) from exc
            await self._goto_and_wait(page, url, wait_for, timeout_ms)
            return await self._walk_next(
                page,
                extract=extract,
                next_selector=next_selector,
                max_pages=max_pages,
                timeout=timeout,
                url=url,
            )

        return await self._browser_call(
            url, op="fetch_via_browser_paginated", attempt=attempt, body=_body
        )

    async def _walk_next(
        self,
        page: Any,
        *,
        extract: str,
        next_selector: str,
        max_pages: int,
        timeout: float,  # noqa: ASYNC109 — per-evaluate budget, not a cancel wrapper
        url: str,
    ) -> list[Any]:
        """Walk the in-page Next control on an already-navigated ``page``.

        Runs entirely within the single page lifecycle the caller set up — no
        extra goto, no second ``_browser_lock`` acquisition. Each iteration:
        run ``extract`` and merge unseen-``id`` rows; probe ``next_selector``
        (stop A if absent/disabled); snapshot the id-set, JS-click Next, then
        poll the bounded budget until the id-set changes (stop B if it never
        does); guard at ``max_pages`` (stop C — log a warning). Returns the
        merged, deduped accumulator in first-seen order.

        The Next selector is injected into the probe/click scripts as a
        JSON-encoded string literal (``json.dumps``) so a selector containing
        quotes is safe — no raw concatenation, no injection surface (T-eqm-03).
        """
        selector_literal = json.dumps(next_selector)
        extract_script = "async () => { " + extract + " }"
        # Probe: present + disabled state of the Next control (``disabled``
        # property OR ``aria-disabled`` attribute).
        probe_script = (
            "() => { const el = document.querySelector("
            + selector_literal
            + "); if (!el) return { present: false, disabled: false };"
            " const disabled = !!el.disabled"
            " || el.getAttribute('aria-disabled') === 'true';"
            " return { present: true, disabled: disabled }; }"
        )
        # JS-click bypasses an invisible overlay that could swallow a real click.
        click_script = (
            "() => { const b = document.querySelector("
            + selector_literal
            + "); if (b) b.click(); }"
        )

        accumulator: list[Any] = []
        seen: set[Any] = set()

        def _ids(rows: Any) -> set[Any]:
            if not isinstance(rows, list):
                return set()
            return {r.get("id") for r in rows if isinstance(r, dict)}

        def _merge(rows: Any) -> None:
            if not isinstance(rows, list):
                return
            for row in rows:
                rid = row.get("id") if isinstance(row, dict) else None
                if rid in seen:
                    continue
                seen.add(rid)
                accumulator.append(row)

        page_count = 1
        while True:
            rows = await _evaluate_guarded(page, extract_script, timeout)
            _merge(rows)

            # Stop C (infinite-loop guard) — checked AFTER the merge so the page
            # we reached is always accumulated before we stop.
            if page_count >= max_pages:
                _log.warning(
                    "fetch_via_browser_paginated: hit max_pages=%d on %s "
                    "(infinite-loop guard — stopping the Next-walk)",
                    max_pages,
                    url,
                )
                break

            # Stop A — Next absent or disabled.
            state = await _evaluate_guarded(page, probe_script, timeout)
            if (
                not isinstance(state, dict)
                or not state.get("present")
                or state.get("disabled")
            ):
                break

            before = _ids(rows)
            await _evaluate_guarded(page, click_script, timeout)

            # Bounded, non-blocking poll for the rendered id-set to change.
            changed = False
            for _ in range(_PAGINATE_POLL_TICKS):
                await asyncio.sleep(_PAGINATE_POLL_INTERVAL)
                if _ids(await _evaluate_guarded(page, extract_script, timeout)) != (
                    before
                ):
                    changed = True
                    break
            if not changed:
                # Stop B — the click produced no new rows within the budget.
                break

            page_count += 1

        return accumulator

    # ``timeout`` is the per-call Playwright operation budget (goto/wait_for and
    # each evaluate receive ``timeout``), NOT a cancellation wrapper — same
    # justification as ``fetch_via_browser``.
    async def fetch_via_browser_parallel_pages(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        last_page_selector: str,
        page_param: str,
        route_rewrite: tuple[str, int],
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — see comment above
    ) -> list[Any]:
        """Navigate ``url``, discover the page count P via the caller's "Last page"
        button, then fan pages 1..P out CONCURRENTLY and merge the deduped row list
        (generic, source-agnostic; #222, spike 014).

        The third sibling browser primitive (alongside :meth:`fetch_via_browser`,
        :meth:`fetch_via_browser_paginated`). It
        replaces the SEQUENTIAL in-page Next-walk of
        :meth:`fetch_via_browser_paginated` with a PARALLEL browser-page fan-out for
        sites whose long lists paginate via a ``?page=N`` URL the SPA reads on first
        paint (spike 014 proved this recovers the byte-identical deduped newest-first
        list ~3.5-5.9x faster). The walk has three phases:

        1. **Discovery** — install the LIMIT-ONLY ``route_rewrite`` route (so the
           "Last page" button computes P at the SAME limit the fan-out uses — the
           load-bearing consistency point), ``goto``, then probe
           ``last_page_selector``. Absent/disabled → P=1; else JS-click it and read
           ``?{page_param}=N`` off the synced URL. P is clamped to ``max_pages``.
        2. **Fan-out** — ``asyncio.gather`` one single-page browser call per page
           1..P, each pinning its own ``page``+``limit`` via ``page.route()`` and
           bounded by the EXISTING ``_browser_lock`` semaphore (no second gate). A
           page that fails OR yields no rows FAILS CLOSED (raises) — the first
           exception is re-raised verbatim, never a partial list.
        3. **Merge** — dedup by ``id``, first-seen (newest-first) order.

        Every site-specific value (the ``last_page_selector``, the ``page_param``,
        the ``route_rewrite`` substring + target, the ``extract`` body, the
        ``wait_for``) is a CALLER argument — this framework method names no source.
        Detected by sources via ``hasattr`` (off-Protocol, D-41).

        Dead-driver recovery mirrors :meth:`fetch_via_browser`: a
        :func:`_looks_like_dead_driver` ``BrowserFetchError`` triggers ONE
        ``recycle_now`` + retry of the WHOLE op (re-discover + re-fan-out, since the
        shared context is dead) against a fresh context (same retry-once cap).

        Args:
            url: The full series/list URL to navigate to.
            extract: A JS function body returning a JSON-serializable list of rows;
                wrapped as ``async () => { ...extract... }``. Rows dedup by ``id``.
            wait_for: Optional pre-read readiness wait (CSS selector or JS
                predicate, routed exactly like :meth:`fetch_via_browser`).
            last_page_selector: CSS selector for the "Last page" control. Absent or
                disabled means a single page (P=1).
            page_param: The query param name the SPA reads the page index from
                (e.g. ``"page"``) — used BOTH to parse the synced Last-page URL and
                to pin each fan-out page's request.
            route_rewrite: ``(url_substring, target_limit)`` — the ``page.route()``
                rewrite applied to requests whose URL contains ``url_substring``.
            max_pages: Infinite-loop guard — P is clamped to this (logs a warning).
            timeout: Seconds budget applied to goto/wait_for and each evaluate.

        Returns:
            The merged, deduped list of rows across pages 1..P, first-seen order.

        Raises:
            BrowserFetchError: navigation/wait/evaluate failed, a fan-out page
                yielded no rows (fail-closed), or the context could not be acquired
                (after the single dead-driver retry).
        """
        try:
            return await self._parallel_pages_once(
                url,
                extract=extract,
                wait_for=wait_for,
                last_page_selector=last_page_selector,
                page_param=page_param,
                route_rewrite=route_rewrite,
                max_pages=max_pages,
                timeout=timeout,
                attempt=1,
            )
        except BrowserFetchError as exc:
            if not _looks_like_dead_driver(exc):
                raise
            _log.warning(
                "fetch_via_browser_parallel_pages: dead-driver signal detected — "
                "recycling browser context and retrying once (url=%s)",
                url,
            )
            await self._lifecycle.recycle_now()
            return await self._parallel_pages_once(
                url,
                extract=extract,
                wait_for=wait_for,
                last_page_selector=last_page_selector,
                page_param=page_param,
                route_rewrite=route_rewrite,
                max_pages=max_pages,
                timeout=timeout,
                attempt=2,
            )

    async def _parallel_pages_once(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None,
        last_page_selector: str,
        page_param: str,
        route_rewrite: tuple[str, int],
        max_pages: int,
        timeout: float,  # noqa: ASYNC109 — same justification as fetch_via_browser
        attempt: int,
    ) -> list[Any]:
        """One attempt of discover-P → parallel fan-out → merge (no retry).

        The dead-driver retry wraps the WHOLE op in the public method, so this
        re-runs discovery AND the fan-out against the (possibly freshly relaunched)
        context. Each phase is its own ``_browser_call`` (op=
        ``fetch_via_browser_parallel_pages``); the fan-out's ``return_exceptions``
        gather lets all tasks settle (no orphan-task warnings) before the first
        exception is re-raised verbatim — never a partial list.
        """
        page_count = await self._discover_page_count(
            url,
            wait_for=wait_for,
            last_page_selector=last_page_selector,
            page_param=page_param,
            route_rewrite=route_rewrite,
            max_pages=max_pages,
            timeout=timeout,
            attempt=attempt,
        )
        # FAN-OUT: one single-page browser call per page, gathered concurrently.
        # Each ``_fetch_one_page`` acquires the EXISTING ``_browser_lock`` inside
        # ``_browser_call``, so the gather self-throttles to ``fetch_concurrency``
        # globally — NO second semaphore. ``return_exceptions=True`` lets every
        # task settle so a failure on one page does not orphan the others.
        settled = await asyncio.gather(
            *[
                self._fetch_one_page(
                    url,
                    target=n,
                    extract=extract,
                    wait_for=wait_for,
                    route_rewrite=route_rewrite,
                    page_param=page_param,
                    timeout=timeout,
                    attempt=attempt,
                )
                for n in range(1, page_count + 1)
            ],
            return_exceptions=True,
        )
        # FAIL-CLOSED: re-raise the FIRST exception verbatim (preserving its
        # ``__cause__`` so the public wrapper's dead-driver check still works);
        # never return a partial list. The narrowed ``pages`` list is then merge-safe.
        pages: list[list[Any]] = []
        for result in settled:
            if isinstance(result, BaseException):
                raise result
            pages.append(result)
        # MERGE: dedup by ``id``, first-seen-wins, in page order (gather preserves
        # the launch order) — identical accumulator shape to ``_walk_next``.
        accumulator: list[Any] = []
        seen: set[Any] = set()
        for rows in pages:
            for row in rows:
                rid = row.get("id") if isinstance(row, dict) else None
                if rid in seen:
                    continue
                seen.add(rid)
                accumulator.append(row)
        return accumulator

    async def _discover_page_count(
        self,
        url: str,
        *,
        wait_for: str | None,
        last_page_selector: str,
        page_param: str,
        route_rewrite: tuple[str, int],
        max_pages: int,
        timeout: float,  # noqa: ASYNC109 — per-evaluate budget, not a cancel wrapper
        attempt: int,
    ) -> int:
        """Discover the page count P via the "Last page" button + ``?page=N`` sync.

        Installs the LIMIT-ONLY ``route_rewrite`` route BEFORE goto so the Last
        button computes P at the SAME ``limit`` the fan-out uses (consistency
        point), then probes ``last_page_selector``: absent/disabled → P=1; else
        JS-clicks it and bounded-polls ``page.url`` for ``?{page_param}=N``. P is
        ``max(1, parsed)`` (or 1 if never synced), clamped to ``max_pages``. The
        selector is ``json.dumps``-encoded into the probe/click JS (T-u7f-02 — no
        raw concatenation), same discipline as :meth:`_walk_next`.
        """
        timeout_ms = int(timeout * 1000)
        selector_literal = json.dumps(last_page_selector)
        probe_script = (
            "() => { const el = document.querySelector("
            + selector_literal
            + "); if (!el) return { present: false, disabled: false };"
            " const disabled = !!el.disabled"
            " || el.getAttribute('aria-disabled') === 'true';"
            " return { present: true, disabled: disabled }; }"
        )
        click_script = (
            "() => { const b = document.querySelector("
            + selector_literal
            + "); if (b) b.click(); }"
        )
        page_re = re.compile(rf"[?&]{re.escape(page_param)}=(\d+)")
        url_substring, target_limit = route_rewrite

        async def _body(page: Any) -> int:
            # LIMIT-ONLY route (no page pin) BEFORE goto — the Last button must
            # compute P at the same limit the fan-out renders.
            handler = _build_limit_rewrite_route_handler(url_substring, target_limit)
            try:
                await page.route("**/*", handler)
            except Exception as exc:  # noqa: BLE001
                raise BrowserFetchError(f"page.route install failed: {exc}") from exc
            await self._goto_and_wait(page, url, wait_for, timeout_ms)
            state = await _evaluate_guarded(page, probe_script, timeout)
            if (
                not isinstance(state, dict)
                or not state.get("present")
                or state.get("disabled")
            ):
                return 1  # Last absent or disabled → single page
            await _evaluate_guarded(page, click_script, timeout)
            # Bounded, non-blocking poll for the synced ``?page=N`` URL. ``page.url``
            # is the property (read directly, not awaited).
            found: int | None = None
            for _ in range(_PAGINATE_POLL_TICKS):
                match = page_re.search(page.url)
                if match:
                    found = int(match.group(1))
                    break
                await asyncio.sleep(_PAGINATE_POLL_INTERVAL)
            page_count = max(1, found) if found is not None else 1
            if page_count > max_pages:
                _log.warning(
                    "fetch_via_browser_parallel_pages: discovered P=%d > max_pages="
                    "%d on %s — clamping (infinite-loop guard)",
                    page_count,
                    max_pages,
                    url,
                )
                page_count = max_pages
            return page_count

        return await self._browser_call(
            url, op="fetch_via_browser_parallel_pages", attempt=attempt, body=_body
        )

    async def _fetch_one_page(
        self,
        url: str,
        *,
        target: int,
        extract: str,
        wait_for: str | None,
        route_rewrite: tuple[str, int],
        page_param: str,
        timeout: float,  # noqa: ASYNC109 — same justification as fetch_via_browser
        attempt: int,
    ) -> list[Any]:
        """Render ONE fan-out page pinned to ``target`` and return its rows.

        Installs the page+limit rewrite route (pinning ``page_param`` to ``target``
        and ``limit`` to the ``route_rewrite`` target) BEFORE goto, navigates, runs
        the extract, and FAILS CLOSED — raises :class:`BrowserFetchError` if the
        result is not a NON-EMPTY list (a blank page would silently drop chapters).
        Its own ``_browser_call`` acquires the EXISTING ``_browser_lock`` so the
        parent gather self-throttles to ``fetch_concurrency``.
        """
        timeout_ms = int(timeout * 1000)
        url_substring, target_limit = route_rewrite

        async def _body(page: Any) -> list[Any]:
            handler = _build_page_limit_rewrite_route_handler(
                url_substring, target, target_limit, page_param
            )
            try:
                await page.route("**/*", handler)
            except Exception as exc:  # noqa: BLE001
                raise BrowserFetchError(f"page.route install failed: {exc}") from exc
            await self._goto_and_wait(page, url, wait_for, timeout_ms)
            rows = await _evaluate_guarded(
                page, "async () => { " + extract + " }", timeout
            )
            if not isinstance(rows, list) or not rows:
                raise BrowserFetchError(f"parallel page {target} returned no rows")
            return rows

        return await self._browser_call(
            url, op="fetch_via_browser_parallel_pages", attempt=attempt, body=_body
        )

    async def aclose(self) -> None:
        """Tear the bounded lifecycle down + stop playwright (Pitfall 4)."""
        await self._lifecycle.aclose()
        pw = self._playwright
        self._playwright = None
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()
        # Stop the Xvfb display last (after the browser using it is gone).
        display = self._virtual_display
        self._virtual_display = None
        if display is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(display.stop)

    # ─────────────────────── real launch/solve closures ───────────────────────

    def _start_virtual_display(self) -> None:
        """Start an Xvfb virtual framebuffer for a headed launch on a display-less
        Linux host. Idempotent; no-op when headless, off-Linux, or a ``DISPLAY``
        already exists (a real desktop, or the operator already runs under
        ``xvfb-run``). Lazy import so the dependency is only touched on the headed
        Linux-server path. Blocking (spawns Xvfb) — callers offload via
        ``asyncio.to_thread``.

        Headed Chromium clears Cloudflare's encrypted challenge on datacenter IPs
        where headless Chrome is fingerprint-flagged at the binary level
        (comix-parallel-engine-probe / #35). ``pyvirtualdisplay`` sets ``DISPLAY``
        on start, which the headed browser launch then inherits.
        """
        if self._headless or self._virtual_display is not None:
            return
        if sys.platform != "linux" or os.environ.get("DISPLAY"):
            return
        from pyvirtualdisplay import (  # type: ignore[attr-defined]  # noqa: PLC0415
            Display,
        )

        display = Display(visible=False, size=(1920, 1080))
        display.start()
        self._virtual_display = display
        _log.info("started Xvfb virtual display for headed browser launch")

    async def _launch_patchright_context(self) -> Any:
        """Launch a Patchright persistent context (lazy import — D-42).

        Uses ``launch_persistent_context`` with the on-disk ``user_data_dir`` so the
        cf_clearance persists across restarts (D-34). NO custom UA/headers/fingerprint
        injection (Anti-Patterns — re-introduces detectable inconsistencies). The
        default engine everywhere (dev + CI + prod) since 2026-06-01 — it is the only
        engine that runs concurrent CF navigations on one warm context
        (comix-parallel-engine-probe). Patchright passes Cloudflare reliably on
        residential IPs but is flagged by Cloudflare's encrypted tier on cloud Linux
        runners (#35), where the headed-on-Xvfb path is the mitigation.
        """
        import asyncio  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from patchright.async_api import (  # noqa: PLC0415 — lazy (D-42)
            async_playwright,
        )

        # Create the persistent-context dir owner-only (0o700 best-effort, Linux
        # prod / V3); it holds the cf_clearance token (T-04-12) — never logged. The
        # blocking mkdir is offloaded off the event loop (ruff ASYNC).
        await asyncio.to_thread(
            Path(self._user_data_dir).mkdir, mode=0o700, parents=True, exist_ok=True
        )
        # Headed-on-display-less-Linux (datacenter/CI) needs an Xvfb display so
        # the browser can launch headed — the config that clears CF where headless
        # is flagged (comix-parallel-engine-probe). No-op when headless/off-Linux.
        await asyncio.to_thread(self._start_virtual_display)

        self._playwright = await async_playwright().start()
        # Build kwargs conditionally: pass ``proxy=`` ONLY when configured so the
        # no-proxy path is byte-for-byte the historic call (some launchers treat
        # an explicit ``proxy=None`` differently — the regression contract #65
        # requires the kwarg be ABSENT, not None).
        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "no_viewport": True,
        }
        if self._proxy is not None:
            launch_kwargs["proxy"] = self._proxy
        return await self._playwright.chromium.launch_persistent_context(
            self._user_data_dir,
            **launch_kwargs,
        )

    async def _launch_camoufox_context(self) -> Any:
        """Launch a Camoufox (Firefox-based) persistent context (lazy import — D-42).

        Camoufox wraps Playwright's Firefox driver with a C++ fingerprint spoof; per
        CLAUDE.md it is the strongest open-source stealth in 2026 (~0% headless
        detection). It is the opt-in fallback engine (Patchright is the default since
        2026-06-01) for hosts where Chromium's fingerprint is flagged; it cannot run
        concurrent CF navigations (Firefox-only failure modes such as issue #54), so
        it must be paired with ``fetch_concurrency=1``. First-time use requires
        ``uv run camoufox fetch`` to download its Firefox binary (~200 MB, one-time,
        into a Camoufox-managed cache dir).

        Camoufox uses ``AsyncNewBrowser(playwright, persistent_context=True, ...)``
        to return a ``BrowserContext`` that matches the shape Patchright's
        ``launch_persistent_context`` returns — both back the lifecycle's
        injection seam identically, so the solve closure stays engine-agnostic.

        ``user_data_dir`` carries cf_clearance across restarts (D-34), same as the
        Patchright closure. NO custom UA/headers (Camoufox handles fingerprint
        spoofing internally; injecting our own would re-introduce detectable
        inconsistencies — same Anti-Patterns note as Patchright).
        """
        import asyncio  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from camoufox.async_api import (  # noqa: PLC0415 — lazy (D-42)
            AsyncNewBrowser,
        )
        from playwright.async_api import (  # noqa: PLC0415
            async_playwright,
        )

        await asyncio.to_thread(
            Path(self._user_data_dir).mkdir, mode=0o700, parents=True, exist_ok=True
        )
        await asyncio.to_thread(self._start_virtual_display)

        self._playwright = await async_playwright().start()
        # ``persistent_context=True`` returns a BrowserContext (same shape as
        # Patchright's launch_persistent_context); Camoufox routes its
        # ``user_data_dir`` through Playwright's launch_persistent_context under
        # the hood. ``no_viewport=True`` matches the Patchright launch for
        # parity — Cloudflare's encrypted tier checks viewport-derived signals.
        # Same conditional-proxy discipline as the patchright closure (#65): the
        # ``proxy=`` kwarg is ABSENT when unconfigured (not ``None``) so the
        # no-proxy launch is byte-for-byte unchanged.
        launch_kwargs: dict[str, Any] = {
            "persistent_context": True,
            "user_data_dir": self._user_data_dir,
            "headless": self._headless,
            "no_viewport": True,
        }
        if self._proxy is not None:
            launch_kwargs["proxy"] = self._proxy
        return await AsyncNewBrowser(self._playwright, **launch_kwargs)

    async def _solve_keyed(
        self, context: Any, key: str, *, force: bool = False
    ) -> Clearance:
        """Resolve ``key`` to its per-domain challenge URL and solve there (#88).

        The lifecycle's solve closure is this method (not ``_solve_real``) so the
        per-key challenge URL is bound at solve time. ``key`` is the
        ``source_key``; its URL comes from the ``challenge_urls`` map, falling
        back to the constructor ``challenge_url`` default when absent. ``force``
        marks a D-35 re-solve (the on-disk cf_clearance was just rejected) so
        ``_solve_real`` discards the stale cookie before re-navigating.
        """
        url = self._challenge_urls.get(key) or self._challenge_url
        return await self._solve_real(context, url, force=force)

    async def _solve_real(
        self, context: Any, challenge_url: str, *, force: bool = False
    ) -> Clearance:
        """Solve the challenge on the live context; capture cf_clearance + UA.

        ``challenge_url`` is supplied by :meth:`_solve_keyed` from the per-domain
        ``challenge_urls`` map (#88), so each cloudflare-gated source solves
        against its OWN host and the cookie scoping (``_belongs_to_host``)
        captures only that host's cookies.

        Engine-agnostic: the polling-cookies + capture-UA logic depends only on
        Playwright's ``BrowserContext`` API surface, which both Patchright (Chromium)
        and Camoufox (Firefox) expose identically. Browser work runs on the
        Playwright async API (already off the event loop). The image fetch is NEVER
        done through the browser (CLAUDE.md) — only the token capture is.
        """
        challenge_host = (urlparse(challenge_url).hostname or "").lower()
        if force:
            # D-35 forced re-solve (debug comix-recent-403): this path runs ONLY
            # because a request just got a CF challenge — i.e. the cf_clearance held
            # for this host (loaded from the persistent ``cloudflare-userdata`` dir)
            # was REJECTED by Cloudflare. If we leave it in the jar, the poll loop
            # below short-circuits on the stale cookie the instant we navigate and
            # hands back a "clearance" we never re-earned (the false-positive
            # "captured clearance (1 cookies)" that masked this bug). Drop it FIRST,
            # host-scoped, so the loop genuinely waits for a fresh cookie. Scoping by
            # the cookie's own domain keeps any OTHER cloudflare domain's clearance on
            # the shared warm context intact (#88).
            stale = await context.cookies()
            stale_domains = {
                c.get("domain")
                for c in stale
                if c.get("name") == "cf_clearance"
                and _belongs_to_host(c, challenge_host)
            }
            for dom in stale_domains:
                with contextlib.suppress(Exception):
                    await context.clear_cookies(name="cf_clearance", domain=dom)
        page = await context.new_page()
        try:
            await page.goto(challenge_url, wait_until="domcontentloaded")
            # Wait for the challenge to clear: poll context.cookies() until the
            # cf_clearance cookie appears. We CANNOT use page.wait_for_function on
            # ``document.cookie.includes('cf_clearance')`` because Cloudflare sets
            # cf_clearance with httpOnly=true, which is invisible to client-side
            # JavaScript by design (verified empirically against comix.to, recon
            # 2026-05-30). The cookie IS readable via CDP (context.cookies()), so
            # we poll that from Python instead.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 60.0
            # Scope the wait to THIS challenge's host (#88, PR#90 review). The ONE
            # shared warm context can now hold cf_clearance cookies for SEVERAL
            # domains at once (each cloudflare-gated source solves on the same
            # context). A host-agnostic ``any(name == "cf_clearance")`` would let
            # solving domain B return the instant domain A's cookie is present in
            # the jar — capturing an EMPTY host-scoped cookie set for B and handing
            # B a clearance it never actually solved. Gate the break on a
            # cf_clearance that ``_belongs_to_host`` the host we navigated
            # (``challenge_host`` computed above, also used by the forced-re-solve
            # stale-cookie purge).
            jar: list[dict[str, Any]] = []
            while True:
                jar = await context.cookies()
                if any(
                    c.get("name") == "cf_clearance"
                    and _belongs_to_host(c, challenge_host)
                    for c in jar
                ):
                    break
                if loop.time() > deadline:
                    raise TimeoutError(
                        "cf_clearance not captured within 60s — Cloudflare may "
                        "have escalated; consider the Camoufox escalation "
                        "(deferred per RESEARCH.md / D-42)."
                    )
                await asyncio.sleep(0.5)
            # The EXACT UA the cookie is bound to — captured from the SAME session
            # (Pitfall 1 / D-40).
            ua = await page.evaluate("navigator.userAgent")
            # Capture ALL cookies bound to the challenge URL's host (or any parent
            # domain thereof). Scoping by domain captures whatever the gated host
            # needs (cf_clearance plus any app/session cookie set during the solve)
            # and naturally excludes cross-site ad-tracking cookies the solver may
            # pick up incidentally. NOTE: a 2026-05-30 recon believed Comix required
            # BOTH cf_clearance AND a Laravel ``session`` cookie, but re-verified
            # 2026-06-07 (debug comix-recent-403) a HEADED solve's cf_clearance ALONE
            # returns 200 from /api/v1/manga — comix.to no longer sets/needs the
            # session cookie, and a 1-cookie capture is healthy. The real failure mode
            # is a HEADLESS solve never earning cf_clearance at all (see config.py /
            # Dockerfile: the image runs headed because comix.to blocks headless).
            cookies = {
                c["name"]: c["value"]
                for c in jar
                if _belongs_to_host(c, challenge_host)
            }
            # Never log the cookie value (T-04-12) — log the solve event only.
            _log.info("CloudflareSolver captured clearance (%d cookies)", len(cookies))
            return Clearance(cookies=cookies, user_agent=ua)
        finally:
            with contextlib.suppress(Exception):
                await page.close()
