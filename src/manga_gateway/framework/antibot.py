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

Engine selection (#35 / #40): the underlying browser is selected via the
``engine`` parameter (``"camoufox"`` Firefox-based, default; ``"patchright"``
Chromium-based, opt-in). Camoufox is the single dev/CI/prod default since #40 —
the dev/prod engine split meant Camoufox-only failure modes (e.g. issue #54:
Playwright Firefox handler crash on undefined pageError.location) went unseen
locally and surfaced only in nightly triage. Patchright remains an opt-in
escalation, historically friendlier to residential IPs but flagged by
Cloudflare's encrypted tier on cloud Linux runners. Both engines back the SAME
``AntiBotSolver`` interface — the swap is a single constructor argument with
no rewrite (CLAUDE.md "keep the browser behind an interface so this is a
config flip"). The launch closure is selected at ``__init__`` and remains LAZY
(the heavy import happens only on first solve), so neither browser binary is
touched by the deterministic gate (D-42).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlparse

from ..metrics.collector import get_collector
from .solver_lifecycle import BrowserLifecycle

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = logging.getLogger("manga_gateway")

# Default URL the solver navigates to for cf_clearance acquisition. Overridden
# by the application wiring (app.py lifespan) from the concrete source's
# metadata — the framework solver itself never names a host. The framework
# default exists only so unit tests can spin up a CloudflareSolver without
# naming a host. Pinned by Plan 04-04 live recon when the application supplies
# a real source.
_DEFAULT_CHALLENGE_URL = "https://example.invalid/"

# Supported anti-bot browser engines (#35). The selector lives on
# ``Settings.cloudflare_engine`` and is forwarded into ``CloudflareSolver``.
# Camoufox is the default everywhere (dev + CI + prod) so engine-specific
# failure modes surface locally instead of only in nightly triage (#40 /
# #54). Patchright is the opt-in escalation.
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
    """Resolves a per-source anti-bot challenge into a reusable clearance."""

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
        engine: AntibotEngine = "patchright",
        log_browser_events: bool = False,
        proxy: dict[str, str] | None = None,
        lifecycle: BrowserLifecycle | None = None,
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
        self._engine: AntibotEngine = engine
        # #54 diagnostic: when True, attach pageerror/console/requestfailed
        # loggers to every page opened by ``_fetch_via_browser_once`` so the
        # JS throw site behind the Playwright Firefox handler crash can be
        # captured under nightly conditions. OFF by default — log volume
        # and per-page overhead are too high for production. Toggled via
        # ``GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1`` through Settings.
        self._log_browser_events = log_browser_events
        # Tests inject a lifecycle wrapping a MOCKED browser; production builds one
        # wrapping the real lazy-launch/solve closures. The launch closure is
        # selected by ``engine`` (Camoufox default since #40; Patchright opt-in).
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
        self, source_key: str, *, force_resolve: bool = False
    ) -> Clearance | None:
        """Return clearance for ``source_key`` (``None`` for non-cloudflare keys).

        ``force_resolve`` (internal, D-35) skips the held clearance and runs a fresh
        solve. It is NOT part of the ``AntiBotSolver`` Protocol (D-41).
        """
        if source_key not in self._cloudflare_keys:
            return None  # MangaDex et al. — no clearance needed
        return await self._lifecycle.solve(source_key, force=force_resolve)

    async def warm(self) -> list[str]:
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
        """
        self._lifecycle.start_recycle_watchdog()
        failed: list[str] = []
        for key in self._cloudflare_keys:
            try:
                await self.get_clearance(key)
            except Exception:  # noqa: BLE001 — isolate per-domain warm failures
                _log.warning("CloudflareSolver warm failed for source %r (D-33)", key)
                failed.append(key)
        return failed

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
        with a small cap, default 5). Multiple ``fetch_via_browser`` calls
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

        The body is identical to the original :meth:`fetch_via_browser` (pre-#54).
        Split out so the public method can wrap a single retry around it after
        detecting a dead-driver crash — the retry simply re-enters this method
        against a freshly launched context. Acquires a ``_browser_lock``
        Semaphore slot so retries re-bound correctly against any concurrent
        ``fetch_via_browser`` call.
        """
        async with self._browser_lock:
            try:
                ctx = await self._lifecycle.get_context()
            except Exception as exc:  # noqa: BLE001
                raise BrowserFetchError(
                    f"browser context unavailable for fetch: {exc}"
                ) from exc
            try:
                page = await ctx.new_page()
            except Exception as exc:  # noqa: BLE001
                raise BrowserFetchError(
                    f"could not open page for fetch_via_browser: {exc}"
                ) from exc
            # #54 diagnostic instrumentation: attach pageerror/console/
            # requestfailed loggers BEFORE goto so we capture early errors
            # raised during navigation itself, not just post-load. Gated on
            # the per-solver flag (default OFF). Handlers die with the page
            # at the ``finally: page.close()`` below — no separate cleanup
            # needed.
            if self._log_browser_events:
                _attach_browser_event_loggers(page, nav_url=url)
            timeout_ms = int(timeout * 1000)
            # #125 / 260605-e9a: wrap the goto + wait_for + evaluate sequence in
            # perf_counter and emit exactly ONE ``browser`` ring event per call
            # (kind=browser), self-attributed to the source/request via contextvars.
            # outcome "ok" on success, "error" on any BrowserFetchError (emitted in
            # the except before re-raising). STRICTLY additive — never changes
            # control flow / exception propagation / timing.
            browser_start = time.perf_counter()
            try:
                try:
                    # ``wait_until="commit"`` returns as soon as the response
                    # is committed (issue #20). The caller's ``wait_for``
                    # selector / predicate is the meaningful readiness signal —
                    # blocking goto on ``domcontentloaded`` first adds 1–2s of
                    # pure overhead since the scaffold wait already covers DOM
                    # readiness.
                    await page.goto(url, wait_until="commit", timeout=timeout_ms)
                except Exception as exc:  # noqa: BLE001
                    raise BrowserFetchError(f"goto {url!r} failed: {exc}") from exc
                if wait_for is not None:
                    try:
                        if _looks_like_js_predicate(wait_for):
                            await page.wait_for_function(wait_for, timeout=timeout_ms)
                        else:
                            await page.wait_for_selector(wait_for, timeout=timeout_ms)
                    except Exception as exc:  # noqa: BLE001
                        raise BrowserFetchError(
                            f"wait_for {wait_for!r} failed: {exc}"
                        ) from exc
                try:
                    result = await asyncio.wait_for(
                        page.evaluate("async () => { " + extract + " }"),
                        timeout=timeout,
                    )
                except TimeoutError as exc:
                    raise BrowserFetchError(
                        f"page.evaluate timed out after {timeout}s"
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    raise BrowserFetchError(f"page.evaluate failed: {exc}") from exc
            except BrowserFetchError as exc:
                _emit_browser(
                    op="fetch_via_browser",
                    url=url,
                    outcome="error",
                    duration_ms=(time.perf_counter() - browser_start) * 1000.0,
                    attempt=attempt,
                    error=repr(exc),
                )
                raise
            else:
                _emit_browser(
                    op="fetch_via_browser",
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
        injection (Anti-Patterns — re-introduces detectable inconsistencies). Opt-in
        escalation only since #40 — Camoufox is the default everywhere (dev + CI +
        prod) to keep engine-specific failures visible in local dev. Patchright
        passes Cloudflare reliably on residential IPs but is flagged by Cloudflare's
        encrypted tier on cloud Linux runners (#35), and the dev/prod engine split
        previously hid Camoufox-only crashes (issue #54) from local repro.
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
        detection). Since #40 it is the default engine everywhere (dev + CI + prod)
        — the previous Patchright-on-dev / Camoufox-on-CI split meant Firefox-only
        failure modes (e.g. issue #54) went unseen in local repro and surfaced only
        in nightly triage. First-time use requires ``uv run camoufox fetch`` to
        download its Firefox binary (~200 MB, one-time, into a Camoufox-managed
        cache dir).

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

    async def _solve_keyed(self, context: Any, key: str) -> Clearance:
        """Resolve ``key`` to its per-domain challenge URL and solve there (#88).

        The lifecycle's solve closure is this method (not ``_solve_real``) so the
        per-key challenge URL is bound at solve time. ``key`` is the
        ``source_key``; its URL comes from the ``challenge_urls`` map, falling
        back to the constructor ``challenge_url`` default when absent.
        """
        url = self._challenge_urls.get(key) or self._challenge_url
        return await self._solve_real(context, url)

    async def _solve_real(self, context: Any, challenge_url: str) -> Clearance:
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
            # cf_clearance that ``_belongs_to_host`` the host we navigated.
            challenge_host = (urlparse(challenge_url).hostname or "").lower()
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
            # domain thereof). Comix's /api/v1 endpoints require BOTH cf_clearance
            # AND the Laravel ``session`` cookie set on comix.to during the solve
            # (empirically verified 2026-05-30 — passing only cf_clearance returns
            # 403). Scoping by domain naturally excludes cross-site ad-tracking
            # cookies the solver may pick up incidentally.
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
