"""FastAPI application factory + lifespan.

The single-process R1 seams are built ONCE in the lifespan and stowed on
``app.state`` (PLAT-02): an injectable ``Transport`` (SRC-04), the shared
``SessionManager``, a ``NoopSolver`` (BOT-01 default), a ``RateLimiter`` seam, an
empty ``SourceRegistry`` (SRC-01), and the 12h caps ``TTLCache`` (PLAT-04). Global
API-key auth (AUTH-01/D-02) is applied on the app so every route is covered, and
the contract JSON error model (ERR-01) is registered.

The full ``load_settings`` default wiring lands in Task 3.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import api_router
from .config import Settings
from .errors import register_error_handlers
from .framework.android_solver import AndroidSolver
from .framework.antibot import Clearance, CloudflareSolver
from .framework.cooldown import SourceFailureCooldown
from .framework.enum_cache import EnumerationCache
from .framework.health import SourceHealth
from .framework.proxy import build_proxy
from .framework.ratelimit import RateLimiter
from .framework.registry import SourceRegistry
from .framework.session import SessionManager
from .framework.session_prep import CsrfBootstrap
from .framework.solver_router import SolverRouter
from .framework.transport import HttpxTransport
from .handles.store import open_handle_store
from .jobs.manager import JobManager
from .jobs.store import open_store
from .logging_config import configure_logging
from .metrics.collector import Collector, set_collector
from .metrics.context import seed_request_ids
from .metrics.middleware import MetricsRequestMiddleware
from .metrics.routes import router as metrics_router
from .metrics.snapshot import MetricSnapshotStore, open_metric_store
from .metrics.store import InMemoryStore
from .security import require_api_key
from .sources import register_builtin_sources

if TYPE_CHECKING:
    from .framework.base import Source

# 12h caps TTL (PLAT-04).
_CAPS_TTL_SECONDS = 43_200

# Bound shutdown drain so a wedged in-flight job cannot hang teardown forever
# (CR-01). On timeout the stragglers are cancelled and shutdown proceeds.
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 30.0

# How often each D-37 recovery supervisor checks whether its cloudflare-gated
# source's breaker has tripped (cheap; the escalating +1h/+6h re-probe runs
# only once a trip is seen).
_RECOVERY_POLL_SECONDS = 60.0

_log = logging.getLogger("manga_gateway")

# Gateway-owned staging temp suffixes left by an interrupted archive (package.py).
# These — and ONLY these — are swept on startup; completed output files (.cbz/.cbt/
# folder dirs) are never touched (PLAT-03 / T-03-13).
_STAGING_GLOBS = ("*.cbz.tmp", "*.cbt.tmp", "*.folder.tmp")


def _sweep_staging(output_root: str) -> int:
    """Blocking: unlink orphan ``*.tmp`` staging artifacts under ``output_root``.

    A crash mid-archive can leave a partial ``*.cbz.tmp`` (or ``.cbt.tmp``/
    ``.folder.tmp``) staging temp that ``write_cbz``'s success path would otherwise
    have ``os.replace``d away (Runtime State Inventory). Walk the gateway-owned output
    root and remove only those staging temps; a completed output (``.cbz``/``.cbt``/a
    folder) is NEVER deleted (T-03-13). Offload via ``asyncio.to_thread`` (Pitfall 2).
    Returns the number of artifacts swept. A missing output root is a no-op.
    """
    root = Path(output_root)
    if not root.is_dir():
        return 0
    swept = 0
    for pattern in _STAGING_GLOBS:
        for temp in root.rglob(pattern):
            with suppress(OSError):
                if temp.is_dir():  # *.folder.tmp staging dir
                    for child in temp.glob("*"):
                        with suppress(OSError):
                            child.unlink()
                    temp.rmdir()
                else:
                    temp.unlink()
                swept += 1
    return swept


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _recovery_watchdog(
    health: SourceHealth,
    *,
    backoff_hours: tuple[int, int],
    sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    probe: Callable[[], Awaitable[bool]],
) -> None:
    """D-37 escalating-backoff recovery for a tripped per-source breaker.

    On a tripped breaker, re-probe the source on an ESCALATING schedule (+1h, then
    +6h by default) — NOT on the request path. A probe that succeeds resets the
    breaker (``record_success`` → re-enabled). This call returns after the last
    backoff step WITHOUT recovery, but the caller (``_supervise_source``) re-enters
    it after its next 60s poll — so a still-down source keeps being re-probed on a
    repeating +1h/+6h cadence and is NEVER pinned down until restart (#236). The
    escalating backoff (not a busy retry) is the doomed-warm-storm guard (D-37).

    ``sleep``/``probe`` are injectable so tests assert the schedule with a fake clock
    and no real waiting. The probe returns ``True`` on recovery, ``False`` otherwise.
    """
    for hours in backoff_hours:
        if health.is_enabled:
            return  # already recovered elsewhere — nothing to do
        await sleep(hours * 3600.0)
        recovered = await probe()
        if recovered:
            health.record_success()  # breaker resets → source re-enabled
            _log.info("Recovery watchdog re-enabled a source after a re-probe")
            return
    # Exhausted this backoff cycle without recovery. The caller's supervisor loop
    # re-enters the watchdog after its next poll, so the source is re-probed again
    # on the same +1h/+6h schedule rather than staying down until restart (#236).
    _log.warning("Recovery watchdog exhausted backoff cycle; will re-probe next cycle")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the R1 singleton seams once; tear the transport down on shutdown."""
    settings: Settings = app.state.settings
    # SRC-04 injectable seam. The transport derives its OWN httpx proxy from the
    # same build_proxy(settings) helper the solver_kwargs below use for the
    # browser leg — so both egress legs share one IP (cf_clearance is IP-bound,
    # issue #65). No extra wiring needed here: the transport reads settings.
    transport = HttpxTransport(settings)
    app.state.transport = transport
    # debug pool-starves-search-cooldown (2026-06-17): a SEPARATE client/pool for
    # the download surface so a large download backlog can never exhaust the
    # connection pool the search fan-out needs (which would trip every source's
    # 300s failure-cooldown → all-sources outage). Same settings → same UA / proxy
    # egress / per-request deadline; clearance rides per-request headers (not a
    # client cookie jar) so the two pools share ONE authenticated identity (R1).
    download_transport = HttpxTransport(settings)
    app.state.download_transport = download_transport
    app.state.session = SessionManager(transport, download_transport)  # R1 identity
    # SRC-01: build the registry first so the rest of the lifespan can inspect
    # per-source metadata (antibot level, decrypt scheme) WITHOUT hardcoding any
    # source key by name. Adding the 50+ planned sources is a register call and
    # zero edits to this shell (CLAUDE.md "framework knows no source by name").
    registry = SourceRegistry()
    # #198/#202: GATEWAY_DISABLED_SOURCES drops a source from the registry
    # entirely — absent from /caps, never searched, and (since cf_sources below
    # is derived from registry.items()) never warmed. Reversible via env.
    register_builtin_sources(registry, disabled=settings.disabled_source_keys())
    app.state.registry = registry
    # Derive the set of cloudflare-gated source classes from the registry rather
    # than hardcoding it — anything whose ``antibot`` declares cloudflare needs
    # both a SourceHealth breaker (D-38) and a slot in the solver's key set.
    cf_sources: dict[str, type] = {
        key: cls
        for key, cls in registry.items()
        if getattr(cls, "antibot", "none").startswith("cloudflare")
    }
    cloudflare_keys: frozenset[str] = frozenset(cf_sources)
    # Phase 10 (BOT-01/SRC-01/SRC-02): partition the cloudflare-gated sources by
    # ``Source.solver_engine`` (default ``"patchright"``). The PATCHRIGHT leg keeps
    # comix byte-for-byte unchanged; the ANDROID leg routes mangadot/kagane to the
    # redroid-WebView sidecar via the AndroidSolver. ``cloudflare_keys`` (the full
    # union) still feeds the ``source_health`` breaker map + the D-37 watchdog below
    # — EVERY cloudflare source gets a breaker + supervisor regardless of engine.
    engine_by_source: dict[str, str] = {
        key: getattr(cls, "solver_engine", "patchright")
        for key, cls in cf_sources.items()
    }
    patchright_keys: frozenset[str] = frozenset(
        key for key, engine in engine_by_source.items() if engine != "android"
    )
    android_keys: frozenset[str] = frozenset(
        key for key, engine in engine_by_source.items() if engine == "android"
    )
    # On-demand keys (debug pooltimeout-recurrence): cloudflare sources whose CF
    # managed challenge is INTERMITTENT (``cloudflare_challenge_optional=True``,
    # mangaball). Both solver legs SKIP these in ``warm()`` (no eager startup solve of
    # an absent challenge → no wasted redroid loop, no D-33 force-disable), and
    # ``_warm_solver`` never disables them — they solve on-demand when a request hits a
    # live challenge.
    on_demand_keys: frozenset[str] = frozenset(
        key
        for key, cls in cf_sources.items()
        if getattr(cls, "cloudflare_challenge_optional", False)
    )
    # Per-source health map (D-38): one breaker per cloudflare-gated source.
    # JobManager + the search route read this by reference (deps.get_source_health).
    source_health: dict[str, SourceHealth] = {
        key: SourceHealth(threshold=settings.cloudflare_breaker_threshold)
        for key in cloudflare_keys
    }
    app.state.source_health = source_health
    # D-01 session-prep provider: derive the csrf-bootstrap source keys from the
    # registry the SAME way cf_sources is derived above — anything declaring
    # ``session_prep == "csrf-bootstrap"`` (MangaBall, Plan 03) needs the framework
    # CSRF/session-bootstrap seam. Each such source's bootstrap HTML page is its
    # ``base_url`` (RECON §"Session / CSRF bootstrap": GET any HTML page → harvest the
    # meta csrf-token + PHPSESSID). Construct ONE shared CsrfBootstrap over the ONE
    # R1 session (never a second httpx client); construction is cheap and synchronous
    # — the bootstrap GET is lazy on first use (mirrors the solver's lazy launch).
    # Before MangaBall registers the key set is empty: prepare() returns None for
    # every key, so MangaDex/Comix are byte-for-byte unchanged.
    csrf_bootstrap_keys: dict[str, type[Source]] = {
        key: cls
        for key, cls in registry.items()
        if getattr(cls, "session_prep", None) == "csrf-bootstrap"
    }
    # Rate-limit seam — created here (before CsrfBootstrap) so the bootstrap GET can
    # share the SAME per-source limiter the SourceContext data path uses. One shared
    # RateLimiter instance for the whole lifespan (CsrfBootstrap + contexts + jobs).
    app.state.ratelimiter = RateLimiter()

    # Cloudflare union (debug mangaball-cloudflare-csrf-243): a csrf-bootstrap source
    # that is ALSO cloudflare-gated (MangaBall, after its 2026-06-15 site-wide managed
    # challenge) needs the bootstrap GET to ride cf_clearance — else it receives the CF
    # interstitial (no meta csrf-token) and every search/recent/download fails with
    # source_unavailable. The provider resolves the clearance LAZILY from the shared
    # ``solver`` (a SolverRouter built below in this same lifespan scope, long before
    # the first request triggers a bootstrap GET). For a non-cloudflare csrf source the
    # solver returns None → the bootstrap GET stays a bare httpx request (unchanged).
    async def _bootstrap_clearance(source_key: str) -> Clearance | None:
        # On-demand source (debug pooltimeout-recurrence): the csrf-bootstrap GET must
        # NOT eagerly solve — that is the same eager-clearance trap as the request
        # path. Peek held clearance only; the bootstrap GET then goes out without
        # cf_clearance (mangaball returns 200 when its challenge is off) and the
        # csrf-bootstrap's own force_refresh / the data path's challenge reconcile
        # escalate to a real solve only if a live challenge actually appears.
        if source_key in on_demand_keys:
            return await solver.get_clearance(source_key, solve_if_missing=False)
        return await solver.get_clearance(source_key)

    session_prep = CsrfBootstrap(
        keys=frozenset(csrf_bootstrap_keys),
        session=app.state.session,
        bootstrap_urls={key: cls.base_url for key, cls in csrf_bootstrap_keys.items()},
        ratelimiter=app.state.ratelimiter,
        # Same rate the SourceContext keys its limiter on → one shared AsyncLimiter
        # per source, so the bootstrap/refresh GET counts against the source budget.
        rates={
            key: cls.rate_limit_per_minute for key, cls in csrf_bootstrap_keys.items()
        },
        get_clearance=_bootstrap_clearance,
    )
    app.state.session_prep = session_prep
    # Build the per-domain Cloudflare challenge-URL map (#88): each
    # cloudflare-gated source maps its ``source_key`` -> its
    # ``cloudflare_challenge_url`` metadata so N sources each solve against their
    # OWN host (the framework solver itself never names a host). Sources without
    # a URL fall back to the framework solver default (an invalid.example
    # placeholder useful only for tests). With exactly one cf source registered
    # (comix today) this collapses to the historic single-domain behavior.
    # Phase 10: the Patchright leg's challenge-URL map is PARTITIONED to patchright
    # sources ONLY — so comix's browser warm/solve is byte-for-byte unchanged and
    # the unclearable-from-Linux mangadot/kagane NEVER enter the Patchright warm set
    # (they route to the Android sidecar leg below instead).
    challenge_urls: dict[str, str] = {
        key: url
        for key, cls in cf_sources.items()
        if key in patchright_keys
        and (url := getattr(cls, "cloudflare_challenge_url", None))
    }
    solver_kwargs: dict[str, Any] = {
        "user_data_dir": settings.cloudflare_user_data_dir,
        "headless": settings.cloudflare_headless,
        "solve_concurrency": settings.cloudflare_solve_concurrency,
        # PR #58 follow-up: per-source-shape concurrency cap for the warm
        # browser. Defaults to 3 (config.py cloudflare_fetch_concurrency) —
        # the patchright/Chromium engine can run that many concurrent CF
        # navigations on one warm context; tune via
        # GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY. A camoufox deploy MUST pin
        # this to 1 (the _reject_camoufox_parallel validator enforces it —
        # Firefox cannot run parallel CF navs).
        "fetch_concurrency": settings.cloudflare_fetch_concurrency,
        # Phase 10: the Patchright leg owns ONLY the patchright sources (comix); the
        # android sources are warmed/solved by the AndroidSolver leg below.
        "cloudflare_keys": patchright_keys,
        # warm() skips on-demand sources (intermittent challenge); patchright-side
        # intersection is empty today but threaded for correctness/symmetry.
        "on_demand_keys": on_demand_keys & patchright_keys,
        # #153: bounded eager-warm retry so a cold-deploy launch flake is absorbed
        # before a source is force_disabled (and so advertised down in /caps for 12h).
        "warm_attempts": settings.cloudflare_warm_attempts,
        "warm_retry_seconds": settings.cloudflare_warm_retry_seconds,
        # #35 / #40: select the stealth-browser engine. patchright (Chromium,
        # patched CDP leaks) is the DEFAULT since the comix-parallel-engine-probe
        # finding (2026-06-01) — only Chromium runs concurrent CF navigations, so
        # it is what makes fetch_concurrency > 1 work. camoufox (Firefox, C++
        # fingerprint spoof) is the opt-in fallback via
        # GATEWAY_CLOUDFLARE_ENGINE=camoufox for hosts where Chromium's
        # fingerprint is flagged (must pin fetch_concurrency=1). Driven by
        # Settings.cloudflare_engine.
        "engine": settings.cloudflare_engine,
        # #54 diagnostic: forward the per-page browser-event capture flag.
        # OFF by default — flip to ON via GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1
        # for nightly evidence-capture runs investigating the Playwright
        # Firefox handler crash (see debug session
        # ``comix-pageerror-throw-site-54.md``).
        "log_browser_events": settings.cloudflare_log_browser_events,
    }
    if challenge_urls:
        solver_kwargs["challenge_urls"] = challenge_urls
    # PROXY-01 / #65: build the proxy ONCE from settings. The Playwright dict
    # (first element) threads into the solver's browser launch closures; the
    # transport built above derives the httpx leg (second element) from the SAME
    # helper, so both egress legs share one IP (cf_clearance is IP-bound). Gated
    # like challenge_url: an unconfigured deploy adds no ``proxy`` key (no
    # regression). Never log the proxy server/username/password.
    playwright_proxy, _ = build_proxy(settings)
    if playwright_proxy is not None:
        solver_kwargs["proxy"] = playwright_proxy
    # Swap NoopSolver for the ONE shared solver (R1/BOT-01). Construction is cheap
    # (no browser yet — the lazy engine-specific launch happens on the first solve);
    # the eager warm() is fired NON-BLOCKING so a launch/solve failure degrades only
    # cloudflare-gated sources and NEVER aborts startup (D-33/Pitfall 3). Non-cloudflare
    # sources (e.g. ``antibot="none"``) resolve no-clearance.
    #
    # Phase 10: the shared solver is a SolverRouter composing two backends —
    #  * the Patchright CloudflareSolver (comix, byte-for-byte unchanged), and
    #  * an AndroidSolver that mints cf_clearance for mangadot/kagane via the
    #    android-solver sidecar over HTTP (R1 — no Android machinery in-process).
    # The router dispatches get_clearance/warm/aclose per source via the
    # ``engine_by_source`` map, so JobManager, the D-37 watchdog, and ``_warm_solver``
    # below consume it through the SAME AntiBotSolver surface with no call-site change.
    cloudflare_solver = CloudflareSolver(**solver_kwargs)
    # The Android leg's challenge-url map covers ONLY the android sources; an
    # unconfigured ``android_solver_url`` makes warm() boot all android keys disabled
    # (D-33) so the gate / CI / a local box without redroid stays green.
    android_challenge_urls: dict[str, str] = {
        key: url
        for key, cls in cf_sources.items()
        if key in android_keys
        and (url := getattr(cls, "cloudflare_challenge_url", None))
    }
    android_solver = AndroidSolver(
        base_url=settings.android_solver_url,
        api_key=settings.android_solver_api_key,
        challenge_urls=android_challenge_urls,
        # warm() skips on-demand android sources (mangaball) — they solve on-demand,
        # never eager (debug pooltimeout-recurrence).
        on_demand_keys=on_demand_keys & android_keys,
        timeout_s=settings.android_solver_timeout_s,
        # Req 7: reuse the SAME ``playwright_proxy`` already built once above for
        # the CloudflareSolver (no second build_proxy call, no new setting). The
        # sidecar's CF-solve egress then matches the gateway's httpx-fetch egress
        # for the minted clearance. ``None`` when ``cloudflare_proxy_*`` is
        # unconfigured ⇒ no proxy in the /solve body (D-08). Never logged.
        proxy=playwright_proxy,
    )
    solver = SolverRouter(
        patchright=cloudflare_solver,
        android=android_solver,
        engine_by_source=engine_by_source,
    )
    app.state.solver = solver

    async def _warm_solver() -> None:
        # #88 / PR#90 review: per-domain warm isolation. ``solver.warm()`` returns
        # the cloudflare keys whose eager solve FAILED; disable ONLY those. With
        # several cloudflare domains now sharing one solver, a single bad domain at
        # startup must not force-disable the healthy ones. A catastrophic warm()
        # raise (e.g. the recycle-watchdog/launch path itself) still falls back to
        # disabling every cloudflare source (D-33/Pitfall 3 — gateway lives).
        try:
            failed = await solver.warm()  # eager best-effort solve + recycle watchdog
        except Exception:  # noqa: BLE001 — total failure: cloudflare sources boot disabled, gateway lives
            failed = list(cloudflare_keys)
            # #153: surface the real cause (exc_info) — a catastrophic warm()
            # raise (launch/watchdog path) was previously logged with no traceback.
            _log.warning(
                "CloudflareSolver warm failed; cloudflare-gated sources "
                "disabled (D-33)",
                exc_info=True,
            )
        # On-demand sources are never disabled for a warm failure: warm() already
        # skips them, but the catastrophic-raise fallback above sets failed=ALL cf
        # keys, so filter them out here too (debug pooltimeout-recurrence).
        failed = [key for key in failed if key not in on_demand_keys]
        for key in failed:
            source_health[key].force_disabled = True
        if failed:
            _log.warning(
                "Cloudflare warm failed for %d source(s) [%s] — those disabled (D-33)",
                len(failed),
                ", ".join(sorted(failed)),
            )

    warm_task = asyncio.create_task(_warm_solver())  # non-blocking (Pitfall 3)
    # (app.state.ratelimiter is created earlier, before CsrfBootstrap, so the
    # bootstrap GET shares the per-source limiter — see above.)
    app.state.caps_cache = TTLCache(maxsize=1, ttl=_CAPS_TTL_SECONDS)  # PLAT-04
    # Opaque downloadHandle store (HDL-01/02). D-16 REVERSAL (debug session
    # release-no-longer-resolvable): now SQLite-backed so handles survive a restart,
    # with a configurable TTL (default 6h, GATEWAY_HANDLE_TTL_SECONDS) that covers
    # Mangarr's grab-replay latency — the prior in-memory-only 60-min TTL was the
    # confirmed cause of the production "release no longer resolvable" misses.
    app.state.handle_store = await open_handle_store(
        settings.handle_db_path,
        ttl=settings.handle_ttl_seconds,
        maxsize=settings.handle_maxsize,
    )
    # CACHE-01: ONE process-wide enumeration cache for the whole lifespan (R1), built
    # from settings exactly like RateLimiter()/HandleStore() above. The per-source TTL
    # override map (D-09) is harvested from the registry the SAME way cf_sources /
    # csrf_bootstrap_keys are derived — anything declaring ``enum_cache_ttl_seconds``
    # contributes an override; the framework clamps each entry to the 60-min handle TTL
    # (CACHE-05). The POST /search route threads this into its SourceContext; the recent
    # + download paths leave the default None (CACHE-05).
    enum_cache_ttl_overrides: dict[str, int] = {
        key: cls.enum_cache_ttl_seconds
        for key, cls in registry.items()
        if cls.enum_cache_ttl_seconds is not None
    }
    app.state.enum_cache = EnumerationCache(
        maxsize=settings.enum_cache_maxsize,
        ttl=settings.enum_cache_ttl_seconds,
        enabled=settings.enum_cache_enabled,
        ttl_overrides=enum_cache_ttl_overrides,
        # CACHE-05: clamp every cached entry to the configured handle TTL so a cached
        # enumeration never out-lives the downloadHandle it would mint — anchored to
        # the live handle_ttl_seconds, not the legacy hard-coded 3600 (debug session
        # release-no-longer-resolvable, so a raised handle TTL is not silently clamped).
        max_ttl=settings.handle_ttl_seconds,
    )
    # 260606-lyb Change 2: ONE process-wide per-source failure cooldown for the whole
    # lifespan (R1), mirroring the enum_cache construction. The search/recent routes
    # thread it into fan_out so a hard-down source is skipped (zero upstream calls)
    # for source_failure_cooldown_seconds; 0 disables it entirely.
    app.state.failure_cooldown = SourceFailureCooldown(
        ttl_seconds=settings.source_failure_cooldown_seconds
    )
    # Download surface: aiosqlite job store + lifespan-owned JobManager (PLAT-03).
    store = await open_store(settings.db_path)
    job_manager = JobManager(
        store=store,
        registry=registry,
        session=app.state.session,
        ratelimiter=app.state.ratelimiter,
        handle_store=app.state.handle_store,
        settings=settings,
        solver=solver,
        source_health=source_health,
        session_prep=session_prep,
    )
    # download-jobs-failed-23: REQUEUE in-flight jobs (not fail them) + project rows
    # (PLAT-03). The re-spawn happens AFTER the staging sweep below.
    await job_manager.rehydrate()
    # Restart staging sweep (PLAT-03 / T-03-13): clear orphan *.tmp archives left by a
    # crash mid-package; completed output files are never touched. Blocking FS walk is
    # offloaded off the event loop (Pitfall 2). MUST run BEFORE resume_interrupted so a
    # resumed job's fresh *.tmp can never be swept away by the previous run's cleanup.
    swept = await asyncio.to_thread(_sweep_staging, settings.output_root)
    if swept:
        _log.info("Swept %d orphan staging artifact(s) on startup", swept)
    # download-jobs-failed-23: resume the jobs a redeploy/crash interrupted — they were
    # requeued by rehydrate above; re-spawn them so they finish ("jobs SHOULD survive
    # restart"). Bounded by the same global/per-source semaphores as a fresh submit.
    resumed = job_manager.resume_interrupted()
    if resumed:
        _log.info("Resumed %d job(s) interrupted by restart", resumed)
    app.state.job_manager = job_manager

    # Metrics system (OBS-04/05/06): open the SEPARATE snapshot DB, rehydrate the
    # last snapshot into the LIVE store (so a restart keeps the recent rings +
    # rollups), build the Collector over it and install it process-wide so every
    # framework seam (Plan 04) + the request middleware flip live. The rehydrated
    # store IS the live store — its rings are then re-bounded to the configured
    # sizes by replacing it with a freshly-bounded store seeded from the rehydrate.
    # WR-02 + open-resilience: the metrics subsystem is purely diagnostic and must
    # NEVER abort startup. Two failure modes degrade cleanly:
    #  (1) the snapshot DB cannot be OPENED — metrics_db_path is unwritable (the
    #      default /state volume is absent outside docker, e.g. CI / a bare run).
    #      sqlite3.OperationalError "unable to open database file" here would
    #      otherwise take the whole gateway down. → in-memory-only metrics, no
    #      restart survival (snapshot loop + final snapshot are skipped below).
    #  (2) the DB opens but rehydrate fails (corrupt / schema-drifted metrics.db —
    #      a changed MetricEvent shape, a partial-write JSONDecodeError, etc.).
    #      → keep the (writable) store for future snapshots, just start empty.
    metric_snapshot: MetricSnapshotStore | None = None
    try:
        metric_snapshot = await open_metric_store(settings.metrics_db_path)
    except Exception:  # noqa: BLE001 — unwritable/unavailable DB must not break startup
        _log.warning(
            "metrics snapshot store could not be opened (metrics_db_path=%s); "
            "continuing with in-memory-only metrics (no restart survival)",
            settings.metrics_db_path,
            exc_info=True,
        )
    # `rehydrated` is a TRANSIENT carrier: only its rollups are drained into the
    # live store below (rings are not rehydrated into memory — 260604-wm2). Its
    # slow_factor is therefore never consulted for classification, but we still
    # thread settings.metrics_slow_factor through (not a hardcoded constant) so the
    # carrier is consistent with the live store on every path.
    if metric_snapshot is None:
        rehydrated = InMemoryStore(slow_factor=settings.metrics_slow_factor)
    else:
        # The batch-size flush trigger comes from Settings (260604-wm2).
        metric_snapshot.configure(flush_max_batch=settings.metrics_ring_flush_max_batch)
        try:
            rehydrated = await metric_snapshot.rehydrate(
                slow_factor=settings.metrics_slow_factor
            )
        except Exception:  # noqa: BLE001 — bad snapshot must not break the service
            _log.warning(
                "metrics rehydrate failed; starting with an empty metric store",
                exc_info=True,
            )
            rehydrated = InMemoryStore(slow_factor=settings.metrics_slow_factor)
    # The LIVE store: this is the one handed to the Collector, so its slow_factor
    # (from Settings) is what actually drives "slow" classification at runtime.
    metric_store = InMemoryStore(slow_factor=settings.metrics_slow_factor)
    # Re-admit the rehydrated ROLLUPS verbatim (rollups stay in memory + survive
    # restart). Ring events are NOT rehydrated into memory anymore — they are the
    # on-disk system of record served by the disk ring store (260604-wm2).
    for rk, rollup in rehydrated.iter_rollups():
        metric_store.restore_rollup(rk, rollup)
    # Restart-monotonic request_id: seed the counter from the persisted ring MAX+1
    # (1 on an empty/missing/degraded DB) so ids never reset-and-collide on boot.
    seed_start = 1
    if metric_snapshot is not None:
        with suppress(Exception):
            seed_start = (await metric_snapshot.max_request_id()) + 1
    seed_request_ids(seed_start)
    # The collector enqueues ring events to the disk writer on the O(1) hot path
    # (None in degraded mode → rollup-only ingest, no ring persistence).
    collector = Collector(store=metric_store, ring_writer=metric_snapshot)
    set_collector(collector)  # flips the Plan-04 seam + the request middleware live
    app.state.metric_store = metric_store
    app.state.metric_snapshot = metric_snapshot  # read by deps.get_metric_ring_store
    app.state.collector = collector

    async def _snapshot_loop() -> None:
        # Periodic ROLLUP snapshot (one transaction, ~5ms — never per-event) PLUS a
        # ring flush + dual-bound prune on the same (slower) cadence. Worst-case
        # rollup loss on a hard crash is one interval (≤45s default), accepted.
        while True:
            await asyncio.sleep(settings.metrics_snapshot_interval_s)
            if metric_snapshot is None:  # in-memory-only degraded mode
                continue
            with suppress(Exception):
                await metric_snapshot.snapshot(metric_store)
            with suppress(Exception):
                await metric_snapshot.flush()
            with suppress(Exception):
                await metric_snapshot.prune(
                    settings.metrics_ring_max_rows,
                    settings.metrics_ring_max_age_days,
                )

    async def _ring_flush_loop() -> None:
        # Faster batched-ring flusher (260604-wm2): wake on the flush signal (a
        # burst hit flush_max_batch) OR the flush interval, whichever first, and
        # drain the ring queue. Prune + rollups snapshot stay on the slower
        # _snapshot_loop cadence (prune is cheap-but-not-per-5s).
        assert metric_snapshot is not None  # only created when the store exists
        while True:
            with suppress(TimeoutError):
                async with asyncio.timeout(settings.metrics_ring_flush_interval_s):
                    await metric_snapshot.flush_signal.wait()
            with suppress(Exception):
                await metric_snapshot.flush()

    # No snapshot/flush loops when the store is unavailable (degraded mode).
    snapshot_task: asyncio.Task[None] | None = (
        asyncio.create_task(_snapshot_loop())  # strong ref (Pitfall 4)
        if metric_snapshot is not None
        else None
    )
    ring_flush_task: asyncio.Task[None] | None = (
        asyncio.create_task(_ring_flush_loop())  # strong ref (Pitfall 4)
        if metric_snapshot is not None
        else None
    )

    # D-37 recovery supervisor: once a cloudflare-gated source is DOWN (breaker
    # tripped OR D-33 force_disabled), re-probe on the escalating +1h/+6h schedule
    # (off the request path). Strong-ref'd (manager.py:247-251 idiom) so each
    # fire-and-forget task is never GC'd. #153: a ``force_disabled`` (eager-launch-
    # flake) source is NOW re-probed too — a successful re-solve clears the latch
    # (record_success resets force_disabled), so a cold-start flake self-heals
    # instead of pinning /caps disabled until a manual restart. One supervisor
    # task per cloudflare-gated source so they recover independently.
    async def _probe_source(source_key: str) -> bool:
        try:
            if source_key in on_demand_keys:
                # On-demand source (debug pooltimeout-recurrence): never force-solve a
                # recovery probe — that would loop the solver on an absent challenge.
                # Peek held clearance only; its breaker clears on the next successful
                # data call (record_success) via the on-demand challenge path instead.
                clearance = await solver.get_clearance(
                    source_key, solve_if_missing=False
                )
            else:
                clearance = await solver.get_clearance(source_key, force_resolve=True)
        except Exception:  # noqa: BLE001 — a failed re-probe just keeps it down
            return False
        return clearance is not None

    async def _supervise_source(source_key: str) -> None:
        health = source_health[source_key]
        while True:
            await asyncio.sleep(_RECOVERY_POLL_SECONDS)
            # Healthy — nothing to do. #153: a ``force_disabled`` source (D-33
            # eager-launch flake) is NO LONGER left down until restart — it is
            # re-probed on the watchdog schedule so a successful re-solve clears
            # the stale latch off the request path (record_success() now resets
            # force_disabled). This guarantees recovery even if Mangarr stops
            # querying the source because /caps reported it disabled.
            if health.is_enabled:
                continue
            await _recovery_watchdog(
                health,
                backoff_hours=settings.cloudflare_watchdog_backoff_hours,
                probe=lambda: _probe_source(source_key),
            )

    watchdog_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_supervise_source(key)) for key in cloudflare_keys
    ]
    try:
        yield
    finally:
        # Cancel the warm + watchdog + snapshot bg tasks and close the solver BEFORE
        # the transport, so no orphan Chromium survives shutdown (Pitfall 4).
        bg_tasks = [
            t
            for t in (warm_task, *watchdog_tasks, snapshot_task, ring_flush_task)
            if t is not None
        ]
        for bg in bg_tasks:
            bg.cancel()
        with suppress(Exception):
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        # Tear the bounded lifecycle + patchright down (Pitfall 4 — orphan Chromium).
        # Guarded so a solver-close failure (timeout/CDP error) does not skip the
        # store + transport teardown that follows.
        with suppress(Exception):
            await solver.aclose()
        # Await in-flight jobs BEFORE releasing the store/transport they depend on
        # (CR-01). Bound the drain so a wedged job cannot hang shutdown: on timeout,
        # cancel the stragglers, then proceed to close the store and transport.
        try:
            async with asyncio.timeout(_SHUTDOWN_DRAIN_TIMEOUT_SECONDS):
                await job_manager.drain()
        except TimeoutError:
            _log.warning(
                "Job drain exceeded %ss on shutdown; cancelling stragglers",
                _SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
            )
            await job_manager.cancel_all()
        await store.close()  # release the job-store connection
        # Drain pending handle persists + release the handle-store connection (D-16
        # reversal). Guarded so a teardown error here cannot skip the transport close.
        with suppress(Exception):
            await app.state.handle_store.close()
        await transport.aclose()  # release the search/recent client
        await download_transport.aclose()  # release the download client (split pool)
        # Metrics teardown LAST (independent of solver/job/transport): a FINAL
        # snapshot so nothing in-memory since the last timer tick is lost
        # (Pitfall 4), then close the snapshot connection. set_collector(None) so a
        # straggler emit during teardown is a no-op and tests don't leak a collector.
        set_collector(None)
        if metric_snapshot is not None:
            # Final ring FLUSH first so no enqueued ring event since the last timer
            # tick is lost (≤ flush_interval honesty, 260604-wm2), THEN the final
            # rollup snapshot, then close the connection.
            with suppress(Exception):
                await metric_snapshot.flush()
            with suppress(Exception):
                await metric_snapshot.snapshot(metric_store)
            with suppress(Exception):
                await metric_snapshot.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        settings: Explicit settings (tests inject a fixed key per D-03). When
            ``None``, Task 3 wires this to ``load_settings()``.
    """
    if settings is None:
        from .config import load_settings  # noqa: PLC0415

        settings = load_settings()

    # Structured JSON-lines logging FIRST (OBS-09, §Q3) so even startup/lifespan logs
    # are structured + redacted and carry request_id once a request scope exists.
    configure_logging(settings)

    app = FastAPI(
        title="Mangarr Manga-Gateway API",
        lifespan=lifespan,
        dependencies=[Depends(require_api_key)],  # GLOBAL auth (AUTH-01/D-02)
        root_path=settings.url_base or "",  # PLAT-01 UrlBase
    )
    app.state.settings = settings

    # Middleware ordering (§Q5): add the pure-ASGI metrics middleware FIRST so it ends
    # up INNER (closest to routing — the request_id contextvar is set just before the
    # route + fan-out children run), then CORS LAST so it ends up OUTERMOST (stamps
    # CORS headers even on error responses). CORS is default-deny: added ONLY when an
    # origin allowlist is configured (empty list → no middleware → no CORS header,
    # identical to today's behavior). allow_credentials=False (auth is the X-Api-Key
    # header, not cookies); read-only methods; the custom header triggers preflight.
    app.add_middleware(MetricsRequestMiddleware)
    if settings.metrics_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.metrics_cors_origins,  # exact origins, NEVER ["*"]
            allow_credentials=False,
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["X-Api-Key", "Content-Type"],
            max_age=600,
        )

    app.include_router(api_router)
    # Admin metrics router as a SIBLING of /api/v1 (NOT nested) — its own versioned
    # prefix, still under the global API-key dep + UrlBase root_path (OBS-05/06/07).
    app.include_router(metrics_router, prefix="/admin/metrics/v1")
    register_error_handlers(app)
    return app
