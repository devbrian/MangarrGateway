"""Anti-bot solver seam (BOT-01/BOT-02).

Kept tiny so the Patchright -> Camoufox escalation (Phase 4) is a config flip.
Phase 1 uses ``NoopSolver`` (MangaDex et al. need no challenge solving); Phase 4
fills ``CloudflareSolver`` with a real Patchright persistent-context solve behind a
bounded :class:`~manga_gateway.framework.solver_lifecycle.BrowserLifecycle`.

D-45 — browser-evaluated decrypt: ``CloudflareSolver`` exposes a ``decrypt``
coroutine that reuses the warm Patchright context (which has already passed
Cloudflare and loaded ``secure-*.js``) to evaluate ``globalThis.t(ciphertext)``
on a dedicated warm comix.to page. The framework's decrypt seam delegates to
this method for the ``comix-v1`` scheme.

Option A (Plan 04-04 deviation, 2026-05-30) — browser-driven content fetch:
``CloudflareSolver`` ALSO exposes :meth:`fetch_via_browser` — a small primitive
that navigates a page in the warm context, optionally waits for a selector or JS
condition, and runs ``page.evaluate`` to read JSON-serializable data out of the
rendered DOM. Sources whose encrypted-API endpoints require a per-chapter token
that we cannot mint statically (Comix ``/api/v1/chapters/{id}`` requires ``_=``
minted by the same VM-obfuscated ``secure-*.js`` that does decryption) instead
let the live page's own JS do token-mint + API call + decrypt + image-tag render,
then read the result from the DOM. The httpx path remains the bulk image fetcher
(CLAUDE.md "image fetch is NEVER through the browser"); the browser only drives
the manifest resolution step.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from .decrypt import DecryptError
from .solver_lifecycle import BrowserLifecycle

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = logging.getLogger("manga_gateway")

# Default URLs the solver navigates to for cf_clearance acquisition + the warm
# decrypt page. Overridden by the application wiring (app.py lifespan) from the
# concrete source's metadata — the framework defaults exist only so unit tests
# can spin up a CloudflareSolver without naming a host. Pinned by Plan 04-04
# live recon when the application supplies a real source.
_DEFAULT_CHALLENGE_URL = "https://example.invalid/"
_DEFAULT_DECRYPT_URL = "https://example.invalid/"


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
    (navigation failure, ``wait_for`` timeout, evaluate exception). Distinct
    from :class:`DecryptError` so the SourceContext can classify it separately —
    a fetch failure is a source-level read error, not a cipher failure.
    """


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
    """Patchright-backed Cloudflare clearance solver (BOT-01/BOT-02).

    Drives a Patchright persistent context (D-34 cross-restart clearance) to solve
    the Cloudflare challenge, then captures the ``cf_clearance`` cookie + the EXACT
    ``navigator.userAgent`` of that SAME session (Pitfall 1 — the cookie is bound to
    its issuing UA) into a :class:`Clearance`. All browser work runs OFF the event
    loop via Patchright's native async API, behind a bounded
    :class:`BrowserLifecycle` (solve cap + single-flight + recycle + cleanup-on-all-
    paths, criterion #4).

    ``patchright`` is imported LAZILY inside the launch closure so the deterministic
    gate never imports/launches a browser (D-42); tests inject a ``lifecycle`` whose
    ``launch``/``solve`` drive a mocked browser instead.

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
        decrypt_url: str = _DEFAULT_DECRYPT_URL,
        cloudflare_keys: Iterable[str] = (),
        lifecycle: BrowserLifecycle | None = None,
        decrypt_page_factory: Any = None,
    ) -> None:
        self._user_data_dir = user_data_dir
        self._headless = headless
        self._challenge_url = challenge_url
        self._decrypt_url = decrypt_url
        self._cloudflare_keys = frozenset(cloudflare_keys)
        # Tests inject a lifecycle wrapping a MOCKED browser; production builds one
        # wrapping the real lazy-patchright launch/solve closures (no real Chromium
        # is touched until the first solve / explicit warm()).
        self._lifecycle = lifecycle or BrowserLifecycle(
            launch=self._launch_real_context,
            solve=self._solve_real,
            solve_concurrency=solve_concurrency,
            recycle_seconds=recycle_seconds,
        )
        self._playwright: Any = None  # the started patchright instance (real path)
        # D-45 warm decrypt page lifecycle. Lazy-warmed on first decrypt(); shares
        # the solver's persistent context (the same browser session that holds
        # cf_clearance), so secure-*.js loads under the same fingerprint that
        # passed Cloudflare. ``_decrypt_lock`` serializes page.evaluate() calls
        # (Patchright is not parallel-safe on a single page). The optional
        # ``decrypt_page_factory`` is the test injection seam (mocked browser).
        self._decrypt_page: Any = None
        self._decrypt_lock = asyncio.Lock()
        self._decrypt_page_factory = decrypt_page_factory or self._open_decrypt_page

    # ─────────────────────────── public seam (D-41) ───────────────────────────

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

    async def decrypt(self, ciphertext: bytes) -> bytes:
        """Browser-evaluated decrypt of a ``comix-v1`` ciphertext envelope (D-45).

        Calls ``await globalThis.t(ciphertext)`` on the warm comix.to page. The
        warm page is lazy-created on first call and reused across decrypts (it
        shares the solver's persistent context, so the cipher VM's
        ``navigator.appCodeName`` / fingerprint checks see the same session that
        passed Cloudflare). Decrypt calls are serialized via ``_decrypt_lock``
        (a single page.evaluate is the bottleneck; parallel calls on one page
        are not safe).

        Self-diagnosing entry-point check: if ``globalThis.t`` is undefined we
        scan the page's global scope for async-bound exports, surface them in
        the raised :class:`DecryptError`, and let the first live-smoke failure
        be self-explanatory (the entry-point identification was inferred by
        elimination from ``secure-*.js`` — strongest candidate, unverified live).

        Raises:
            DecryptError: warm-page navigation failed (no clearance, JS bundle
                blocked, or the entry point ``globalThis.t`` does not exist).
        """
        async with self._decrypt_lock:
            page = await self._ensure_decrypt_page()
            # ciphertext is a JSON envelope `{"e":"<base64url>"}` per live recon —
            # globalThis.t accepts a UTF-8 string of the envelope (or the inner
            # base64url string). We pass the envelope verbatim; the cipher VM
            # extracts `.e` internally (live_recon: "the only .e runtime check is
            # in the VM dispatcher").
            text = ciphertext.decode("utf-8", errors="strict")
            try:
                plain_str: str = await page.evaluate(
                    "async (b) => {"
                    "  if (typeof globalThis.t !== 'function') {"
                    "    const cands = Object.getOwnPropertyNames(globalThis).filter("
                    "      n => { try { const v = globalThis[n];"
                    "        return typeof v === 'function'"
                    "          && v.constructor"
                    "          && v.constructor.name === 'AsyncFunction';"
                    "      } catch (e) { return false; } });"
                    "    throw new Error("
                    "      'browser-eval decrypt entry point not found; "
                    "expected globalThis.t; async-fn candidates=' "
                    "      + JSON.stringify(cands.slice(0, 20)));"
                    "  }"
                    "  return await globalThis.t(b);"
                    "}",
                    text,
                )
            except Exception as exc:  # noqa: BLE001 — translate JS error → DecryptError
                msg = str(exc)
                if "browser-eval decrypt entry point not found" in msg:
                    raise DecryptError(msg) from exc
                raise DecryptError(f"browser-eval decrypt failed: {msg}") from exc
            return plain_str.encode("utf-8")

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

        Concurrency: serialized through the SAME ``_decrypt_lock`` that bounds
        :meth:`decrypt`. We do not add a new semaphore because every browser-driven
        operation (solve, decrypt, fetch_via_browser) competes for the warm context
        and a fresh page; bounding them as one queue avoids unfair starvation and
        keeps the "minimize fingerprinting events" invariant (Pitfall 6).

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
                underlying exception so the SourceContext sees one type.
        """
        async with self._decrypt_lock:
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
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=timeout_ms
                    )
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

    async def _ensure_decrypt_page(self) -> Any:
        """Return the warm decrypt page; lazy-create on first call.

        Reuses the lifecycle's persistent context (so the warm page lives under
        the same browser session that holds ``cf_clearance``) — solver's
        existing recycle watchdog and aclose() drive its lifecycle.
        """
        if self._decrypt_page is not None:
            return self._decrypt_page
        # Ensure the persistent context is up (this also drives the lazy
        # patchright launch in the real path).
        ctx = await self._lifecycle._ensure_context()
        try:
            page = await self._decrypt_page_factory(ctx, self._decrypt_url)
        except Exception as exc:  # noqa: BLE001
            raise DecryptError(f"decrypt page warm failed: {exc}") from exc
        self._decrypt_page = page
        return page

    async def _open_decrypt_page(self, context: Any, url: str) -> Any:
        """Default real-Patchright warm-page factory: new_page → goto → wait."""
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        # Wait briefly for secure-*.js to attach globalThis.t. Bounded so a
        # broken bundle surfaces as a DecryptError on the first evaluate().
        with contextlib.suppress(Exception):
            await page.wait_for_function(
                "() => typeof globalThis.t === 'function'", timeout=15_000
            )
        return page

    async def aclose(self) -> None:
        """Tear the bounded lifecycle down + stop patchright (Pitfall 4).

        Closes the warm decrypt page BEFORE the lifecycle's persistent context
        (closing the context implicitly closes its pages, but explicit teardown
        keeps cleanup auditable and works with mocked browsers in tests).
        """
        page = self._decrypt_page
        self._decrypt_page = None
        if page is not None:
            with contextlib.suppress(Exception):
                await page.close()
        await self._lifecycle.aclose()
        pw = self._playwright
        self._playwright = None
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()

    # ─────────────────────── real patchright launch/solve ───────────────────────

    async def _launch_real_context(self) -> Any:
        """Launch the Patchright persistent context (lazy import — D-42).

        Uses ``launch_persistent_context`` with the on-disk ``user_data_dir`` so the
        cf_clearance persists across restarts (D-34). NO custom UA/headers/fingerprint
        injection (Anti-Patterns — re-introduces detectable inconsistencies).
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

    async def _solve_real(self, context: Any) -> Clearance:
        """Solve the challenge on the live context; capture cf_clearance + UA.

        Browser work runs on Patchright's native async API (already off the event
        loop). The image fetch is NEVER done through the browser (CLAUDE.md) — only
        the token capture is.
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
