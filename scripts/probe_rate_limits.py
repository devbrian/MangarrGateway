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
import sys
import time
import traceback
from dataclasses import dataclass, field
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

# Report output dir under the repo root (gitignored — see Task 2 / .gitignore).
_REPORT_DIR = _REPO_ROOT / "_rate_limit_probe_out"


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


async def _cmd_live(args: argparse.Namespace, registry: SourceRegistry) -> int:
    """Live sweep entry point (filled in Task 2)."""
    raise NotImplementedError("live sweep is implemented in Task 2")


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
