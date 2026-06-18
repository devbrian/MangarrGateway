"""Gateway-side Android-WebView Cloudflare solver (BOT-01/BOT-02, Phase 10).

``AndroidSolver`` implements the existing
:class:`~manga_gateway.framework.antibot.AntiBotSolver`
Protocol by httpx-calling the ``android-solver`` sidecar over the docker-internal
network — it mints a ``cf_clearance`` from a REAL Android WebView (a redroid sidecar)
for the strict Cloudflare Turnstile on ``mangadot.net`` / ``kagane.to`` that desktop
browser automation cannot clear from Linux (root cause + proof: resolved debug session
``mangadot-cf-linux-fingerprint``). R1 is preserved: the gateway process holds NO
Android machinery — it reaches the sidecar over HTTP only (the sidecar drives
redroid via adb + CDP; Plans 10-01/10-02). This module imports NOTHING from the
``android_solver`` sidecar package.

It returns the SAME :class:`Clearance` shape the existing httpx leg already injects
per request (D-40: ``cf_clearance`` cookie + the EXACT WebView UA the cookie is bound
to — a mismatched UA silently invalidates the cookie). The per-source engine selection
(comix → Patchright, mangadot/kagane → here) lives in
:class:`~manga_gateway.framework.solver_router.SolverRouter`, not in this class.

D-35 re-solve hold: the last-good clearance is held per source key; a non-force call
reuses it, a ``force_resolve=True`` call (the 403 self-heal, driven by context.py's
``inspect.signature`` pass-through, D-41) discards the held value and runs ONE fresh
sidecar solve. ``warm()`` mirrors :meth:`CloudflareSolver.warm`'s return contract
(the FAILED keys) so the lifespan disables only failures; an unconfigured sidecar URL
reports ALL android keys failed so the gate / CI / a local box without redroid stays
green. The ``cf_clearance`` value is NEVER logged (T-10-04).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from .antibot import Clearance

if TYPE_CHECKING:
    from pydantic import SecretStr

_log = logging.getLogger("manga_gateway")


class AndroidSolver:
    """Mint a per-source clearance via the android-solver sidecar (AntiBotSolver).

    Constructed with the sidecar base URL (``None`` ⇒ unconfigured: every solve
    raises so ``warm()`` boots all android keys disabled), the sidecar API key (a
    ``SecretStr`` so its repr/logs auto-redact — SEC-01/T-10-04), a per-call request
    timeout, and the android-engine source keys mapped to their
    ``cloudflare_challenge_url``. A key NOT in that map resolves to ``None`` on every
    call (BOT-01). The httpx client is owned (lazily built) unless one is injected
    (tests); ``aclose`` closes only an owned client.
    """

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: SecretStr | None,
        challenge_urls: dict[str, str],
        # Client timeout: must exceed the sidecar's per-solve cap (default 120s) +
        # margin so the gateway never abandons + re-fires a solve mid-flight (the app
        # wires this from settings.android_solver_timeout_s, default 180).
        timeout_s: float = 180.0,
        # PROXY-01 / Req 7: the ``build_proxy`` Playwright dict
        # (``{server, username?, password?}``) — the SAME value already feeding
        # the CloudflareSolver. ``None`` ⇒ no proxy in the /solve body (D-08, the
        # gate/CI/local-no-redroid path stays byte-for-byte unchanged). The dict
        # arrives already built by ``build_proxy`` (the sole SecretStr unpacker,
        # T-odg-01) — it is threaded through verbatim and NEVER logged (T-11-02).
        proxy: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Strip a trailing slash so ``f"{base}/solve"`` never doubles it.
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._challenge_urls = dict(challenge_urls)
        self._timeout_s = timeout_s
        self._proxy = proxy
        self._client = client
        self._owns_client = client is None
        # Last-good clearance per source key (the D-35 hold — mirrors the browser
        # solver's persistent-context clearance reuse).
        self._held: dict[str, Clearance] = {}

    async def get_clearance(
        self,
        source_key: str,
        *,
        force_resolve: bool = False,
        solve_if_missing: bool = True,
    ) -> Clearance | None:
        """Return clearance for ``source_key`` (``None`` for non-android keys).

        A key absent from the android challenge-url map → ``None`` (BOT-01 — the
        gateway never touches the sidecar for it). Otherwise a non-force call reuses
        the held clearance when present; ``force_resolve=True`` discards it and runs a
        FRESH sidecar solve (D-35, the 403 self-heal). ``force_resolve`` is a declared
        keyword so context.py's ``_call_solver`` ``inspect.signature`` detection passes
        it through (D-41).

        ``solve_if_missing=False`` (the on-demand peek, debug pooltimeout-recurrence):
        serve the held clearance if present but return ``None`` instead of running a
        BLOCKING sidecar solve when none is held — the caller (a
        ``cloudflare_challenge_optional`` source) will let the request go out without
        clearance and only force a real solve if the response is an actual challenge.
        This is what stops an intermittent-challenge source from hanging on the sidecar
        for a clearance the site is not currently demanding.
        """
        if source_key not in self._challenge_urls:
            return None  # MangaDex et al. — no android clearance needed
        if force_resolve:
            # WR-05: discard the held entry BEFORE the fresh solve so a FAILED
            # force-resolve (the common 403 self-heal case) cannot leave the known
            # -bad token held — the next non-force call must re-solve rather than
            # silently re-serve the stale clearance that produced the 403.
            self._held.pop(source_key, None)
        else:
            held = self._held.get(source_key)
            if held is not None:
                return held
            if not solve_if_missing:
                # On-demand peek with nothing held — do NOT block on a sidecar solve.
                return None
        clearance = await self._solve(source_key)
        self._held[source_key] = clearance
        return clearance

    async def warm(self) -> list[str]:
        """Best-effort eager solve per android key; return the FAILED keys.

        Mirrors :meth:`CloudflareSolver.warm`'s return contract so the lifespan
        force-disables ONLY the sources whose eager solve failed (D-33, per-source
        isolation). When the sidecar URL is unconfigured every solve raises, so every
        android key is reported failed — they boot disabled and the gate / CI / a
        local box without redroid stays green. Each key's solve is isolated in its own
        try/except; the underlying exception is logged with ``exc_info`` (NEVER the
        clearance value).
        """
        failed: list[str] = []
        for key in self._challenge_urls:
            try:
                await self.get_clearance(key)
            except Exception as exc:  # noqa: BLE001 — isolate per-key warm failures
                _log.warning(
                    "AndroidSolver warm failed for source %r: %r "
                    "(boots disabled — D-33)",
                    key,
                    exc,
                    exc_info=True,
                )
                failed.append(key)
        return failed

    async def aclose(self) -> None:
        """Close the owned httpx client (no-op for an injected one)."""
        if self._owns_client and self._client is not None:
            client = self._client
            self._client = None
            await client.aclose()

    # ─────────────────────────────── internals ───────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily build the owned httpx client (no new gateway dependency — httpx is
        already the shared transport library; this is a SEPARATE client to the
        sidecar, not the R1-shared image-fetch client)."""
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _solve(self, source_key: str) -> Clearance:
        """POST the sidecar ``/solve`` and parse a :class:`Clearance` (BOT-02).

        Sends ``X-Solver-Key`` (SEC-01) + ``{"challenge_url": <map[key]>}``; a
        non-200 raises (``raise_for_status``) so ``warm()`` reports the key failed and
        the lifespan disables it, exactly like a failed browser solve. The
        ``cf_clearance`` token is parsed into the SAME ``Clearance`` shape the httpx
        leg injects via D-40 — and is NEVER logged (T-10-04).
        """
        if self._base_url is None:
            raise RuntimeError(
                "android_solver_url is not configured — cannot solve "
                f"{source_key!r} (the android-solver sidecar is unwired)"
            )
        challenge_url = self._challenge_urls[source_key]
        headers: dict[str, str] = {}
        if self._api_key is not None:
            headers["X-Solver-Key"] = self._api_key.get_secret_value()
        # Req 7: thread the single static proxy into the /solve body so the
        # sidecar's CF-solve egress matches the gateway's httpx-fetch egress for
        # the same clearance. Gated like the CloudflareSolver: when unconfigured
        # the body carries ONLY ``challenge_url`` (D-08). The proxy dict is passed
        # through verbatim (already unpacked by build_proxy) and never logged.
        body: dict[str, object] = {"challenge_url": challenge_url}
        if self._proxy is not None:
            body["proxy"] = self._proxy
        resp = await self._ensure_client().post(
            f"{self._base_url}/solve",
            json=body,
            headers=headers,
            timeout=self._timeout_s,
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload["cf_clearance"]
        user_agent = payload["user_agent"]
        # Log the solve event only — never the token value (T-10-04).
        _log.info("AndroidSolver minted clearance for source %r", source_key)
        return Clearance(cookies={"cf_clearance": token}, user_agent=user_agent)
