"""Standalone rate-limit measurement harness (replay-captured-requests driver).

Empirically measures the TRUE rate limits of any registered manga source: how much
parallelism the site tolerates, the calls/min ceiling, and whether those differ by
endpoint category (search vs manifest vs image). It exists to replace the
conservative-guess ``rate_limit_per_minute`` values (e.g. mangadot=10) with measured
ceilings, closing GitHub issue #101.

The harness reuses the real framework seams so swapping the source under test
requires ZERO per-source code:

* ``SourceRegistry`` + ``register_builtin_sources`` discover sources by
  ``--source <key>``;
* an :class:`InstrumentedTransport` wraps the injectable ``HttpxTransport`` and
  records the RAW status (before tenacity retry) of every outbound call;
* a ``SourceContext`` is built EXACTLY like ``api/routes/search.py`` does — but
  with OUR own ``aiolimiter`` set effectively unlimited (so we measure the SITE's
  limit, not our self-imposed one) and the health breaker neutralized
  (``source_health=None``).

Probe driver (LOCKED, CONTEXT.md): REPLAY CAPTURED REQUESTS. Run ONE real
``source.search()`` (plus best-effort manifest/image resolution) as a warm-up
through the instrumented transport, capture a representative request per endpoint
category, then probe by replaying that captured request repeatedly at controlled
rate/concurrency. The sweep is an aggressive FULL sweep (does not stop at the
first 429 — maps the whole penalty/recovery curve, but still marks the first
sustained block).

By default the proxy pool is health-checked first (Tier 1: IP-echo liveness +
bypass + latency); the fastest healthy proxy is pinned and the run rotates to the
next on warm-up/CF-solve failure (Tier 2). ``--no-health-check`` pins
``--proxy-index`` (or 0) directly.

SECURITY (T-w1k-01/03): ``proxies.txt`` holds secret residential proxy credentials.
This script NEVER prints a full proxy string, NEVER writes any proxy value into
the JSON report, and NEVER hardcodes a proxy value. Proxies are referenced by
host-only or a masked ``proxy[#index]``. The health check also asserts the proxy
actually changes the egress IP (never silently testing on your own IP). The proxy
is REQUIRED by default; ``--no-proxy`` is the explicit opt-out that runs on the
local IP.

Usage::

    uv run python scripts/probe_rate_limits.py --list-sources
    uv run python scripts/probe_rate_limits.py --dry-run --source mangadex
    uv run python scripts/probe_rate_limits.py --source mangadex
    uv run python scripts/probe_rate_limits.py --source mangadex --proxy-index 7

This is a standalone diagnostic script — no pytest tests, no ``src/`` module, no
CLI packaging — but it ships in the repo and MUST pass ``uv run nox -s gate``
(ruff + ruff format --check + mypy strict). Console output is ASCII-only
(Windows cp1252).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import httpx  # noqa: E402

from manga_gateway.config import Settings  # noqa: E402
from manga_gateway.framework.antibot import (  # noqa: E402
    CloudflareSolver,
    NoopSolver,
)
from manga_gateway.framework.context import SourceContext, is_cf_challenge  # noqa: E402
from manga_gateway.framework.ratelimit import RateLimiter  # noqa: E402
from manga_gateway.framework.registry import SourceRegistry  # noqa: E402
from manga_gateway.framework.session import SessionManager  # noqa: E402
from manga_gateway.framework.session_prep import NoSessionPrep  # noqa: E402
from manga_gateway.framework.transport import HttpxTransport  # noqa: E402
from manga_gateway.handles.store import HandleStore  # noqa: E402
from manga_gateway.models.search import SearchRequest  # noqa: E402
from manga_gateway.sources import register_builtin_sources  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Sequence

    from manga_gateway.framework.base import Source

# A non-real API key satisfies Settings(api_key=...) construction (config.py D-01:
# init kwargs beat env, so this never persists or reads a real key).
_PROBE_API_KEY = "rate-limit-probe-not-an-api-key"

# OUR own per-source aiolimiter is set effectively unlimited so it NEVER gates — we
# measure the SITE's ceiling, not our self-imposed one (CONTEXT.md).
_UNLIMITED_RATE_PER_MINUTE = 1_000_000

# Default search term for the warm-up. Overridable via --query.
_DEFAULT_QUERY = "one piece"

# Endpoint categories the warm-up may capture. "search" is always attempted;
# "manifest"/"image" are best-effort (skip gracefully on CF sources / no release).
_CATEGORY_SEARCH = "search"
_CATEGORY_MANIFEST = "manifest"
_CATEGORY_IMAGE = "image"

# Report output dir under the repo root (gitignored — see .gitignore).
_REPORT_DIR = _REPO_ROOT / "_rate_limit_probe_out"

# proxies.txt path (raw host:port:user:pass lines). Gitignored — holds secrets.
_PROXIES_PATH = _REPO_ROOT / "proxies.txt"

# A grid cell is a "sustained RATE-LIMIT block" when its rate-limited fraction crosses
# this threshold. Rate-limited = a TRUE anti-throttle signal (HTTP 429/403, a Cloudflare
# challenge, or a Retry-After header), NOT a timeout/connection error. Timeouts/errors
# are tracked separately as latency/degradation: a slow or flaky site is not the same
# as one that is throttling you. We map the WHOLE grid, marking the first cell to cross.
_RATE_LIMIT_THRESHOLD = 0.20

# A grid cell is "latency-degraded" when the SITE starts responding slower under load —
# the soft-throttle signal that often precedes (or replaces) a hard rate-limit. Two
# independent triggers, either of which flags the cell:
#   1) its median latency is at least this factor times the gentlest cell's baseline, or
#   2) its timeout+error fraction crosses _DEGRADATION_FRACTION (requests starting to
#      die on the read timeout rather than returning a block status).
_LATENCY_DEGRADATION_FACTOR = 2.0
_DEGRADATION_FRACTION = 0.20

# Suggested rate_limit_per_minute = this safety fraction of the measured calls/min
# ceiling (the highest sustained rate with no sustained block). Conservative so the
# fed-back value (closing #101) stays comfortably under the real ceiling.
_SUGGESTED_RATE_SAFETY_FRACTION = 0.50

# A proxy is treated as IP-BANNED (rotate to the next) when its whole-grid HARD-block
# fraction (rate-limited + connection errors, EXCLUDING plain timeouts) crosses this — a
# sustained, grid-wide block that looks IP-level rather than a real rate ceiling.
_PROXY_BAN_FRACTION = 0.80

# Per-cell WALL-CLOCK deadline (seconds). Caps each grid cell so a slow/dead proxy or a
# tarpitting site cannot hang the sweep — without it, concurrency=1 serial 30s timeouts
# could stretch one cell toward an hour. Overridable via --cell-timeout-seconds.
_DEFAULT_CELL_TIMEOUT_SECONDS = 90.0

# Proxy health check (Tier 1): a single IP-echo call per proxy confirms it is alive,
# fast enough, and ACTUALLY changes the egress IP (not silently bypassed to the user's
# own IP — T-w1k-03). The pool is ranked fastest-first; the live path pins the best and
# rotates to the next on warm-up/CF-solve failure (Tier 2). The IP-echo is an external
# dependency; --no-health-check skips the whole step.
_IP_ECHO_URL = "https://api.ipify.org"
_HEALTH_PROBE_TIMEOUT_SECONDS = 10.0
_HEALTH_CHECK_CONCURRENCY = (
    10  # bounded liveness fan-out (NOT a rate-limit measurement)
)
_DEFAULT_MAX_PROXY_LATENCY_SECONDS = 5.0
_DEFAULT_PROXY_HEALTH_RETRIES = 3
_MAX_REJECTIONS_SHOWN = 10  # cap the masked rejection list so a dead pool isn't spammy


# ──────────────────────────── proxy masking (T-w1k-01) ────────────────────────────


@dataclass(frozen=True)
class ProxyEntry:
    """One parsed ``proxies.txt`` line (host:port:user:pass).

    SECURITY: the ``username``/``password`` fields are credentials. They are fed ONLY
    into ``Settings`` (so ``framework.proxy.build_proxy`` applies them) and are NEVER
    printed, logged, written to the JSON report, or embedded in any URL string. The
    ``index`` identifies the proxy in masked output (``proxy[#index]``).
    """

    index: int
    host: str
    port: str
    username: str
    password: str

    @property
    def server(self) -> str:
        """The credential-free ``http://host:port`` server URL (fed to Settings)."""
        return f"http://{self.host}:{self.port}"


def _mask_proxy(proxy: ProxyEntry | None) -> str:
    """Return a SAFE masked label for ``proxy`` — host-only / ``proxy[#index]``.

    NEVER returns ``user:pass`` or a full proxy URL. This is the SOLE function any log
    line or JSON-report field uses to reference a proxy (T-w1k-01). ``None`` means the
    sweep is running with no proxy (the explicit ``--no-proxy`` opt-out).
    """
    if proxy is None:
        return "no-proxy"
    return f"proxy[#{proxy.index}] host={proxy.host}"


def _load_proxies(path: Path) -> list[ProxyEntry]:
    """Parse ``proxies.txt`` (raw ``host:port:user:pass`` lines) into entries.

    Blank lines and ``#`` comments are skipped. A malformed line (not 4 colon-separated
    fields) is skipped with a masked warning (NEVER echoing the line — it holds creds).
    Returns an empty list if the file does not exist (the ``--no-proxy`` / dry-run paths
    do not require it).
    """
    if not path.exists():
        return []
    entries: list[ProxyEntry] = []
    index = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 4:
            # NEVER print the offending line — it may carry credentials.
            print(
                f"[probe] WARNING: skipping malformed proxies.txt line {index + 1} "
                "(expected host:port:user:pass)",
                file=sys.stderr,
            )
            index += 1
            continue
        host, port, username, password = parts
        entries.append(
            ProxyEntry(
                index=index,
                host=host,
                port=port,
                username=username,
                password=password,
            )
        )
        index += 1
    return entries


# ──────────────────────────── instrumented transport ────────────────────────────


@dataclass
class RequestRecord:
    """One recorded outbound call (RAW, before any retry/limiter above this layer).

    Captured straight off the returned ``httpx.Response`` (or the raised
    exception) so a 429/403 is observed directly here even though the
    ``SourceContext`` tenacity retry sits ABOVE this transport (CONTEXT.md
    measurement-correctness requirement).
    """

    timestamp: float
    method: str
    url: str
    status_code: int | None
    latency_seconds: float
    exception_type: str | None = None
    exception_message: str | None = None
    cf_challenge: bool = False
    retry_after: str | None = None


@dataclass
class CapturedRequest:
    """A representative request to replay for one endpoint category.

    Captures the method/url and the request kwargs (params/headers/data) actually sent —
    enough to re-issue the SAME call directly through the instrumented transport during
    the sweep, bypassing the source's parsing but still through the real httpx client +
    pinned proxy + injected clearance.
    """

    category: str
    method: str
    url: str
    kwargs: dict[str, Any]


class InstrumentedTransport:
    """Wraps a ``HttpxTransport`` and records every outbound call (Transport Protocol).

    Records the RAW status BEFORE any retry/limiter (CONTEXT.md): this recorder
    sits at the transport layer beneath the ``SourceContext`` tenacity retry, so a
    429/403 is observed directly here. Both the warm-up requests AND the later
    probe-replay requests append into ``records``; :meth:`segment` clears the
    buffer between phases so the
    sweep stats are computed only over the sweep's own calls.
    """

    def __init__(self, inner: HttpxTransport) -> None:
        self._inner = inner
        self.records: list[RequestRecord] = []
        # Last warm-up request kwargs per (method, url) so the sweep can replay the
        # EXACT params/headers/data the framework actually sent (incl. clearance).
        self._last_kwargs: dict[tuple[str, str], dict[str, Any]] = {}

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._last_kwargs[(method, url)] = dict(kwargs)
        started = time.monotonic()
        try:
            resp = await self._inner.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            self.records.append(
                RequestRecord(
                    timestamp=started,
                    method=method,
                    url=url,
                    status_code=None,
                    latency_seconds=time.monotonic() - started,
                    exception_type=type(exc).__name__,
                    exception_message=str(exc)[:200],
                )
            )
            raise
        self.records.append(
            RequestRecord(
                timestamp=started,
                method=method,
                url=url,
                status_code=resp.status_code,
                latency_seconds=time.monotonic() - started,
                cf_challenge=_safe_is_cf_challenge(resp),
                retry_after=resp.headers.get("retry-after"),
            )
        )
        return resp

    async def aclose(self) -> None:
        await self._inner.aclose()

    def last_kwargs(self, method: str, url: str) -> dict[str, Any]:
        """The kwargs the framework last sent for ``(method, url)`` (for replay)."""
        return dict(self._last_kwargs.get((method, url), {}))

    def segment(self) -> list[RequestRecord]:
        """Return the records so far and clear the buffer (warm-up vs sweep split)."""
        out = self.records
        self.records = []
        return out


def _safe_is_cf_challenge(resp: httpx.Response) -> bool:
    """``is_cf_challenge`` guarded — reading ``resp.content`` may raise mid-stream."""
    try:
        return is_cf_challenge(resp)
    except Exception:  # noqa: BLE001 — a classification probe must never crash a record
        return False


# ──────────────────────────── seam construction ────────────────────────────


def _build_registry() -> SourceRegistry:
    """A fresh registry with every built-in source registered by key (SRC-01)."""
    registry = SourceRegistry()
    register_builtin_sources(registry)
    return registry


def _build_settings(proxy: ProxyEntry | None) -> Settings:
    """Construct ``Settings`` with the API key + (optionally) the pinned proxy.

    The proxy is threaded via ``cloudflare_proxy_server/_username/_password`` so the
    existing ``framework.proxy.build_proxy`` applies it to BOTH egress legs (browser +
    httpx) — we do NOT re-implement proxy wiring (CONTEXT.md). Init kwargs beat env
    (config.py D-01), so this never reads a real key or a TOML file.
    """
    kwargs: dict[str, Any] = {"api_key": _PROBE_API_KEY}
    if proxy is not None:
        kwargs["cloudflare_proxy_server"] = proxy.server
        kwargs["cloudflare_proxy_username"] = proxy.username
        kwargs["cloudflare_proxy_password"] = proxy.password
    return Settings(**kwargs)


def _build_solver(
    source_cls: type[Source], settings: Settings, proxy: ProxyEntry | None
) -> NoopSolver | CloudflareSolver:
    """Build the right solver for the source's ``antibot`` level (mirrors app.py).

    ``antibot="none"`` -> :class:`NoopSolver`. ``cloudflare*`` -> a
    :class:`CloudflareSolver` keyed to this source only, with the source's
    challenge URL and the proxy threaded as the Playwright dict leg. The browser is
    NOT warmed/solved here — the warm-up clearance is best-effort and only needed
    for the live path (Task 1 does no solve).
    """
    antibot = getattr(source_cls, "antibot", "none")
    if not antibot.startswith("cloudflare"):
        return NoopSolver()
    key = source_cls.key
    challenge_url = getattr(source_cls, "cloudflare_challenge_url", None)
    solver_kwargs: dict[str, Any] = {
        "user_data_dir": settings.cloudflare_user_data_dir,
        "headless": settings.cloudflare_headless,
        "solve_concurrency": settings.cloudflare_solve_concurrency,
        "fetch_concurrency": settings.cloudflare_fetch_concurrency,
        "cloudflare_keys": {key},
        "engine": settings.cloudflare_engine,
    }
    if challenge_url:
        solver_kwargs["challenge_urls"] = {key: challenge_url}
    # PROXY-01: the Playwright dict leg (first element) threads into the browser launch.
    # NEVER unpack/log the proxy here — build_proxy is the sole SecretStr unpacker.
    from manga_gateway.framework.proxy import build_proxy  # noqa: PLC0415

    playwright_proxy, _ = build_proxy(settings)
    if playwright_proxy is not None:
        solver_kwargs["proxy"] = playwright_proxy
    return CloudflareSolver(**solver_kwargs)


def _build_context(
    source: Source,
    *,
    session: SessionManager,
    ratelimiter: RateLimiter,
    handle_store: HandleStore,
    solver: NoopSolver | CloudflareSolver,
) -> SourceContext:
    """Build a ``SourceContext`` like search.py — but with OUR limiter unlimited.

    ``rate_limit_per_minute`` is forced to :data:`_UNLIMITED_RATE_PER_MINUTE` so
    OUR aiolimiter never gates (we want the SITE's ceiling). ``source_health=None``
    neutralizes the breaker (context.py no-ops health when None).
    """
    src_decrypt_config = getattr(source, "decrypt_config", None)
    return SourceContext(
        source_key=source.key,
        rate_limit_per_minute=_UNLIMITED_RATE_PER_MINUTE,
        session=session,
        ratelimiter=ratelimiter,
        handle_store=handle_store,
        solver=solver,
        antibot=source.antibot,
        decrypt_scheme=source.decrypt_scheme,
        decrypt_config=dict(src_decrypt_config) if src_decrypt_config else None,
        source_health=None,
        session_prep=NoSessionPrep(),
    )


# ──────────────────────────── proxy health check (Tier 1) ────────────────────────────


@dataclass
class ProxyHealth:
    """One proxy's Tier-1 liveness result (referenced by MASKED index only).

    SECURITY: no IP value (egress or local) is ever stored or printed — the bypass check
    compares them in-memory and keeps only the boolean verdict (T-w1k-01/03).
    """

    proxy: ProxyEntry
    reachable: bool
    egress_ok: bool  # egress IP differs from the user's real IP (proxy not bypassed)
    within_latency: bool
    latency_seconds: float | None
    reason: str

    @property
    def healthy(self) -> bool:
        return self.reachable and self.egress_ok and self.within_latency


async def _fetch_echo_ip(
    transport: HttpxTransport,
) -> tuple[str | None, float, str | None]:
    """GET the IP-echo through ``transport``; return ``(ip, latency_s, error)``.

    ``ip`` is None on any non-200 / transport failure (with ``error`` set). The returned
    IP is held only long enough for the in-memory bypass comparison — never logged.
    """
    started = time.monotonic()
    try:
        resp = await transport.request(
            "GET", _IP_ECHO_URL, timeout=_HEALTH_PROBE_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        return None, time.monotonic() - started, f"transport:{type(exc).__name__}"
    latency = time.monotonic() - started
    if resp.status_code != 200:
        return None, latency, f"http-{resp.status_code}"
    return resp.text.strip(), latency, None


async def _fetch_local_ip() -> str | None:
    """Fetch the user's real public IP DIRECTLY (no proxy) for the bypass check."""
    transport = HttpxTransport(_build_settings(None))
    try:
        ip, _latency, _error = await _fetch_echo_ip(transport)
        return ip
    finally:
        await transport.aclose()


async def _check_proxy_health(
    proxy: ProxyEntry, local_ip: str | None, max_latency: float
) -> ProxyHealth:
    """Tier-1 liveness check for ONE proxy via a single IP-echo call.

    Catches: dead/unreachable (request fails), bypassed (egress IP == the user's own IP
    — T-w1k-03), and grossly slow (latency over ``max_latency``). It cannot predict a
    proxy's bandwidth ceiling from one request, so a proxy that passes here can still
    degrade at high rate during the sweep (the post-run IP-ban advisory backstops it).
    """
    transport = HttpxTransport(_build_settings(proxy))
    try:
        egress_ip, latency, error = await _fetch_echo_ip(transport)
    finally:
        await transport.aclose()
    if egress_ip is None:
        return ProxyHealth(proxy, False, False, False, latency, error or "unreachable")
    bypassed = local_ip is not None and egress_ip == local_ip
    within = latency <= max_latency
    if bypassed:
        reason = "bypassed (egress == local IP)"
    elif not within:
        reason = f"too slow ({latency:.1f}s > {max_latency:.1f}s)"
    else:
        reason = f"ok ({latency:.2f}s)"
    return ProxyHealth(
        proxy,
        reachable=True,
        egress_ok=not bypassed,
        within_latency=within,
        latency_seconds=latency,
        reason=reason,
    )


async def _select_healthy_proxies(
    proxies: list[ProxyEntry], *, max_latency: float
) -> list[ProxyHealth]:
    """Tier-1 health-check the whole pool; return HEALTHY proxies fastest-first.

    Liveness checks MAY fan out across proxies in parallel: this is NOT a rate-limit
    measurement (exactly one request per IP, nothing measured about throttling), so it
    does not violate the one-proxy-per-measurement rule. Bounded to
    :data:`_HEALTH_CHECK_CONCURRENCY` to stay gentle. The bypass safety check needs the
    user's real IP; if that cannot be fetched it is skipped with a loud warning.
    """
    local_ip = await _fetch_local_ip()
    if local_ip is None:
        print(
            "[probe] WARNING: could not determine your real IP; the proxy-bypass "
            "safety check (egress != your IP) is DISABLED for this run.",
            file=sys.stderr,
        )
    semaphore = asyncio.Semaphore(_HEALTH_CHECK_CONCURRENCY)

    async def _guarded(p: ProxyEntry) -> ProxyHealth:
        async with semaphore:
            return await _check_proxy_health(p, local_ip, max_latency)

    results = await asyncio.gather(*[_guarded(p) for p in proxies])
    healthy = sorted(
        (h for h in results if h.healthy),
        key=lambda h: (
            h.latency_seconds if h.latency_seconds is not None else float("inf")
        ),
    )
    print(
        f"[probe] proxy health: {len(healthy)}/{len(proxies)} healthy "
        "(IP-echo liveness, fastest-first)."
    )
    rejected = [h for h in results if not h.healthy]
    for h in rejected[:_MAX_REJECTIONS_SHOWN]:
        print(f"  - {_mask_proxy(h.proxy)}: {h.reason}")
    if len(rejected) > _MAX_REJECTIONS_SHOWN:
        print(f"  - ... +{len(rejected) - _MAX_REJECTIONS_SHOWN} more rejected")
    return healthy


# ──────────────────────────── warm-up capture ────────────────────────────


@dataclass
class WarmupResult:
    """The captured representative request per endpoint category + skip notes."""

    captured: dict[str, CapturedRequest] = field(default_factory=dict)
    notes: list[dict[str, Any]] = field(default_factory=list)

    def note_skip(self, category: str, reason: str) -> None:
        """Record a 'endpoint not captured' note (LOCKED graceful-skip decision)."""
        self.notes.append({"category": category, "captured": False, "reason": reason})


async def _capture_warmup(
    source: Source,
    ctx: SourceContext,
    transport: InstrumentedTransport,
    *,
    query: str,
) -> WarmupResult:
    """Run ONE real ``search()`` (+ best-effort manifest/image) through the recorder.

    Classifies the outbound calls made during ``search()`` as the "search"
    category and picks a representative request from the recorded rows.
    Manifest/image capture is best-effort: wrapped in try/except so a CF source
    needing the browser solver, or no resolvable release, records a structured skip
    note and continues (never fails the run).
    """
    result = WarmupResult()
    req = SearchRequest(type="chapter", query=query, sources=[source.key])

    # ── search (always attempted) ──
    transport.segment()  # isolate the search calls
    try:
        releases = await source.search(req, ctx)
    except Exception as exc:  # noqa: BLE001 — a failed warm-up search is a clean skip
        result.note_skip(_CATEGORY_SEARCH, f"search() raised: {type(exc).__name__}")
        releases = []
    search_rows = transport.segment()
    search_req = _pick_representative(transport, search_rows, _CATEGORY_SEARCH)
    if search_req is not None:
        result.captured[_CATEGORY_SEARCH] = search_req
    else:
        result.note_skip(_CATEGORY_SEARCH, "no outbound search request recorded")

    if not releases:
        result.note_skip(_CATEGORY_MANIFEST, "no release resolved from warm-up search")
        result.note_skip(_CATEGORY_IMAGE, "no release resolved from warm-up search")
        return result

    # ── manifest (best-effort) ──
    record = ctx.handle_store.resolve(releases[0].download_handle)
    if record is None:
        result.note_skip(_CATEGORY_MANIFEST, "download_handle did not resolve")
        result.note_skip(_CATEGORY_IMAGE, "download_handle did not resolve")
        return result

    manifest_urls: list[str] = []
    transport.segment()
    try:
        manifest_urls = await source.fetch_manifest(record.chapter_id, ctx)
    except Exception as exc:  # noqa: BLE001 — CF/browser manifest is a graceful skip
        result.note_skip(
            _CATEGORY_MANIFEST, f"fetch_manifest raised: {type(exc).__name__}"
        )
    manifest_rows = transport.segment()
    manifest_req = _pick_representative(transport, manifest_rows, _CATEGORY_MANIFEST)
    if manifest_req is not None:
        result.captured[_CATEGORY_MANIFEST] = manifest_req
    elif not any(n["category"] == _CATEGORY_MANIFEST for n in result.notes):
        result.note_skip(_CATEGORY_MANIFEST, "no outbound manifest request recorded")

    # ── image (best-effort) ──
    if not manifest_urls:
        result.note_skip(_CATEGORY_IMAGE, "manifest produced no page URLs")
        return result
    transport.segment()
    try:
        await source.fetch_image(manifest_urls[0], ctx)
    except Exception as exc:  # noqa: BLE001 — image fetch is a graceful skip
        result.note_skip(_CATEGORY_IMAGE, f"fetch_image raised: {type(exc).__name__}")
    image_rows = transport.segment()
    image_req = _pick_representative(transport, image_rows, _CATEGORY_IMAGE)
    if image_req is not None:
        result.captured[_CATEGORY_IMAGE] = image_req
    elif not any(n["category"] == _CATEGORY_IMAGE for n in result.notes):
        result.note_skip(_CATEGORY_IMAGE, "no outbound image request recorded")

    return result


def _pick_representative(
    transport: InstrumentedTransport,
    rows: Sequence[RequestRecord],
    category: str,
) -> CapturedRequest | None:
    """Pick a representative successful request from ``rows`` to replay.

    Prefers the last 2xx call (the steady-state data request after any redirects), then
    re-reads the EXACT kwargs the framework sent for that (method, url) so the replay
    carries the same params/headers/data (incl. injected clearance).
    """
    chosen: RequestRecord | None = None
    for row in rows:
        if row.status_code is not None and 200 <= row.status_code < 300:
            chosen = row
    if chosen is None and rows:
        chosen = rows[-1]
    if chosen is None:
        return None
    return CapturedRequest(
        category=category,
        method=chosen.method,
        url=chosen.url,
        kwargs=transport.last_kwargs(chosen.method, chosen.url),
    )


# ──────────────────────────── CLI ────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI. ``--help`` documents every flag (Task 1 done)."""
    parser = argparse.ArgumentParser(
        prog="probe_rate_limits.py",
        description=(
            "Measure a manga source's TRUE rate limits (parallelism, calls/min, "
            "per-endpoint) by replaying captured requests. Closes GH#101."
        ),
    )
    parser.add_argument(
        "--source",
        help="Source key to probe (via the registry; required for capture/sweep).",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="Print every registered source key and exit (no network, no proxy).",
    )
    parser.add_argument(
        "--query",
        default=_DEFAULT_QUERY,
        help=f"Warm-up search term (default: {_DEFAULT_QUERY!r}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Construct all seams and print the planned sweep grid WITHOUT issuing any "
            "outbound request or requiring a proxy. The gate-safe verification path."
        ),
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help=(
            "Explicit opt-out: run the live sweep on the local IP. Without this, a "
            "live sweep REQUIRES a pinned proxy from proxies.txt (never your own IP)."
        ),
    )
    parser.add_argument(
        "--proxy-index",
        type=int,
        default=None,
        help=(
            "Manually pin this proxies.txt index (skips health-based auto-selection; "
            "still rotates to other healthy proxies on warm-up failure unless "
            "--no-health-check). Omit to auto-select the fastest healthy proxy."
        ),
    )
    parser.add_argument(
        "--health-check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Tier-1 health-check the proxy pool (IP-echo liveness + bypass + latency) "
            "and pin the fastest healthy proxy, rotating on warm-up/CF failure. Use "
            "--no-health-check to pin --proxy-index (or 0) unchecked (default: on)."
        ),
    )
    parser.add_argument(
        "--max-proxy-latency-seconds",
        type=float,
        default=_DEFAULT_MAX_PROXY_LATENCY_SECONDS,
        help=(
            "Reject proxies whose health-check IP-echo latency exceeds this, as too "
            f"slow to measure cleanly (default: {_DEFAULT_MAX_PROXY_LATENCY_SECONDS})."
        ),
    )
    parser.add_argument(
        "--proxy-health-retries",
        type=int,
        default=_DEFAULT_PROXY_HEALTH_RETRIES,
        help=(
            "Max healthy proxies to try (rotate-on-failure) before giving up the "
            f"warm-up (default: {_DEFAULT_PROXY_HEALTH_RETRIES})."
        ),
    )
    parser.add_argument(
        "--concurrency-steps",
        default="1,2,4,8",
        help="Comma-separated concurrency levels for the grid (default: 1,2,4,8).",
    )
    parser.add_argument(
        "--rate-steps",
        default="30,60,120,240",
        help="Comma-separated target calls/min for the grid (default: 30,60,120,240).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=400,
        help="Hard cap on total outbound requests across the sweep (default: 400).",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=5.0,
        help="Cooldown between grid cells so recovery is observable (default: 5.0).",
    )
    parser.add_argument(
        "--cell-timeout-seconds",
        type=float,
        default=_DEFAULT_CELL_TIMEOUT_SECONDS,
        help=(
            "Per-cell wall-clock deadline; stragglers past it are cancelled and "
            "counted as slowness so a dead/slow proxy can't hang the sweep (default: "
            f"{_DEFAULT_CELL_TIMEOUT_SECONDS})."
        ),
    )
    return parser


def _parse_int_steps(raw: str) -> list[int]:
    """Parse a comma-separated ``"1,2,4"`` knob into a sorted unique int list."""
    values = sorted({int(p.strip()) for p in raw.split(",") if p.strip()})
    if not values:
        raise ValueError("step list must contain at least one positive integer")
    if any(v < 1 for v in values):
        raise ValueError("step values must be >= 1")
    return values


def _print_sweep_grid(
    *,
    source_key: str,
    antibot: str,
    concurrency_steps: Sequence[int],
    rate_steps: Sequence[int],
    max_requests: int,
    cooldown_seconds: float,
    cell_timeout_seconds: float,
    proxy_label: str,
) -> None:
    """Print the planned sweep grid (ASCII-only — Windows cp1252)."""
    print("=" * 72)
    print(f"  Rate-limit probe plan -- source={source_key} antibot={antibot}")
    print(f"  Proxy: {proxy_label}")
    print("=" * 72)
    print(f"  Concurrency steps : {list(concurrency_steps)}")
    print(f"  Rate steps (c/min): {list(rate_steps)}")
    print(f"  Grid cells        : {len(concurrency_steps) * len(rate_steps)}")
    print(f"  Max requests cap  : {max_requests}")
    print(f"  Cooldown per cell : {cooldown_seconds}s")
    print(f"  Cell deadline     : {cell_timeout_seconds}s")
    print("=" * 72)


# ──────────────────────────── block classification ────────────────────────────


# Per-request OUTCOME buckets. The key distinction (the whole point of this rewrite):
# RATE_LIMITED is a TRUE anti-throttle signal; TIMEOUT/ERROR are slowness/connectivity
# and must NOT be conflated with rate-limiting — a slow or flaky site is not throttling.
_OUTCOME_OK = "ok"
_OUTCOME_RATE_LIMITED = "rate_limited"
_OUTCOME_TIMEOUT = "timeout"
_OUTCOME_ERROR = "error"


def _classify(record: RequestRecord) -> str:
    """Bucket one recorded outbound result into an OUTCOME (Claude's discretion).

    Returns one of ``_OUTCOME_OK`` / ``_OUTCOME_RATE_LIMITED`` / ``_OUTCOME_TIMEOUT`` /
    ``_OUTCOME_ERROR``:

    * ``rate_limited`` — a TRUE rate-limit / anti-bot block: HTTP 429, HTTP 403, a
      Cloudflare challenge (``record.cf_challenge``, reusing the
      ``framework.context.is_cf_challenge`` heuristic spirit), or a ``Retry-After``
      header. These are what drive the measured parallelism / calls-min ceiling.
    * ``timeout`` — the request died on the read/connect timeout (``exception_type``
      containing ``Timeout``). This is SLOWNESS, tracked for the latency-degradation
      signal — NOT counted as rate-limiting.
    * ``error`` — any other transport error (connection reset/read error) or a non-block
      HTTP error status (4xx other than 429/403, or 5xx without a CF marker).
    * ``ok`` — a normal success.
    """
    if record.cf_challenge:
        return _OUTCOME_RATE_LIMITED
    if record.retry_after is not None:
        return _OUTCOME_RATE_LIMITED
    if record.status_code in (429, 403):
        return _OUTCOME_RATE_LIMITED
    if record.exception_type is not None:
        if "Timeout" in record.exception_type:
            return _OUTCOME_TIMEOUT
        return _OUTCOME_ERROR
    if record.status_code is not None and record.status_code >= 400:
        return _OUTCOME_ERROR
    return _OUTCOME_OK


def _signal_label(record: RequestRecord) -> str:
    """A short human-readable label for WHY ``record`` got its outcome (detail dict)."""
    if record.cf_challenge:
        return "cf-challenge"
    if record.status_code == 429:
        return "http-429"
    if record.status_code == 403:
        return "http-403"
    if record.retry_after is not None:
        return "retry-after"
    if record.exception_type is not None:
        return f"transport:{record.exception_type}"
    if record.status_code is not None and record.status_code >= 400:
        return f"http-{record.status_code}"
    return _OUTCOME_OK


# ──────────────────────────── sweep engine ────────────────────────────


@dataclass
class CellResult:
    """One concurrency × rate grid cell's measured outcome.

    Outcomes are kept in SEPARATE buckets so rate-limiting is never conflated with
    slowness: ``rate_limited_count`` drives the measured ceiling, while
    ``timeout_count``/``error_count`` feed the latency-degradation signal only.
    """

    concurrency: int
    target_rate_per_min: int
    ok_count: int
    rate_limited_count: int
    timeout_count: int
    error_count: int
    signals: dict[str, int]
    latencies: list[float]

    @property
    def total(self) -> int:
        return (
            self.ok_count
            + self.rate_limited_count
            + self.timeout_count
            + self.error_count
        )

    @property
    def rate_limited_fraction(self) -> float:
        return self.rate_limited_count / self.total if self.total else 0.0

    @property
    def timeout_fraction(self) -> float:
        return self.timeout_count / self.total if self.total else 0.0

    @property
    def degradation_fraction(self) -> float:
        """Fraction of requests that timed out or errored (slowness, not throttling)."""
        return (
            (self.timeout_count + self.error_count) / self.total if self.total else 0.0
        )

    @property
    def hard_block_fraction(self) -> float:
        """Rate-limited + connection errors (EXCLUDING timeouts) — IP-ban heuristic."""
        return (
            (self.rate_limited_count + self.error_count) / self.total
            if self.total
            else 0.0
        )

    @property
    def is_rate_limited(self) -> bool:
        """True when this cell's TRUE rate-limit fraction crosses the threshold."""
        return self.total > 0 and self.rate_limited_fraction >= _RATE_LIMIT_THRESHOLD

    def latency_summary(self) -> dict[str, float]:
        """min / median / mean / p95 / max latency seconds (0.0s when empty)."""
        if not self.latencies:
            return {"min": 0.0, "median": 0.0, "mean": 0.0, "p95": 0.0, "max": 0.0}
        ordered = sorted(self.latencies)
        return {
            "min": ordered[0],
            "median": _percentile(ordered, 0.50),
            "mean": sum(ordered) / len(ordered),
            "p95": _percentile(ordered, 0.95),
            "max": ordered[-1],
        }

    @property
    def median_latency(self) -> float:
        return _percentile(sorted(self.latencies), 0.50) if self.latencies else 0.0


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list (empty -> 0.0)."""
    if not ordered:
        return 0.0
    rank = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[rank]


@dataclass
class CategoryResult:
    """The full sweep outcome for one endpoint category (search/manifest/image).

    ``planned_concurrency``/``planned_rates`` are the FULL configured grid axes, so the
    report can distinguish a level that was tested-and-clean from one that was never
    reached because the ``--max-requests`` budget ran out (Fix 2 — no untested level is
    ever reported as a measured ceiling).
    """

    category: str
    cells: list[CellResult]
    planned_concurrency: list[int]
    planned_rates: list[int]

    # ── what was actually exercised vs planned ──────────────────────────────────
    @property
    def tested_concurrency(self) -> list[int]:
        return sorted({cell.concurrency for cell in self.cells})

    @property
    def tested_rates(self) -> list[int]:
        return sorted({cell.target_rate_per_min for cell in self.cells})

    @property
    def untested_concurrency(self) -> list[int]:
        return sorted(set(self.planned_concurrency) - set(self.tested_concurrency))

    @property
    def untested_rates(self) -> list[int]:
        return sorted(set(self.planned_rates) - set(self.tested_rates))

    @property
    def parallelism_fully_tested(self) -> bool:
        return not self.untested_concurrency

    @property
    def rate_fully_tested(self) -> bool:
        return not self.untested_rates

    # ── true rate-limiting (drives the ceiling) ─────────────────────────────────
    @property
    def rate_limit_observed(self) -> bool:
        return any(cell.is_rate_limited for cell in self.cells)

    def first_rate_limit(self) -> CellResult | None:
        """The FIRST grid cell (in sweep order) to cross the rate-limit threshold."""
        for cell in self.cells:
            if cell.is_rate_limited:
                return cell
        return None

    @property
    def baseline_latency(self) -> float:
        """Median latency of the gentlest tested cell (lowest concurrency then rate)."""
        if not self.cells:
            return 0.0
        gentlest = min(self.cells, key=lambda c: (c.concurrency, c.target_rate_per_min))
        return gentlest.median_latency

    def is_degraded(self, cell: CellResult) -> bool:
        """True when ``cell`` shows latency degradation vs the gentlest baseline.

        Either the cell's median latency blew past the baseline by
        ``_LATENCY_DEGRADATION_FACTOR``, or its timeout+error fraction crossed
        ``_DEGRADATION_FRACTION`` (requests dying on the read timeout under load).
        """
        if cell.total == 0:
            return False
        base = self.baseline_latency
        slow = base > 0 and cell.median_latency >= base * _LATENCY_DEGRADATION_FACTOR
        flaky = cell.degradation_fraction >= _DEGRADATION_FRACTION
        return slow or flaky

    @property
    def degradation_observed(self) -> bool:
        return any(self.is_degraded(cell) for cell in self.cells)

    def first_degradation(self) -> CellResult | None:
        for cell in self.cells:
            if self.is_degraded(cell):
                return cell
        return None

    # ── tolerated parallelism / rate (NO untested level reported as a ceiling) ───
    def max_parallelism_clean(self) -> int | None:
        """Highest TESTED concurrency with no rate-limit and no degradation at any rate.

        ``None`` when nothing was tested. Read with ``parallelism_fully_tested``: if
        that is False this is a FLOOR ("at least"), since higher levels never ran.
        """
        bad = {
            cell.concurrency
            for cell in self.cells
            if cell.is_rate_limited or self.is_degraded(cell)
        }
        clean = {c for c in self.tested_concurrency if c not in bad}
        return max(clean) if clean else None

    def calls_per_min_clean(self) -> int | None:
        """Highest TESTED rate with no rate-limit and no degradation at any concurrency.

        ``None`` when nothing was tested. A FLOOR when ``rate_fully_tested`` is False.
        """
        bad = {
            cell.target_rate_per_min
            for cell in self.cells
            if cell.is_rate_limited or self.is_degraded(cell)
        }
        clean = {r for r in self.tested_rates if r not in bad}
        return max(clean) if clean else None

    # ── #101 feedback value ─────────────────────────────────────────────────────
    def suggested_rate_per_min(self) -> int | None:
        """Safety-margin suggestion for ``rate_limit_per_minute`` (feeds back to #101).

        ``None`` when no clean rate was observed. See :meth:`suggested_basis` for how to
        read it: when no limit was hit AND the rate axis was not fully tested, this is a
        FLOOR, not a ceiling.
        """
        clean = self.calls_per_min_clean()
        if clean is None:
            return None
        return int(clean * _SUGGESTED_RATE_SAFETY_FRACTION)

    def suggested_basis(self) -> str:
        """How the suggested value should be read (honest about what was measured)."""
        if self.calls_per_min_clean() is None:
            return "no clean cell (all tested rates rate-limited or degraded)"
        if self.rate_limit_observed:
            return "ceiling (safety fraction of the highest rate before a rate-limit)"
        if self.degradation_observed:
            return "soft ceiling (safety fraction of the highest rate before slowdown)"
        if not self.rate_fully_tested:
            return (
                "FLOOR -- no limit hit; higher rates not tested (raise --max-requests)"
            )
        return "FLOOR -- no limit hit across the full tested grid"


async def _run_cell(
    captured: CapturedRequest,
    transport: InstrumentedTransport,
    *,
    concurrency: int,
    target_rate_per_min: int,
    remaining_budget: int,
    cell_deadline_seconds: float,
) -> CellResult:
    """Replay ``captured`` across one grid cell and tally ok/blocked outcomes.

    Replay = re-issue the SAME captured method+url+kwargs through the instrumented
    transport directly (bypassing the source's parsing but still through the real
    httpx client + pinned proxy + injected clearance). Concurrency is controlled by an
    ``asyncio.Semaphore`` sized to ``concurrency``; rate by pacing the dispatch to
    ``target_rate_per_min`` (NOT by ``SourceContext._limiter`` — we want the SITE's
    limit). The number of requests this cell issues is bounded by ``remaining_budget``
    (the global ``--max-requests`` cap) so even aggressive mode stays controlled.
    """
    # One "burst" per cell: issue up to min(target rate, remaining budget) requests,
    # paced to the target rate, bounded by the concurrency semaphore. A WALL-CLOCK
    # deadline caps the whole cell so a slow/dead proxy or a tarpitting site can't hang
    # the sweep: at concurrency=1, serial 30s read-timeouts would otherwise stretch a
    # 120-request cell toward an hour. Hitting the deadline is itself a strong slowness
    # signal (the cell could not drain in its time budget) and feeds degradation.
    n_requests = max(1, min(target_rate_per_min, remaining_budget))
    interval = 60.0 / target_rate_per_min if target_rate_per_min > 0 else 0.0
    semaphore = asyncio.Semaphore(concurrency)
    transport.segment()  # isolate this cell's records
    deadline = time.monotonic() + cell_deadline_seconds

    async def _one() -> None:
        async with semaphore:
            try:
                resp = await transport.request(
                    captured.method, captured.url, **captured.kwargs
                )
            except httpx.HTTPError:
                return  # already recorded by the instrumented transport
            # Drain the body so cf-challenge classification can read content.
            with contextlib.suppress(Exception):
                _ = resp.content

    tasks: list[asyncio.Task[None]] = []
    for i in range(n_requests):
        if time.monotonic() >= deadline:
            break  # stop DISPATCHING once the cell's time budget is spent
        tasks.append(asyncio.create_task(_one()))
        if interval and i < n_requests - 1:
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    # Await outstanding tasks up to the deadline, then cancel stragglers (requests too
    # slow to complete in the cell window). Cancelled tasks raise CancelledError, which
    # the instrumented transport does NOT record, so we count them here as a deadline
    # signal folded into the cell's degradation (timeout) bucket.
    deadline_cancelled = 0
    if tasks:
        remaining = max(0.0, deadline - time.monotonic())
        _done, pending = await asyncio.wait(tasks, timeout=remaining)
        deadline_cancelled = len(pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    cell_rows = transport.segment()
    counts = {
        _OUTCOME_OK: 0,
        _OUTCOME_RATE_LIMITED: 0,
        _OUTCOME_TIMEOUT: 0,
        _OUTCOME_ERROR: 0,
    }
    signals: dict[str, int] = {}
    latencies: list[float] = []
    for row in cell_rows:
        latencies.append(row.latency_seconds)
        outcome = _classify(row)
        counts[outcome] += 1
        if outcome != _OUTCOME_OK:
            label = _signal_label(row)
            signals[label] = signals.get(label, 0) + 1
    if deadline_cancelled:
        # Deadline-cancelled requests are slowness deaths: count toward timeouts (so
        # they drive the degradation signal) and record their (capped) latency.
        counts[_OUTCOME_TIMEOUT] += deadline_cancelled
        signals["cell-deadline"] = deadline_cancelled
        latencies.extend([cell_deadline_seconds] * deadline_cancelled)
    return CellResult(
        concurrency=concurrency,
        target_rate_per_min=target_rate_per_min,
        ok_count=counts[_OUTCOME_OK],
        rate_limited_count=counts[_OUTCOME_RATE_LIMITED],
        timeout_count=counts[_OUTCOME_TIMEOUT],
        error_count=counts[_OUTCOME_ERROR],
        signals=signals,
        latencies=latencies,
    )


async def _sweep_category(
    captured: CapturedRequest,
    transport: InstrumentedTransport,
    *,
    concurrency_steps: Sequence[int],
    rate_steps: Sequence[int],
    cooldown_seconds: float,
    cell_deadline_seconds: float,
    budget: list[int],
) -> CategoryResult:
    """Run the FULL concurrency × rate grid for one category (aggressive sweep).

    Completes the WHOLE grid even after hitting a limit (maps the full
    penalty/recovery curve) — :meth:`CategoryResult.first_rate_limit` marks where a true
    rate-limit first appears, :meth:`CategoryResult.first_degradation` where the site
    first slows down. ``budget`` is a one-element mutable list carrying the
    remaining global ``--max-requests`` allowance, decremented across cells so the cap
    spans ALL categories. A short cooldown between cells makes recovery observable.
    """
    cells: list[CellResult] = []
    for concurrency in concurrency_steps:
        for rate in rate_steps:
            if budget[0] <= 0:
                break
            cell = await _run_cell(
                captured,
                transport,
                concurrency=concurrency,
                target_rate_per_min=rate,
                remaining_budget=budget[0],
                cell_deadline_seconds=cell_deadline_seconds,
            )
            budget[0] -= cell.total
            cells.append(cell)
            mark = " <== FIRST RATE-LIMIT BLOCK" if cell.is_rate_limited else ""
            print(
                f"  [{captured.category}] c={concurrency} rate={rate}/min "
                f"ok={cell.ok_count} rate_limited={cell.rate_limited_count} "
                f"timeout={cell.timeout_count} error={cell.error_count} "
                f"median={cell.median_latency:.2f}s{mark}"
            )
            if cooldown_seconds > 0 and budget[0] > 0:
                await asyncio.sleep(cooldown_seconds)
        if budget[0] <= 0:
            break
    return CategoryResult(
        category=captured.category,
        cells=cells,
        planned_concurrency=list(concurrency_steps),
        planned_rates=list(rate_steps),
    )


# ──────────────────────────── report ────────────────────────────


def _build_report(
    *,
    source_key: str,
    antibot: str,
    proxy: ProxyEntry | None,
    warmup: WarmupResult,
    category_results: list[CategoryResult],
    concurrency_steps: Sequence[int],
    rate_steps: Sequence[int],
    max_requests: int,
) -> dict[str, Any]:
    """Build the JSON report dict (proxy referenced by MASKED index only).

    NEVER writes any proxy value (server/user/pass/url) — only ``_mask_proxy`` output.
    Answers all three user questions per endpoint category (max parallelism, calls/min
    ceiling, per-endpoint difference) plus a suggested ``rate_limit_per_minute``.
    """
    per_category: dict[str, Any] = {}
    for result in category_results:
        per_category[result.category] = {
            # Fix 1 — rate-limiting kept distinct from slowness.
            "rate_limit_observed": result.rate_limit_observed,
            "first_rate_limit": _cell_to_dict(result.first_rate_limit(), result),
            # Fix 3 — latency-degradation (soft-throttle) signal, separate axis.
            "degradation_observed": result.degradation_observed,
            "first_degradation": _cell_to_dict(result.first_degradation(), result),
            "baseline_latency_seconds": round(result.baseline_latency, 3),
            # Fix 2 — never report an untested level as a measured ceiling.
            "max_parallelism_clean": result.max_parallelism_clean(),
            "parallelism_fully_tested": result.parallelism_fully_tested,
            "concurrency_tested": result.tested_concurrency,
            "concurrency_not_tested": result.untested_concurrency,
            "calls_per_min_clean": result.calls_per_min_clean(),
            "rate_fully_tested": result.rate_fully_tested,
            "rate_tested": result.tested_rates,
            "rate_not_tested": result.untested_rates,
            # #101 feedback (read with the basis string — may be a FLOOR not a ceiling).
            "suggested_rate_per_minute": result.suggested_rate_per_min(),
            "suggested_basis": result.suggested_basis(),
            "cells": [_cell_to_dict(cell, result) for cell in result.cells],
        }
    # Per-endpoint difference is judged on the clean ceiling (None-aware).
    ceilings = {r.category: r.calls_per_min_clean() for r in category_results}
    suggestions = [
        s
        for s in (r.suggested_rate_per_min() for r in category_results)
        if s is not None
    ]
    return {
        "source_key": source_key,
        "antibot": antibot,
        "generated_at": datetime.now(UTC).isoformat(),
        # SECURITY (T-w1k-01): masked label ONLY — never a proxy value.
        "proxy": _mask_proxy(proxy),
        "grid": {
            "concurrency_steps": list(concurrency_steps),
            "rate_steps": list(rate_steps),
            "max_requests": max_requests,
        },
        "warmup_notes": warmup.notes,
        "endpoints": per_category,
        "limits_differ_across_endpoints": len(set(ceilings.values())) > 1,
        # Overall = the most conservative per-endpoint suggestion (None if none clean).
        "suggested_rate_per_minute": (min(suggestions) if suggestions else None),
    }


def _cell_to_dict(
    cell: CellResult | None, result: CategoryResult
) -> dict[str, Any] | None:
    """Serialize one ``CellResult`` (or None) for the JSON report."""
    if cell is None:
        return None
    return {
        "concurrency": cell.concurrency,
        "target_rate_per_min": cell.target_rate_per_min,
        "ok": cell.ok_count,
        "rate_limited": cell.rate_limited_count,
        "timeout": cell.timeout_count,
        "error": cell.error_count,
        "rate_limited_fraction": round(cell.rate_limited_fraction, 3),
        "degradation_fraction": round(cell.degradation_fraction, 3),
        "signals": cell.signals,
        "latency_seconds": {k: round(v, 3) for k, v in cell.latency_summary().items()},
        "is_rate_limited": cell.is_rate_limited,
        "is_degraded": result.is_degraded(cell),
    }


def _write_report(report: dict[str, Any], source_key: str) -> Path:
    """Write the JSON report under ``_REPORT_DIR`` and return its path."""
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _REPORT_DIR / f"{source_key}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def _print_console_summary(report: dict[str, Any], report_path: Path) -> None:
    """Print the ASCII-only console summary (Windows cp1252 — no arrow/glyph chars)."""
    print("=" * 72)
    print(f"  RATE-LIMIT PROBE SUMMARY -- source={report['source_key']}")
    print(f"  Proxy: {report['proxy']}")
    print("=" * 72)
    endpoints: dict[str, Any] = report["endpoints"]
    if not endpoints:
        print("  No endpoint categories captured (see warmup_notes in the report).")
    for category, data in endpoints.items():
        print(f"  [{category}]")

        # 1) True rate-limiting (429/403/CF/Retry-After) — the hard ceiling.
        if data["rate_limit_observed"]:
            rl = data["first_rate_limit"]
            print(
                f"    rate-limit block          : YES, first at "
                f"c={rl['concurrency']} rate={rl['target_rate_per_min']}/min "
                f"(signals={rl['signals']})"
            )
        else:
            print("    rate-limit block          : none (no 429/403/CF/Retry-After)")

        # 3) Latency degradation (the site slowing down / timing out under load).
        if data["degradation_observed"]:
            dg = data["first_degradation"]
            print(
                f"    latency degradation       : YES, first at "
                f"c={dg['concurrency']} rate={dg['target_rate_per_min']}/min "
                f"(median {dg['latency_seconds']['median']}s vs baseline "
                f"{data['baseline_latency_seconds']}s, "
                f"timeouts+errors {dg['degradation_fraction']:.0%})"
            )
        else:
            print("    latency degradation       : none (no slowdown / timeouts)")

        # 2) Parallelism / rate — never claim an untested level as a measured limit.
        par = data["max_parallelism_clean"]
        par_txt = "untested" if par is None else f"{par}"
        if not data["parallelism_fully_tested"] and data["concurrency_not_tested"]:
            par_txt += f" (NOT reached: {data['concurrency_not_tested']} -- budget)"
        elif par is not None:
            par_txt += " (full axis tested)"
        print(f"    max parallelism clean     : {par_txt}")

        rate = data["calls_per_min_clean"]
        rate_txt = "untested" if rate is None else f"{rate}/min"
        if not data["rate_fully_tested"] and data["rate_not_tested"]:
            rate_txt += f" (NOT reached: {data['rate_not_tested']} -- budget)"
        elif rate is not None:
            rate_txt += " (full axis tested)"
        print(f"    calls/min clean           : {rate_txt}")

        sug = data["suggested_rate_per_minute"]
        sug_txt = "n/a" if sug is None else str(sug)
        print(f"    SUGGESTED rate_per_minute : {sug_txt}")
        print(f"      basis                   : {data['suggested_basis']}")
    print("-" * 72)
    differ = "YES" if report["limits_differ_across_endpoints"] else "no"
    print(f"  Limits differ across endpoints: {differ}")
    overall = report["suggested_rate_per_minute"]
    overall_txt = "n/a (no clean cell)" if overall is None else str(overall)
    print(f"  OVERALL SUGGESTED rate_limit_per_minute (closes #101): {overall_txt}")
    if report["warmup_notes"]:
        print("-" * 72)
        print("  Warm-up notes (endpoints not captured):")
        for note in report["warmup_notes"]:
            if not note.get("captured", True):
                print(f"    - {note['category']}: {note['reason']}")
    print("=" * 72)
    print(f"  JSON report: {report_path}")
    print("=" * 72)


def _cmd_list_sources(registry: SourceRegistry) -> int:
    """Print registered source keys (no network, no proxy)."""
    for key in registry.keys():
        print(key)
    return 0


async def _cmd_dry_run(args: argparse.Namespace, registry: SourceRegistry) -> int:
    """Construct all seams and print the planned grid — ZERO outbound requests."""
    source_cls = registry.get(args.source)
    if source_cls is None:
        print(
            f"[probe] ERROR: unknown source {args.source!r}. Known: {registry.keys()}",
            file=sys.stderr,
        )
        return 2
    source = source_cls()
    concurrency_steps = _parse_int_steps(args.concurrency_steps)
    rate_steps = _parse_int_steps(args.rate_steps)

    # Build the seams (no proxy in dry-run — the no-proxy path must work without
    # proxies.txt). Constructing them proves the wiring resolves.
    settings = _build_settings(None)
    inner = HttpxTransport(settings)
    transport = InstrumentedTransport(inner)
    session = SessionManager(transport)
    ratelimiter = RateLimiter()
    handle_store = HandleStore()
    solver = _build_solver(source_cls, settings, None)
    _build_context(
        source,
        session=session,
        ratelimiter=ratelimiter,
        handle_store=handle_store,
        solver=solver,
    )
    # No outbound request happens; close the client we opened.
    await transport.aclose()

    _print_sweep_grid(
        source_key=source.key,
        antibot=source.antibot,
        concurrency_steps=concurrency_steps,
        rate_steps=rate_steps,
        max_requests=args.max_requests,
        cooldown_seconds=args.cooldown_seconds,
        cell_timeout_seconds=args.cell_timeout_seconds,
        proxy_label=_mask_proxy(None),
    )
    print("[probe] dry-run: seams constructed, NO outbound request issued.")
    return 0


async def _async_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    registry = _build_registry()

    if args.list_sources:
        return _cmd_list_sources(registry)

    if args.source is None:
        parser.error("--source is required (or use --list-sources)")

    if args.dry_run:
        return await _cmd_dry_run(args, registry)

    # The live sweep path is built in Task 2.
    return await _cmd_live(args, registry)


def _load_required_proxies(args: argparse.Namespace) -> list[ProxyEntry]:
    """Load proxies.txt, enforcing the proxy-REQUIRED default (T-w1k-03).

    Refuses to run if proxies.txt is missing/empty and ``--no-proxy`` was not passed.
    """
    proxies = _load_proxies(_PROXIES_PATH)
    if not proxies:
        raise SystemExit(
            "[probe] ERROR: no proxy available. A live sweep REQUIRES a pinned proxy "
            f"from {_PROXIES_PATH.name} (host:port:user:pass per line). Add one, or "
            "pass --no-proxy to explicitly run on your own IP."
        )
    return proxies


def _validate_index(index: int, proxies: list[ProxyEntry]) -> None:
    if not 0 <= index < len(proxies):
        raise SystemExit(
            f"[probe] ERROR: --proxy-index {index} out of range "
            f"(have {len(proxies)} proxies, indices 0..{len(proxies) - 1})."
        )


async def _resolve_proxy_candidates(
    args: argparse.Namespace,
) -> list[ProxyEntry | None]:
    """Resolve the ORDERED proxy candidates for a live run (first pinned, rest rotate).

    Each measurement still uses exactly ONE pinned proxy (LOCKED — never fan a single
    measurement across IPs); the list only provides rotate-on-failure fallbacks for the
    warm-up. Precedence:

    * ``--no-proxy`` -> ``[None]`` (explicit local-IP opt-out).
    * ``--no-health-check`` -> ``[--proxy-index or 0]`` (single pin, no rotation).
    * health-check on, ``--proxy-index`` set -> that index first, then other healthy
      proxies (fastest-first) as fallbacks.
    * health-check on, no index -> healthy proxies fastest-first (the default path).
    """
    if args.no_proxy:
        print(
            "[probe] WARNING: --no-proxy set — sweeping on the LOCAL IP (no proxy).",
            file=sys.stderr,
        )
        return [None]

    proxies = _load_required_proxies(args)

    if not args.health_check:
        index = 0 if args.proxy_index is None else int(args.proxy_index)
        _validate_index(index, proxies)
        return [proxies[index]]

    healthy = await _select_healthy_proxies(
        proxies, max_latency=args.max_proxy_latency_seconds
    )

    if args.proxy_index is not None:
        index = int(args.proxy_index)
        _validate_index(index, proxies)
        chosen = proxies[index]
        if not any(h.proxy.index == index for h in healthy):
            print(
                f"[probe] WARNING: --proxy-index {index} ({_mask_proxy(chosen)}) "
                "failed the health check; pinning it anyway as you asked.",
                file=sys.stderr,
            )
        fallbacks = [h.proxy for h in healthy if h.proxy.index != index]
        return [chosen, *fallbacks]

    if not healthy:
        raise SystemExit(
            "[probe] ERROR: no healthy proxy in the pool (all dead / bypassed / too "
            "slow). Fix the pool, raise --max-proxy-latency-seconds, or pass "
            "--no-health-check to pin one without checking."
        )
    return [h.proxy for h in healthy]


def _build_live_seams(
    source: Source, source_cls: type[Source], proxy: ProxyEntry | None
) -> tuple[InstrumentedTransport, NoopSolver | CloudflareSolver, SourceContext]:
    """Construct the per-proxy seams (transport/solver/context) for one live attempt.

    Built per attempt so rotate-on-failure gets a fresh transport + solver bound to the
    next proxy (the proxy is baked into Settings -> build_proxy -> both egress legs).
    """
    settings = _build_settings(proxy)
    transport = InstrumentedTransport(HttpxTransport(settings))
    session = SessionManager(transport)
    solver = _build_solver(source_cls, settings, proxy)
    ctx = _build_context(
        source,
        session=session,
        ratelimiter=RateLimiter(),
        handle_store=HandleStore(),
        solver=solver,
    )
    return transport, solver, ctx


async def _close_seams(
    transport: InstrumentedTransport, solver: NoopSolver | CloudflareSolver
) -> None:
    """Tear down a live attempt's transport + browser solver before rotate/exit."""
    await transport.aclose()
    if isinstance(solver, CloudflareSolver):
        with contextlib.suppress(Exception):
            await solver.aclose()


async def _warm_clearance_if_needed(
    source_cls: type[Source],
    solver: NoopSolver | CloudflareSolver,
) -> None:
    """Best-effort eager CF solve before the live warm-up (live path only).

    For a ``cloudflare*`` source the warm-up ``search()`` needs injected clearance; we
    eagerly solve so the captured request carries a valid cf_clearance + UA. A solve
    failure is non-fatal — the warm-up will simply record a graceful skip note.
    """
    if not isinstance(solver, CloudflareSolver):
        return
    with contextlib.suppress(Exception):
        await solver.get_clearance(source_cls.key)


_WarmupOutcome = tuple[
    ProxyEntry | None,
    InstrumentedTransport,
    NoopSolver | CloudflareSolver,
    WarmupResult,
]


async def _warmup_with_rotation(
    args: argparse.Namespace,
    source: Source,
    source_cls: type[Source],
    candidates: list[ProxyEntry | None],
) -> _WarmupOutcome | None:
    """Try candidate proxies in order until warm-up captures an endpoint (Tier 2).

    Returns ``(proxy, transport, solver, warmup)`` for the first proxy whose warm-up
    captured at least one endpoint, or ``None`` if every attempt failed. Each failed
    attempt's seams are closed before rotating. Bounded by ``--proxy-health-retries``.
    """
    attempts = candidates[: max(1, int(args.proxy_health_retries))]
    for i, proxy in enumerate(attempts, start=1):
        suffix = f" (attempt {i}/{len(attempts)})" if len(attempts) > 1 else ""
        print(f"[probe] pinned proxy: {_mask_proxy(proxy)}{suffix}")
        transport, solver, ctx = _build_live_seams(source, source_cls, proxy)
        warmup: WarmupResult | None = None
        try:
            await _warm_clearance_if_needed(source_cls, solver)
            print(f"[probe] warm-up: running search({args.query!r}) ...")
            warmup = await _capture_warmup(source, ctx, transport, query=args.query)
        except Exception as exc:  # noqa: BLE001 — warm-up failure -> rotate proxy
            print(
                f"[probe] warm-up errored on {_mask_proxy(proxy)}: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
        if warmup is not None and warmup.captured:
            return proxy, transport, solver, warmup
        if warmup is not None:
            for note in warmup.notes:
                print(f"  - {note['category']}: {note['reason']}", file=sys.stderr)
        await _close_seams(transport, solver)
        if i < len(attempts):
            print(
                f"[probe] no capture via {_mask_proxy(proxy)}; rotating to next proxy.",
                file=sys.stderr,
            )
    return None


async def _cmd_live(args: argparse.Namespace, registry: SourceRegistry) -> int:
    """Run the live aggressive sweep: warm-up capture -> grid sweep -> report.

    Guardrail: this path runs ONLY when explicitly invoked (NOT under --dry-run /
    --list-sources). The executor never runs a real sweep — the user owns live runs.
    """
    source_cls = registry.get(args.source)
    if source_cls is None:
        print(
            f"[probe] ERROR: unknown source {args.source!r}. Known: {registry.keys()}",
            file=sys.stderr,
        )
        return 2
    source = source_cls()
    concurrency_steps = _parse_int_steps(args.concurrency_steps)
    rate_steps = _parse_int_steps(args.rate_steps)

    candidates = await _resolve_proxy_candidates(args)
    outcome = await _warmup_with_rotation(args, source, source_cls, candidates)
    if outcome is None:
        print(
            "[probe] ERROR: no endpoint captured on any candidate proxy; cannot sweep.",
            file=sys.stderr,
        )
        return 3
    proxy, transport, solver, warmup = outcome

    try:
        print(f"[probe] captured categories: {sorted(warmup.captured)}")
        print("[probe] starting aggressive full sweep (whole grid) ...")
        budget = [args.max_requests]
        category_results: list[CategoryResult] = []
        for category in (_CATEGORY_SEARCH, _CATEGORY_MANIFEST, _CATEGORY_IMAGE):
            captured = warmup.captured.get(category)
            if captured is None:
                continue
            result = await _sweep_category(
                captured,
                transport,
                concurrency_steps=concurrency_steps,
                rate_steps=rate_steps,
                cooldown_seconds=args.cooldown_seconds,
                cell_deadline_seconds=args.cell_timeout_seconds,
                budget=budget,
            )
            category_results.append(result)
            if budget[0] <= 0:
                print("[probe] --max-requests cap reached; stopping sweep.")
                break
    finally:
        await _close_seams(transport, solver)

    report = _build_report(
        source_key=source.key,
        antibot=source.antibot,
        proxy=proxy,
        warmup=warmup,
        category_results=category_results,
        concurrency_steps=concurrency_steps,
        rate_steps=rate_steps,
        max_requests=args.max_requests,
    )
    report_path = _write_report(report, source.key)
    _print_console_summary(report, report_path)
    _advise_proxy_rotation(proxy, category_results)
    return 0


def _advise_proxy_rotation(
    proxy: ProxyEntry | None, category_results: list[CategoryResult]
) -> None:
    """If the pinned proxy looks IP-BANNED (grid-wide block), advise rotating forward.

    A sustained, grid-wide block (whole-run blocked fraction over
    :data:`_PROXY_BAN_FRACTION`) looks IP-level rather than a real rate ceiling — the
    measurement is invalid on this IP. Rotation is a BETWEEN-RUNS action (LOCKED: one
    proxy is pinned per run), so we advise the next ``--proxy-index`` rather than
    silently re-running on a different IP mid-measurement.
    """
    if proxy is None:
        return
    total = sum(c.total for r in category_results for c in r.cells)
    # Hard block = rate-limited + connection errors (NOT plain timeouts — a slow site is
    # not a banned IP). This is the grid-wide IP-ban heuristic.
    hard_blocked = sum(
        c.rate_limited_count + c.error_count for r in category_results for c in r.cells
    )
    if total == 0:
        return
    if hard_blocked / total >= _PROXY_BAN_FRACTION:
        print(
            f"[probe] WARNING: {_mask_proxy(proxy)} looks IP-BANNED "
            f"({hard_blocked}/{total} hard-blocked grid-wide). Re-run with "
            f"--proxy-index {proxy.index + 1} to rotate to the next proxy."
        )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(_async_main(argv))
    except KeyboardInterrupt:
        print("\n[probe] interrupted", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 — surface a clean traceback, never a half-crash
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
