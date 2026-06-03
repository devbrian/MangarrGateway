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

SECURITY (T-w1k-01): ``proxies.txt`` holds secret residential proxy credentials.
This script NEVER prints a full proxy string, NEVER writes any proxy value into
the JSON report, and NEVER hardcodes a proxy value. Proxies are referenced by
host-only or a masked ``proxy[#index]``. The proxy is REQUIRED by default;
``--no-proxy`` is the explicit opt-out that runs the sweep on the local IP.

Usage::

    uv run python scripts/probe_rate_limits.py --list-sources
    uv run python scripts/probe_rate_limits.py --dry-run --source mangadex
    uv run python scripts/probe_rate_limits.py --source mangadex --proxy-index 0

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

# A grid cell is the "first sustained block" when its blocked fraction crosses this
# threshold. "Sustained" = the cell's own blocked-rate is at/above the threshold
# (we map the WHOLE grid regardless, but mark the first cell that crosses).
_BLOCK_THRESHOLD = 0.20

# Suggested rate_limit_per_minute = this safety fraction of the measured calls/min
# ceiling (the highest sustained rate with no sustained block). Conservative so the
# fed-back value (closing #101) stays comfortably under the real ceiling.
_SUGGESTED_RATE_SAFETY_FRACTION = 0.50

# A proxy is treated as IP-BANNED (rotate to the next) when its whole-grid blocked
# fraction crosses this — a sustained, grid-wide block that looks IP-level.
_PROXY_BAN_FRACTION = 0.80


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
        default=0,
        help="Starting proxy index in proxies.txt to pin (rotate forward on ban).",
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
    print("=" * 72)


# ──────────────────────────── block classification ────────────────────────────


def _is_blocked(record: RequestRecord) -> bool:
    """Classify one recorded outbound result as blocked vs ok (Claude's discretion).

    "blocked" = any of (CONTEXT.md required coverage):

    * HTTP 429 (rate limited);
    * HTTP 403 (forbidden — includes a Cloudflare challenge 403, flagged separately
      via ``record.cf_challenge`` reusing the ``framework.context.is_cf_challenge``
      heuristic spirit);
    * a 503 carrying a Cloudflare challenge marker (``record.cf_challenge``);
    * a ``Retry-After`` header present on the response;
    * a transport error / connection reset / timeout (``httpx.TransportError`` →
      recorded as ``exception_type`` with no ``status_code``).
    """
    if record.exception_type is not None:
        return True
    if record.retry_after is not None:
        return True
    if record.cf_challenge:
        return True
    return record.status_code in (429, 403)


def _block_signal(record: RequestRecord) -> str | None:
    """A short human-readable label for WHY ``record`` is blocked (or None if ok)."""
    if record.exception_type is not None:
        return f"transport:{record.exception_type}"
    if record.cf_challenge:
        return "cf-challenge"
    if record.status_code == 429:
        return "http-429"
    if record.status_code == 403:
        return "http-403"
    if record.retry_after is not None:
        return "retry-after"
    return None


# ──────────────────────────── sweep engine ────────────────────────────


@dataclass
class CellResult:
    """One concurrency × rate grid cell's measured outcome."""

    concurrency: int
    target_rate_per_min: int
    ok_count: int
    blocked_count: int
    block_signals: dict[str, int]
    latencies: list[float]

    @property
    def total(self) -> int:
        return self.ok_count + self.blocked_count

    @property
    def blocked_fraction(self) -> float:
        return self.blocked_count / self.total if self.total else 0.0

    @property
    def is_sustained_block(self) -> bool:
        """True when this cell's blocked fraction crosses the sustained threshold."""
        return self.total > 0 and self.blocked_fraction >= _BLOCK_THRESHOLD

    def latency_summary(self) -> dict[str, float]:
        """min / mean / max latency seconds (0.0s when the cell issued nothing)."""
        if not self.latencies:
            return {"min": 0.0, "mean": 0.0, "max": 0.0}
        return {
            "min": min(self.latencies),
            "mean": sum(self.latencies) / len(self.latencies),
            "max": max(self.latencies),
        }


@dataclass
class CategoryResult:
    """The full sweep outcome for one endpoint category (search/manifest/image)."""

    category: str
    cells: list[CellResult]

    def first_sustained_block(self) -> CellResult | None:
        """The FIRST grid cell (in sweep order) whose block fraction crossed."""
        for cell in self.cells:
            if cell.is_sustained_block:
                return cell
        return None

    def max_parallelism(self) -> int:
        """Highest concurrency level with NO sustained block at any tested rate."""
        ok_levels = {
            cell.concurrency for cell in self.cells if not cell.is_sustained_block
        }
        return max(ok_levels) if ok_levels else 0

    def calls_per_min_ceiling(self) -> int:
        """Highest target rate sustained with NO sustained block at any concurrency."""
        ok_rates = {
            cell.target_rate_per_min
            for cell in self.cells
            if not cell.is_sustained_block
        }
        return max(ok_rates) if ok_rates else 0

    def suggested_rate_per_min(self) -> int:
        """Safety-margin fraction of the measured ceiling — feeds back into #101."""
        return int(self.calls_per_min_ceiling() * _SUGGESTED_RATE_SAFETY_FRACTION)


async def _run_cell(
    captured: CapturedRequest,
    transport: InstrumentedTransport,
    *,
    concurrency: int,
    target_rate_per_min: int,
    remaining_budget: int,
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
    # paced to the target rate, bounded by the concurrency semaphore.
    n_requests = max(1, min(target_rate_per_min, remaining_budget))
    interval = 60.0 / target_rate_per_min if target_rate_per_min > 0 else 0.0
    semaphore = asyncio.Semaphore(concurrency)
    transport.segment()  # isolate this cell's records

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
        tasks.append(asyncio.create_task(_one()))
        if interval and i < n_requests - 1:
            await asyncio.sleep(interval)
    await asyncio.gather(*tasks)

    cell_rows = transport.segment()
    ok = 0
    blocked = 0
    signals: dict[str, int] = {}
    latencies: list[float] = []
    for row in cell_rows:
        latencies.append(row.latency_seconds)
        if _is_blocked(row):
            blocked += 1
            signal = _block_signal(row) or "unknown"
            signals[signal] = signals.get(signal, 0) + 1
        else:
            ok += 1
    return CellResult(
        concurrency=concurrency,
        target_rate_per_min=target_rate_per_min,
        ok_count=ok,
        blocked_count=blocked,
        block_signals=signals,
        latencies=latencies,
    )


async def _sweep_category(
    captured: CapturedRequest,
    transport: InstrumentedTransport,
    *,
    concurrency_steps: Sequence[int],
    rate_steps: Sequence[int],
    cooldown_seconds: float,
    budget: list[int],
) -> CategoryResult:
    """Run the FULL concurrency × rate grid for one category (aggressive sweep).

    Completes the WHOLE grid even after hitting a limit (maps the full
    penalty/recovery curve) — :meth:`CategoryResult.first_sustained_block` marks where
    the block first appears. ``budget`` is a one-element mutable list carrying the
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
            )
            budget[0] -= cell.total
            cells.append(cell)
            mark = " <== FIRST SUSTAINED BLOCK" if cell.is_sustained_block else ""
            print(
                f"  [{captured.category}] c={concurrency} rate={rate}/min "
                f"ok={cell.ok_count} blocked={cell.blocked_count} "
                f"({cell.blocked_fraction:.0%}){mark}"
            )
            if cooldown_seconds > 0 and budget[0] > 0:
                await asyncio.sleep(cooldown_seconds)
        if budget[0] <= 0:
            break
    return CategoryResult(category=captured.category, cells=cells)


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
            "max_parallelism": result.max_parallelism(),
            "calls_per_min_ceiling": result.calls_per_min_ceiling(),
            "suggested_rate_per_minute": result.suggested_rate_per_min(),
            "first_sustained_block": _cell_to_dict(result.first_sustained_block()),
            "cells": [_cell_to_dict(cell) for cell in result.cells],
        }
    suggestions = {r.category: r.suggested_rate_per_min() for r in category_results}
    ceilings = {r.category: r.calls_per_min_ceiling() for r in category_results}
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
        "suggested_rate_per_minute": (min(suggestions.values()) if suggestions else 0),
    }


def _cell_to_dict(cell: CellResult | None) -> dict[str, Any] | None:
    """Serialize one ``CellResult`` (or None) for the JSON report."""
    if cell is None:
        return None
    return {
        "concurrency": cell.concurrency,
        "target_rate_per_min": cell.target_rate_per_min,
        "ok": cell.ok_count,
        "blocked": cell.blocked_count,
        "blocked_fraction": round(cell.blocked_fraction, 3),
        "block_signals": cell.block_signals,
        "latency_seconds": {k: round(v, 3) for k, v in cell.latency_summary().items()},
        "sustained_block": cell.is_sustained_block,
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
        print(f"    max parallelism tolerated : {data['max_parallelism']}")
        print(f"    calls/min ceiling         : {data['calls_per_min_ceiling']}")
        fsb = data["first_sustained_block"]
        if fsb is None:
            print("    first sustained block     : none observed in grid")
        else:
            print(
                f"    first sustained block     : c={fsb['concurrency']} "
                f"rate={fsb['target_rate_per_min']}/min "
                f"({fsb['blocked_fraction']:.0%} blocked, "
                f"signals={fsb['block_signals']})"
            )
        print(f"    SUGGESTED rate_per_minute : {data['suggested_rate_per_minute']}")
    print("-" * 72)
    differ = "YES" if report["limits_differ_across_endpoints"] else "no"
    print(f"  Limits differ across endpoints: {differ}")
    print(
        f"  OVERALL SUGGESTED rate_limit_per_minute (closes #101): "
        f"{report['suggested_rate_per_minute']}"
    )
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


def _resolve_pinned_proxy(args: argparse.Namespace) -> ProxyEntry | None:
    """Resolve the pinned proxy for a live run, enforcing the proxy-REQUIRED default.

    DEFAULT: require a proxy. If ``proxies.txt`` is missing/empty AND ``--no-proxy``
    was not passed, refuse to run the live sweep (never silently test on the user's own
    IP — T-w1k-03). ``--no-proxy`` returns ``None`` (the explicit local-IP opt-out).
    Otherwise the ``--proxy-index`` proxy is PINNED for the whole run (LOCKED: never fan
    a single measurement across proxies). Raises ``SystemExit`` with a clear message on
    the refuse path.
    """
    if args.no_proxy:
        print(
            "[probe] WARNING: --no-proxy set — sweeping on the LOCAL IP (no proxy).",
            file=sys.stderr,
        )
        return None
    proxies = _load_proxies(_PROXIES_PATH)
    if not proxies:
        raise SystemExit(
            "[probe] ERROR: no proxy available. A live sweep REQUIRES a pinned proxy "
            f"from {_PROXIES_PATH.name} (host:port:user:pass per line). Add one, or "
            "pass --no-proxy to explicitly run on your own IP."
        )
    index = int(args.proxy_index)
    if not 0 <= index < len(proxies):
        raise SystemExit(
            f"[probe] ERROR: --proxy-index {index} out of range "
            f"(have {len(proxies)} proxies, indices 0..{len(proxies) - 1})."
        )
    return proxies[index]


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

    proxy = _resolve_pinned_proxy(args)
    print(f"[probe] pinned proxy: {_mask_proxy(proxy)}")

    settings = _build_settings(proxy)
    inner = HttpxTransport(settings)
    transport = InstrumentedTransport(inner)
    session = SessionManager(transport)
    ratelimiter = RateLimiter()
    handle_store = HandleStore()
    solver = _build_solver(source_cls, settings, proxy)
    ctx = _build_context(
        source,
        session=session,
        ratelimiter=ratelimiter,
        handle_store=handle_store,
        solver=solver,
    )

    try:
        await _warm_clearance_if_needed(source_cls, solver)
        print(f"[probe] warm-up: running search({args.query!r}) ...")
        warmup = await _capture_warmup(source, ctx, transport, query=args.query)
        if not warmup.captured:
            print(
                "[probe] ERROR: no endpoint captured during warm-up; cannot sweep.",
                file=sys.stderr,
            )
            for note in warmup.notes:
                print(f"  - {note['category']}: {note['reason']}", file=sys.stderr)
            return 3

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
                budget=budget,
            )
            category_results.append(result)
            if budget[0] <= 0:
                print("[probe] --max-requests cap reached; stopping sweep.")
                break
    finally:
        await transport.aclose()
        if isinstance(solver, CloudflareSolver):
            with contextlib.suppress(Exception):
                await solver.aclose()

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
    blocked = sum(c.blocked_count for r in category_results for c in r.cells)
    if total == 0:
        return
    if blocked / total >= _PROXY_BAN_FRACTION:
        print(
            f"[probe] WARNING: {_mask_proxy(proxy)} looks IP-BANNED "
            f"({blocked}/{total} blocked grid-wide). Re-run with "
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
