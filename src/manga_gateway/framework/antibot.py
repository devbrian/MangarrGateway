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

Engine selection (#35): the underlying browser is selected via the ``engine``
parameter (``"patchright"`` Chromium-based, default; ``"camoufox"`` Firefox-based).
Patchright passes Cloudflare reliably on residential IPs (dev/Windows); Camoufox
is the documented escalation when Patchright's Chromium fingerprint is flagged
by Cloudflare's encrypted tier on cloud Linux runners (ubuntu-latest in CI). Both
engines back the SAME ``AntiBotSolver`` interface — the swap is a single
constructor argument with no rewrite (CLAUDE.md "keep the browser behind an
interface so this is a config flip"). The launch closure is selected at
``__init__`` and remains LAZY (the heavy import happens only on first solve),
so neither browser binary is touched by the deterministic gate (D-42).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlparse

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
# Patchright is the default (dev parity on Windows); Camoufox is the CI
# escalation (cloud Linux runners).
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

    Engine selection (#35): ``engine="patchright"`` (default) uses Patchright's
    Chromium build; ``engine="camoufox"`` uses Camoufox's Firefox build. The choice
    affects ONLY which launch closure is wired into the lifecycle — the solve
    closure (cf_clearance polling) is engine-agnostic. The heavy import happens
    inside the launch closure so the deterministic gate never imports/launches a
    browser (D-42); tests inject a ``lifecycle`` whose ``launch``/``solve`` drive
    a mocked browser instead.

    The internal keyword-only ``force_resolve`` on ``get_clearance`` is the D-35
    re-solve path; it is deliberately kept OFF the ``AntiBotSolver`` Protocol (D-41).
    """

    def __init__(
        self,
        *,
        user_data_dir: str = "cloudflare-userdata",
        headless: bool = True,
        solve_concurrency: int = 1,
        recycle_seconds: float | None = None,
        challenge_url: str = _DEFAULT_CHALLENGE_URL,
        cloudflare_keys: Iterable[str] = (),
        engine: AntibotEngine = "patchright",
        lifecycle: BrowserLifecycle | None = None,
    ) -> None:
        self._user_data_dir = user_data_dir
        self._headless = headless
        self._challenge_url = challenge_url
        self._cloudflare_keys = frozenset(cloudflare_keys)
        self._engine: AntibotEngine = engine
        # Tests inject a lifecycle wrapping a MOCKED browser; production builds one
        # wrapping the real lazy-launch/solve closures. The launch closure is
        # selected by ``engine`` (Patchright default; Camoufox escalation, #35).
        # No real browser is touched until the first solve / explicit warm().
        launch = (
            self._launch_camoufox_context
            if engine == "camoufox"
            else self._launch_patchright_context
        )
        self._lifecycle = lifecycle or BrowserLifecycle(
            launch=launch,
            solve=self._solve_real,
            solve_concurrency=solve_concurrency,
            recycle_seconds=recycle_seconds,
        )
        self._playwright: Any = None  # the started playwright instance (real path)
        # Serializes ``fetch_via_browser`` (one fresh page per call on the warm
        # context); browser-driven operations compete for the same context and
        # bounding them as one queue keeps "minimize fingerprinting events"
        # (Pitfall 6).
        self._browser_lock = asyncio.Lock()

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
        return await self._lifecycle.solve(force=force_resolve)

    async def warm(self) -> None:
        """Best-effort eager solve at startup (D-33) + start the recycle watchdog.

        Called as a fire-and-forget task by the lifespan so a slow/failed solve never
        blocks startup. Exceptions are swallowed here — the caller (the lifespan's
        non-blocking launch) owns the force_disabled fallback on failure (D-33).
        """
        self._lifecycle.start_recycle_watchdog()
        # Trigger one solve so the clearance is ready before the first request.
        for key in self._cloudflare_keys:
            await self.get_clearance(key)
            break

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

        Concurrency: serialized through ``_browser_lock``. Every browser-driven
        operation (solve, fetch_via_browser) competes for the warm context and a
        fresh page; bounding them as one queue avoids unfair starvation and
        keeps the "minimize fingerprinting events" invariant (Pitfall 6).

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
                url, extract=extract, wait_for=wait_for, timeout=timeout
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
                url, extract=extract, wait_for=wait_for, timeout=timeout
            )

    async def _fetch_via_browser_once(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — same justification as fetch_via_browser
    ) -> Any:
        """One attempt of the goto + wait_for + evaluate sequence (no retry).

        The body is identical to the original :meth:`fetch_via_browser` (pre-#54).
        Split out so the public method can wrap a single retry around it after
        detecting a dead-driver crash — the retry simply re-enters this method
        against a freshly launched context. Holds ``_browser_lock`` so retries
        re-serialize correctly against any concurrent ``fetch_via_browser`` call.
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
            timeout_ms = int(timeout * 1000)
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
                    return await asyncio.wait_for(
                        page.evaluate("async () => { " + extract + " }"),
                        timeout=timeout,
                    )
                except TimeoutError as exc:
                    raise BrowserFetchError(
                        f"page.evaluate timed out after {timeout}s"
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    raise BrowserFetchError(f"page.evaluate failed: {exc}") from exc
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

    # ─────────────────────── real launch/solve closures ───────────────────────

    async def _launch_patchright_context(self) -> Any:
        """Launch a Patchright persistent context (lazy import — D-42).

        Uses ``launch_persistent_context`` with the on-disk ``user_data_dir`` so the
        cf_clearance persists across restarts (D-34). NO custom UA/headers/fingerprint
        injection (Anti-Patterns — re-introduces detectable inconsistencies). This is
        the dev/Windows default — passes Cloudflare reliably on residential IPs but
        is flagged by Cloudflare's encrypted tier on cloud Linux runners (#35); CI
        flips to the Camoufox closure below.
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

        self._playwright = await async_playwright().start()
        return await self._playwright.chromium.launch_persistent_context(
            self._user_data_dir,
            headless=self._headless,
            no_viewport=True,
        )

    async def _launch_camoufox_context(self) -> Any:
        """Launch a Camoufox (Firefox-based) persistent context (lazy import — D-42).

        Camoufox wraps Playwright's Firefox driver with a C++ fingerprint spoof; per
        CLAUDE.md it is the strongest open-source stealth in 2026 (~0% headless
        detection) and is the documented escalation when Patchright/Chromium stops
        passing Cloudflare's encrypted tier on cloud Linux runners (#35). The CI
        nightly-live-smoke workflow sets ``GATEWAY_CLOUDFLARE_ENGINE=camoufox`` and
        runs ``uv run camoufox fetch`` to download its Firefox binary.

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

        self._playwright = await async_playwright().start()
        # ``persistent_context=True`` returns a BrowserContext (same shape as
        # Patchright's launch_persistent_context); Camoufox routes its
        # ``user_data_dir`` through Playwright's launch_persistent_context under
        # the hood. ``no_viewport=True`` matches the Patchright launch for
        # parity — Cloudflare's encrypted tier checks viewport-derived signals.
        return await AsyncNewBrowser(
            self._playwright,
            persistent_context=True,
            user_data_dir=self._user_data_dir,
            headless=self._headless,
            no_viewport=True,
        )

    async def _solve_real(self, context: Any) -> Clearance:
        """Solve the challenge on the live context; capture cf_clearance + UA.

        Engine-agnostic: the polling-cookies + capture-UA logic depends only on
        Playwright's ``BrowserContext`` API surface, which both Patchright (Chromium)
        and Camoufox (Firefox) expose identically. Browser work runs on the
        Playwright async API (already off the event loop). The image fetch is NEVER
        done through the browser (CLAUDE.md) — only the token capture is.
        """
        page = await context.new_page()
        try:
            await page.goto(self._challenge_url, wait_until="domcontentloaded")
            # Wait for the challenge to clear: poll context.cookies() until the
            # cf_clearance cookie appears. We CANNOT use page.wait_for_function on
            # ``document.cookie.includes('cf_clearance')`` because Cloudflare sets
            # cf_clearance with httpOnly=true, which is invisible to client-side
            # JavaScript by design (verified empirically against comix.to, recon
            # 2026-05-30). The cookie IS readable via CDP (context.cookies()), so
            # we poll that from Python instead.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 60.0
            jar: list[dict[str, Any]] = []
            while True:
                jar = await context.cookies()
                if any(c.get("name") == "cf_clearance" for c in jar):
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
            challenge_host = (urlparse(self._challenge_url).hostname or "").lower()
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
