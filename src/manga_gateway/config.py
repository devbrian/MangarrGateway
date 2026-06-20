"""Application settings + API-key provisioning.

TOML is the source of truth (D-10); env vars override ops knobs (D-11).
Precedence: env > TOML > default. The API key is auto-generated-and-persisted
on first start and is NEVER supplied via the environment (D-01).

Env-exclusion mechanism for ``api_key`` (Open Question 2 / Pitfall 4):
``load_settings`` constructs ``Settings(api_key=<from TOML>, ...)`` passing the
key as an explicit init keyword. In pydantic-settings, init keywords take
precedence over the environment source, so ``GATEWAY_API_KEY`` can never set the
effective key. The ``alias`` on ``api_key`` further decouples it from the
``GATEWAY_`` env prefix. A regression test asserts the env value is ignored.

TOML→Settings merge for ops knobs (issue #3): any non-``api_key`` field declared
on :class:`Settings` may be set from the TOML file. Per-field precedence is
preserved by passing the TOML value as an init kwarg ONLY when the corresponding
``GATEWAY_<NAME>`` env var is unset — env vars still win when present, matching
what ``config.example.toml`` advertises.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger("manga_gateway")

_KEY_BYTES = 32  # secrets.token_urlsafe(32) -> >= 43 url-safe chars
# Default downloadHandle TTL — was a hard-coded 3600s (60 min) which was the
# CONFIRMED cause of the "release no longer resolvable" production failures
# (debug session release-no-longer-resolvable): Mangarr's automated
# library-sync replays grabs for releases whose handle minted >60 min earlier
# had already aged out of the in-memory TTLCache (~60% bad_handle misses in
# active windows). Raised to 6h (21600s) so the window comfortably covers
# Mangarr's grab-replay latency; still ENFORCED so handles age out (SEC posture
# / D-15 — never keep-forever). Operator-overridable via GATEWAY_HANDLE_TTL_SECONDS.
_DEFAULT_HANDLE_TTL_SECONDS = 21600


class Settings(BaseSettings):
    """Gateway runtime settings.

    ``host``/``port``/``output_root`` are env-overridable ops knobs (D-11).
    ``api_key`` is provisioned from the TOML file / explicit construction only
    (D-01): its alias keeps it off the ``GATEWAY_`` env mapping, and
    ``load_settings`` always passes it explicitly.
    """

    model_config = SettingsConfigDict(env_prefix="GATEWAY_", extra="ignore")

    host: str = "127.0.0.1"  # AUTH-02: localhost bind by default
    port: int = 9191
    url_base: str = ""  # PLAT-01: UrlBase reverse-proxy prefix; "" = none
    output_root: str = "/data/manga"  # D-11: gateway-determined default
    # Download-surface concurrency + persistence knobs — env-overridable ops knobs
    # (D-11), same treatment as host/port/output_root (NOT the api_key exclusion).
    # ge=1: a non-positive bound would break semaphore creation / job scheduling.
    max_concurrent_chapters: int = Field(
        default=8, ge=1
    )  # D-30: global job bound, reported by /status
    # D-30 / WR-02: DEFAULT per-source ceiling, intentionally <= the global bound.
    # Without a distinct knob the per-source semaphore was sized to the global
    # bound and so never constrained anything for the single-registered-source
    # case. Defaulting to 1 makes the second source's first job queue behind the
    # first source's job in the obvious way; operators raise it per deployment.
    # A source MAY override this via its ``Source.max_concurrent_jobs`` class attr
    # (e.g. mangadot=3); the job manager clamps any override to max_concurrent_chapters.
    max_concurrent_per_source: int = Field(default=1, ge=1)
    image_fetch_concurrency: int = Field(
        default=6, ge=1
    )  # D-31: per-job image-fetch bound
    db_path: str = "gateway.db"  # RESEARCH Open Q2: aiosqlite job store path
    # IN-05: cap the persisted terminal-job history so a long-running gateway
    # does not grow the projection / GET /downloads payload without bound.
    # Applied at rehydrate: keep at most this many TERMINAL (completed/failed)
    # rows ordered by updated_at DESC; older rows are dropped. Live jobs are
    # never trimmed. ge=1 keeps the bound valid against zero/negative overrides.
    max_history_jobs: int = Field(default=500, ge=1)
    # ── Cloudflare-solver / anti-bot knobs — env-overridable ops knobs (D-11),
    # same treatment as host/port/output_root (NOT the api_key exclusion). These
    # govern the framework's shared CloudflareSolver (lifespan-owned R1) and
    # apply to ALL cloudflare-gated sources registered in the source registry.
    # ``ge=1`` bounds keep the breaker/semaphore valid against zero/negative
    # overrides (T-04-02).
    cloudflare_breaker_threshold: int = Field(
        default=5, ge=1
    )  # D-36: N consecutive failures flips is_enabled False
    # #153: eager-warm cold-start flake absorber. On a fresh container deploy the
    # first Cloudflare warm for a cloudflare-gated source occasionally fails fast
    # (cold persistent-context / browser-launch race) even though an on-demand
    # warm succeeds seconds later. ``warm()`` retries each source this many times
    # (linear backoff ``cloudflare_warm_retry_seconds * attempt``) BEFORE reporting
    # it failed and letting the lifespan force_disable it (D-33). 1 ⇒ historic
    # single-attempt behavior.
    cloudflare_warm_attempts: int = Field(default=3, ge=1)
    cloudflare_warm_retry_seconds: float = Field(default=2.0, ge=0.0)
    cloudflare_solve_concurrency: int = Field(
        default=1, ge=1
    )  # D-33/Pattern 7: solve cap; default 1 collapses to single-flight
    # Bound on concurrent ``fetch_via_browser`` calls within a single
    # CloudflareSolver instance. Backed by an ``asyncio.Semaphore`` in
    # ``CloudflareSolver._browser_lock``. Default 3 → the common comix
    # series-candidate fan-out runs 3-at-a-time (max-not-sum wall-clock); set
    # to 1 to collapse back to the historic ``asyncio.Lock`` shape (one nav at
    # a time, fully sequential).
    #
    # ENGINE CONSTRAINT (debug session ``comix-parallel-engine-probe``,
    # 2026-06-01 — supersedes the earlier ``comix-parallel-page-contention``
    # "CF per-IP burst" caveat, now REFUTED): ``> 1`` is ONLY safe on
    # ``cloudflare_engine="patchright"`` (Chromium), which is now the DEFAULT —
    # so the default 3 is a safe pairing. Chromium runs N concurrent CF
    # navigations on one shared warm context cleanly (4/4 proven on
    # residential-IP Windows + Linux, and through a residential proxy). Camoufox
    # (Firefox) STALLS concurrent CF navigations at goto-commit — the
    # chapter-list DOM never renders — so ``engine="camoufox"`` REQUIRES this to
    # be 1. That pairing is enforced by the ``_reject_camoufox_parallel`` model
    # validator below (closes #64): camoufox + ``> 1`` fails fast at startup
    # instead of silently returning zero results. The failure is engine-specific,
    # NOT a Cloudflare per-IP burst limit (issue #59). DEPLOY NOTE:
    # ``engine=patchright`` may need a residential-reputation egress to clear CF
    # on some hosts; a datacenter-IP host may need to route the Chromium egress
    # through a residential proxy (CLAUDE.md proxy-ready transport, issue #65).
    cloudflare_fetch_concurrency: int = Field(default=3, ge=1)
    # Run the stealth browser headless. The app default is True (bare-app/dev with
    # a real display), but the SHIPPED DOCKER IMAGE bakes
    # ``GATEWAY_CLOUDFLARE_HEADLESS=false`` (headed under xvfb) — see the Dockerfile.
    # Headless historically cleared CF on a residential-reputation IP, but as of
    # 2026-06-07 comix.to's Cloudflare fingerprints/blocks the headless Chromium UA
    # ("HeadlessChrome") even from a residential IP: a headless solve no longer earns
    # cf_clearance, so the gateway falls back to a stale on-disk cookie and comix
    # /recent + /search 403 (debug comix-recent-403). HEADED Chromium still clears it
    # on every host (residential + datacenter, #35). When headed on a display-less
    # Linux host the solver auto-starts an Xvfb virtual display (pyvirtualdisplay) —
    # the host only needs the ``xvfb`` package installed (it is in the image).
    cloudflare_headless: bool = True
    # D-34: persistent-context dir holding cf_clearance; resolved via pathlib at
    # the use site (Plan 04), NOT under output_root (T-04-03 — never logged).
    cloudflare_user_data_dir: str = "cloudflare-userdata"
    # D-37: watchdog re-clearance backoff schedule (min_hours, max_hours).
    cloudflare_watchdog_backoff_hours: tuple[int, int] = (1, 6)
    # Anti-bot engine selector (#35): which stealth browser drives the
    # CloudflareSolver. ``patchright`` (Chromium-based, patched CDP leaks) is
    # the DEFAULT since the ``comix-parallel-engine-probe`` finding
    # (2026-06-01): only Chromium can run concurrent CF navigations on one warm
    # context, so making it the default is what lets
    # ``cloudflare_fetch_concurrency`` > 1 work out of the box. ``camoufox``
    # (Firefox-based, C++ fingerprint spoof) is the opt-in fallback
    # (``GATEWAY_CLOUDFLARE_ENGINE=camoufox``) — it clears CF on some hosts where
    # Chromium's fingerprint has been flagged (historically the GitHub Actions
    # datacenter runner, issue #35) but CANNOT run parallel, so a camoufox deploy
    # must also set ``cloudflare_fetch_concurrency=1`` (the
    # ``_reject_camoufox_parallel`` validator below enforces this). NOTE: this
    # REVERSES the #40 "camoufox everywhere" default; whether Chromium also
    # clears CF on datacenter runners is being re-tested (the #35 conclusion
    # predates this work — camoufox passing there proved it is a fingerprint
    # signal, not pure IP reputation). Both back the SAME ``AntiBotSolver``
    # interface — swap is a config flip, not a rewrite. Patchright requires
    # ``uv run patchright install chromium``; Camoufox requires
    # ``uv run camoufox fetch`` (~200 MB Firefox build, one-time).
    cloudflare_engine: Literal["patchright", "camoufox"] = "patchright"
    # Diagnostic-only: when True, ``CloudflareSolver._fetch_via_browser_once``
    # attaches ``page.on("pageerror" | "console" | "requestfailed")`` listeners
    # so we can capture the JS throw site behind the Playwright Firefox driver
    # crash investigated in debug session ``comix-pageerror-throw-site-54``
    # (issue #54). OFF by default — the handlers log INFO-level per-event
    # lines, which are too noisy for production and add a small per-page
    # overhead. Enable for nightly evidence-capture runs with
    # ``GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1`` (pydantic-settings auto-binds
    # the ``GATEWAY_`` prefix to this field). When evidence has been captured,
    # this flag should be turned back off (or the instrumentation removed
    # entirely) — it is NOT intended as a permanent runtime knob.
    cloudflare_log_browser_events: bool = False
    # ── Single static residential proxy (PROXY-01, issue #65) — env-overridable
    # ops knobs (D-11), same treatment as host/port (NOT the api_key exclusion).
    # They ride the existing load_settings TOML->kwargs merge automatically.
    # "Proxy configured" is defined as ``cloudflare_proxy_server is not None``;
    # when None, NO ``proxy=`` is threaded anywhere (today's behavior unchanged).
    # cf_clearance is IP-bound, so the SAME proxy egresses BOTH the stealth
    # browser (CF clearance) AND the shared httpx image-fetch client — both
    # derived from the one ``framework.proxy.build_proxy`` helper (the future
    # pool/rotation seam). A datacenter-IP host routes Chromium egress through a
    # residential proxy here to clear CF (CLAUDE.md proxy-ready transport).
    cloudflare_proxy_server: str | None = None  # e.g. "http://host:port"
    cloudflare_proxy_username: str | None = None
    # SecretStr so repr/str/logs auto-redact (T-odg-01). The password is env-only
    # by OPERATOR DISCIPLINE, not by mechanism — set it via
    # GATEWAY_CLOUDFLARE_PROXY_PASSWORD; never hand-edit a real value into the
    # TOML and never commit a real credential. get_secret_value() is unpacked
    # ONLY inside build_proxy.
    cloudflare_proxy_password: SecretStr | None = None
    # ── Image-fetch residential proxy pool (260620-4im, PROXY-01 extension) ─────
    # Env-overridable ops knobs (D-11), same treatment as host/port (NOT the api_key
    # exclusion); they ride the existing load_settings TOML→kwargs merge. ROUTE ONLY
    # source image-byte fetches (the ``fetch_image`` phase) through a reusable framework
    # proxy pool; search + the read-page CF solve stay DIRECT (unchanged). A source opts
    # in with ``Source.image_fetch_via_proxy_pool = True`` (MangaFire first — its
    # ``mfcdnN`` image-CDN zones IP-ban the gateway's direct egress; a clean residential
    # proxy returns 200). "Pool enabled" is DERIVED (file set + ``ProxyPool.from_file``
    # returns non-None) — no separate enabled bool. When unconfigured the download path
    # egresses byte-for-byte exactly as today (the hard regression contract).
    #
    # ``image_proxy_pool_file`` points at an operator-supplied ``host:port:user:pass``
    # file (gitignored; NEVER committed). It rides the ``_empty_proxy_field_is_none``
    # validator below so CI's unset-secret ``""`` normalizes to None (pool disabled).
    image_proxy_pool_file: str | None = None
    # Cooldown (seconds) a proxy is sidelined after a failed image fetch before
    # ``acquire`` offers it again. ge=0.0: 0 disables the cooldown (every proxy always
    # immediately re-eligible).
    image_proxy_cooldown_seconds: float = Field(default=300.0, ge=0.0)
    # Per-page proxy attempts: 1 initial + 2 retries-with-a-different-proxy (the locked
    # spec). ge=1: at least one attempt must be made.
    image_proxy_max_attempts: int = Field(default=3, ge=1)
    # ── Android-WebView Cloudflare solver sidecar (Phase 10, BOT-02) ───────────
    # Env-overridable ops knobs (D-11), same treatment as the cloudflare_* knobs
    # (NOT the api_key exclusion); they ride the load_settings TOML→kwargs merge.
    # The gateway-side ``AndroidSolver`` httpx-POSTs this sidecar's ``/solve`` to
    # mint a ``cf_clearance`` from a real Android WebView (redroid) for the strict
    # Cloudflare Turnstile on mangadot/kagane that desktop automation cannot clear
    # from Linux (resolved debug ``mangadot-cf-linux-fingerprint``). R1 is
    # preserved: the gateway holds no Android machinery — it reaches the sidecar
    # over the docker-internal network only. ``None`` URL ⇒ UNCONFIGURED: the
    # AndroidSolver's warm() boots every android source disabled, so the gate / CI
    # / a local box without redroid stays green (the sidecar is a DEPLOY-only
    # dependency). The api key is a SecretStr (repr/log-redacted) sent as the
    # ``X-Solver-Key`` header (SEC-01); set it via GATEWAY_ANDROID_SOLVER_API_KEY.
    android_solver_url: str | None = None  # e.g. "http://android-solver:8080"
    android_solver_api_key: SecretStr | None = None
    # gt=0: a non-positive timeout would break the httpx request budget. This is the
    # gateway->sidecar CLIENT timeout; it MUST exceed the sidecar's own per-solve cap
    # (SOLVER_SOLVE_TIMEOUT_S, default 120s) + margin — otherwise the gateway abandons
    # a solve the sidecar would complete and re-fires, thrashing the single redroid
    # (Phase 10 live-verify). Default 180 = the 120s cap + 60s margin, so a clean
    # out-of-the-box run does not trip the coupling. Raise it if you raise the cap.
    android_solver_timeout_s: float = Field(default=180.0, gt=0)
    # ── Source enable/disable (reversible ops knob; #198/#202) ─────────────────
    # Comma-separated source keys to SKIP registering at startup, e.g.
    # GATEWAY_DISABLED_SOURCES="kagane,mangadot". A disabled source is dropped
    # from the registry entirely → absent from /caps, never searched, and never
    # added to the CF solver warm set (so a source that cannot clear Cloudflare
    # in this environment can't create a startup warm storm). Default "" = every
    # built-in source registered (behavior unchanged). REVERSIBLE: clear the env
    # to re-enable. Kept a plain str (NOT a set) so the comma-separated env value
    # needs no JSON encoding (unlike metrics_cors_origins); call
    # ``disabled_source_keys()`` for the normalized frozenset.
    disabled_sources: str = ""
    # alias decouples the key from the GATEWAY_ env prefix (D-01).
    api_key: str = Field(alias="api_key")
    # ── Observability & metrics (Phase 8) ─────────────────────────────────────
    # Env-overridable ops knobs (D-11), same treatment as host/port/output_root
    # (NOT the api_key exclusion). They ride the existing load_settings
    # TOML->kwargs merge automatically — env > TOML > default. Bounds keep a
    # zero/negative override from reaching a deque/timer (T-08-02): every ring /
    # interval / backup-count is Field(ge=1); metrics_slow_factor is Field(gt=0).
    log_level: str = "INFO"  # §Q3: root log level for the dictConfig
    log_dir: str = "/state/logs"  # §Q3: RotatingFileHandler dir (mkdir at setup)
    log_max_bytes: int = Field(
        default=10_000_000, ge=1
    )  # §Q3: RotatingFileHandler maxBytes (≥1 byte)
    log_backup_count: int = Field(
        default=5, ge=1
    )  # §Q3: RotatingFileHandler backupCount (≥1 rotation kept)
    # §Q2: default-deny — empty list means the CORS middleware is not even added
    # (T-08-03). pydantic-settings parses a JSON-list env value, e.g.
    # GATEWAY_METRICS_CORS_ORIGINS='["http://localhost:5173"]'.
    metrics_cors_origins: list[str] = Field(default_factory=list)
    # NOTE (260604-wm2): the three ring-size knobs below are SUPERSEDED by the
    # disk-backed ring model — ring events are now the on-disk ring_events table
    # (system of record), NOT in-memory deques, bounded by the retention knobs
    # (metrics_ring_max_rows / metrics_ring_max_age_days) rather than a per-ring
    # maxlen. They are retained (NOT removed) so an operator's existing TOML/env
    # that sets them does not fail validation; the disk store ignores them.
    metrics_recent_ring: int = Field(
        default=500, ge=1
    )  # SUPERSEDED: was the bounded `recent` deque size
    metrics_failures_ring: int = Field(
        default=200, ge=1
    )  # SUPERSEDED: was the bounded `failures` deque size
    metrics_slow_ring: int = Field(
        default=200, ge=1
    )  # SUPERSEDED: was the bounded `slow` deque size
    # ── Disk-backed ring retention + batched-write knobs (260604-wm2) ──────────
    # Env-overridable ops knobs (D-11), same TOML→kwargs merge as the other
    # metrics_* fields. The ring_events table is pruned to BOTH bounds (whichever
    # hits first) on the background timer; writes are batched (O(1) enqueue, a
    # ~5s/1000-event flush). Bounds are Field(ge=1) so a zero/negative override is
    # rejected at construction.
    metrics_ring_max_rows: int = Field(
        default=200_000, ge=1
    )  # retention: max ring_events rows kept (oldest pruned beyond this)
    metrics_ring_max_age_days: int = Field(
        default=30, ge=1
    )  # retention: max ring_events age in days (older pruned)
    metrics_ring_flush_interval_s: int = Field(
        default=5, ge=1
    )  # batched flush cadence (≤ this many seconds of events lost on hard crash)
    metrics_ring_flush_max_batch: int = Field(
        default=1000, ge=1
    )  # batched flush trigger: flush early once the queue reaches this size
    metrics_snapshot_interval_s: int = Field(
        default=45, ge=1
    )  # §Q4: snapshot-loop cadence (≤45s loss-on-crash bound)
    # §Q4: "slow" admission factor — duration_ms > max(slow_factor*avg, p95) per
    # rollup. A float multiplier, bounded gt=0 (a non-positive factor would admit
    # everything / nothing); NEVER a global ms constant.
    metrics_slow_factor: float = Field(default=3.0, gt=0)
    # Runtime State Inventory: SEPARATE DB file from gateway.db for the snapshot
    # store (08-02), so the metrics persistence never contends with the job store.
    metrics_db_path: str = "/state/metrics.db"
    # ── Search-result / chapter-link caching (Phase 9) ─────────────────────────
    # Env-overridable ops knobs (D-11), same treatment as host/port/output_root
    # (NOT the api_key exclusion). They ride the existing load_settings
    # TOML->kwargs merge automatically — env > TOML > default. These govern the
    # source-agnostic EnumerationCache (framework/enum_cache.py, 09-01) that the
    # lifespan owns (R1 single-process) and every source opts into at its
    # enumeration boundary.
    #
    # D-08 kill-switch: default ON. Set GATEWAY_ENUM_CACHE_ENABLED=0 to disable
    # caching entirely (short-circuit straight to the upstream fetch) — the
    # operator escape hatch to fall back to pre-Phase-9 behavior while debugging
    # stale/wrong results.
    enum_cache_enabled: bool = True
    # D-07 ops knobs. Field(ge=1): a zero/negative bound would break the cache's
    # TTLCache construction (T-09-05). Blueprint defaults: maxsize 512, ttl 30min.
    enum_cache_maxsize: int = Field(default=512, ge=1)
    # D-09/CACHE-05: the TTL MUST stay ≤ the 60-min (3600s) handle TTL so a cached
    # enumeration can never out-live the downloadHandle it would serve. The
    # `_enum_cache_ttl_within_handle_ttl` model validator below fails fast above
    # 3600 (mirrors _reject_camoufox_parallel).
    enum_cache_ttl_seconds: int = Field(default=1800, ge=1)
    # 260606-lyb Change 2 / 260620 backoff rework: per-source failure cooldown
    # (negative cache for FAILURES, distinct from the enum cache above which caches
    # successes). When a source's fan-out branch hits a hard failure (timeout /
    # SourceError / 5xx / transport / unexpected) the framework grows a CONSECUTIVE-
    # failure backoff and suppresses that source SOURCE-WIDE while a cooldown is live,
    # so a repeat search returns instantly with zero upstream calls. The 1st failure
    # opens NO cooldown (one blip never sidelines a source); the 2nd+ escalates
    # min(base * 2**(streak-2), max): 30, 60, 120, 240, 480, 600 by default.
    #
    # Two env-overridable ops knobs (D-11, NOT the api_key exclusion):
    #   GATEWAY_SOURCE_FAILURE_COOLDOWN_SECONDS      — the MAX cap, default 600s (10min)
    #   GATEWAY_SOURCE_FAILURE_COOLDOWN_BASE_SECONDS — the first step, default 30s
    # ge=0 (NOT ge=1) on both precisely because 0 is the documented DISABLE value:
    # setting EITHER to 0 turns the cooldown off entirely (every in_cooldown → False).
    source_failure_cooldown_seconds: int = Field(default=600, ge=0)
    source_failure_cooldown_base_seconds: int = Field(default=30, ge=0)
    # HDL-02/GAP-2: in-memory downloadHandle store capacity. An env-overridable
    # ops knob (D-11, same host/port treatment — NOT the api_key exclusion):
    # GATEWAY_HANDLE_MAXSIZE, default 200_000. The cap MUST stay >= one full
    # library-sync burst so handles survive their 60-min HDL-02 TTL instead of
    # being evicted early (production saw ~23k handles minted in a 2-hour window,
    # 2.3x the prior 10_000 cap → 739 `bad_handle` rejections). Field(ge=1): a
    # zero/negative override would break the store's TTLCache construction
    # (T-txd-02), so reject it at Settings construction (fail-fast at startup).
    # Rides the existing load_settings model_fields TOML->kwargs merge — no extra
    # wiring needed.
    handle_maxsize: int = Field(default=200_000, ge=1)
    # HDL-02: the downloadHandle TTL in seconds. An env-overridable ops knob
    # (D-11, same host/port treatment — NOT the api_key exclusion):
    # GATEWAY_HANDLE_TTL_SECONDS, default 21600 (6h). Replaces the prior hard-coded
    # 3600s (60 min) that was the CONFIRMED cause of the "release no longer
    # resolvable" production failures — Mangarr's library-sync replays grabs long
    # after the 60-min window, so the handle was already gone (debug session
    # release-no-longer-resolvable). The TTL is STILL enforced (handles age out —
    # SEC posture / D-15, never keep-forever); it just spans Mangarr's grab-replay
    # latency now. ge=1800: MUST stay >= the 30-min Mangarr interactive-search
    # floor AND >= the enum-cache TTL ceiling default (1800), so a cached
    # enumeration can never out-live the handle it serves (D-09/CACHE-05, enforced
    # by `_enum_cache_ttl_within_handle_ttl` which now anchors to THIS value).
    handle_ttl_seconds: int = Field(default=_DEFAULT_HANDLE_TTL_SECONDS, ge=1800)
    # HDL-02 / D-16 REVERSAL: path to the aiosqlite handle store so minted handles
    # SURVIVE a restart (the in-memory TTLCache is wiped on restart, which combined
    # with the long TTL above would silently re-introduce bad_handle misses after a
    # redeploy). Mirrors db_path/metrics_db_path: an env-overridable ops knob
    # (GATEWAY_HANDLE_DB_PATH); the Dockerfile points it at the /state volume so it
    # persists across container recreation. Persisting also lets the on-disk store
    # hold far more than a 6h in-memory burst without blowing the 200k cap.
    handle_db_path: str = "handles.db"

    @field_validator("metrics_cors_origins")
    @classmethod
    def _reject_wildcard_cors(cls, origins: list[str]) -> list[str]:
        """Enforce the default-deny / explicit-allowlist / never-``*`` CORS rule.

        §Q2 is default-deny (empty list = no CORS middleware). When an operator DOES
        configure origins they must be an explicit allowlist — a wildcard ``"*"`` (or
        a blank/whitespace-only entry that some configs use as a stand-in) would
        re-open the surface to any origin, defeating the allowlist. Reject those at
        construction so the misconfiguration fails fast at startup instead of silently
        widening CORS (CodeRabbit). The empty-list default still passes untouched.
        """
        for origin in origins:
            if origin.strip() == "*":
                raise ValueError(
                    'metrics_cors_origins must be an explicit allowlist, never "*" '
                    "(default-deny / never-wildcard CORS). Remove the wildcard and "
                    "list each allowed origin, e.g. "
                    '["http://localhost:5173"].'
                )
            if not origin.strip():
                raise ValueError(
                    "metrics_cors_origins entries must be non-empty origins; "
                    "remove blank/whitespace-only entries."
                )
        return origins

    @field_validator(
        "cloudflare_proxy_server",
        "cloudflare_proxy_username",
        "cloudflare_proxy_password",
        "image_proxy_pool_file",
        mode="before",
    )
    @classmethod
    def _empty_proxy_field_is_none(cls, value: object) -> object:
        """Treat an empty/whitespace proxy env value as UNSET (None).

        GitHub Actions (and most CI) substitute an UNSET secret as the empty
        string ``""``, not as a missing var — so ``GATEWAY_CLOUDFLARE_PROXY_SERVER:
        ${{ secrets.X }}`` with no secret configured reaches us as ``""``, not
        absent. Without this, ``""`` is a truthy-vs-None mismatch: ``build_proxy``
        keys off ``server is None``, so an empty string would build a BROKEN proxy
        (``httpx.Proxy(url="")`` / ``{"server": ""}``) instead of the documented
        "unconfigured ⇒ no proxy, bare egress" #65 contract — and an empty
        username/password would forge bogus auth. Normalizing ``""`` → ``None``
        here keeps the invariant "configured == cloudflare_proxy_server is not
        None" true regardless of how the empty value arrived (CodeRabbit, #138).
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def disabled_source_keys(self) -> frozenset[str]:
        """Normalized set of source keys to skip registering (#198/#202).

        Parses the comma-separated ``GATEWAY_DISABLED_SOURCES`` (``disabled_sources``)
        value into a lowercased, whitespace-stripped frozenset; blank entries are
        dropped. Empty/unset → empty set, i.e. every built-in source registers.
        """
        return frozenset(
            key.strip().lower()
            for key in self.disabled_sources.split(",")
            if key.strip()
        )

    @model_validator(mode="after")
    def _reject_camoufox_parallel(self) -> Settings:
        """Fail fast on the camoufox + parallel-fetch misconfiguration (#64).

        ``cloudflare_fetch_concurrency > 1`` is only safe under
        ``cloudflare_engine="patchright"`` (Chromium). Camoufox/Firefox stalls
        concurrent Cloudflare navigations at goto-commit, so the combination
        silently returns zero results (debug session
        ``comix-parallel-engine-probe``, issue #59). Since patchright + 3 is the
        default, this only trips when an operator overrides the engine back to
        camoufox (e.g. a datacenter host / CI runner) without also pinning
        concurrency to 1 — surfacing the mistake loudly at startup instead.
        """
        if (
            self.cloudflare_engine == "camoufox"
            and self.cloudflare_fetch_concurrency > 1
        ):
            raise ValueError(
                "cloudflare_fetch_concurrency="
                f"{self.cloudflare_fetch_concurrency} requires "
                'cloudflare_engine="patchright" (Chromium). Camoufox/Firefox '
                "stalls concurrent Cloudflare navigations and returns zero "
                "results. Fix via env: set GATEWAY_CLOUDFLARE_FETCH_CONCURRENCY=1, "
                "or keep parallelism with GATEWAY_CLOUDFLARE_ENGINE=patchright "
                "(issue #59)."
            )
        return self

    @model_validator(mode="after")
    def _enum_cache_ttl_within_handle_ttl(self) -> Settings:
        """Fail fast when the enum-cache TTL exceeds the handle TTL (D-09).

        A cached enumeration mints a fresh ``downloadHandle`` on every serve
        (CACHE-03), but a handle only lives ``handle_ttl_seconds``. If the
        enumeration TTL were allowed above that ceiling the cache could keep
        serving a series whose handles can no longer be resolved downstream
        (CACHE-05). The ceiling is now ANCHORED to the configured
        ``handle_ttl_seconds`` (debug session release-no-longer-resolvable) rather
        than a hard-coded 3600 — raising the handle TTL must not retroactively
        clamp the enum cache, and lowering it must still be respected. Reject the
        misconfiguration at construction — mirroring ``_reject_camoufox_parallel``
        — so it surfaces loudly at startup instead of silently mis-behaving.
        """
        if self.enum_cache_ttl_seconds > self.handle_ttl_seconds:
            raise ValueError(
                "enum_cache_ttl_seconds="
                f"{self.enum_cache_ttl_seconds} exceeds the "
                f"{self.handle_ttl_seconds}s downloadHandle TTL ceiling "
                "(handle_ttl_seconds, D-09/CACHE-05): a cached enumeration must "
                "not out-live the handle it serves. Fix via env: set "
                "GATEWAY_ENUM_CACHE_TTL_SECONDS <= GATEWAY_HANDLE_TTL_SECONDS "
                f"(currently {self.handle_ttl_seconds})."
            )
        return self


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from TOML, generating + persisting the key on first run.

    Args:
        path: TOML config path (source of truth, D-10). When ``None`` (the
            default), resolves in priority order:

            1. ``GATEWAY_CONFIG`` env var (absolute or CWD-relative path)
            2. ``./config.toml`` in the process CWD

            The resolved path is converted to ABSOLUTE so a later ``cwd`` change
            (or an integration test launching the app from a tmp dir) can't
            silently re-resolve to a different file. Running ``python -m
            manga_gateway`` from two different directories with NEITHER an
            explicit ``path`` nor ``GATEWAY_CONFIG`` is what previously generated
            two independent ``config.toml`` files and two independent API keys
            (IN-04); the env-var override is the supported escape hatch.

    Returns:
        Fully-provisioned ``Settings``. The ``api_key`` always comes from the
        TOML data, never from the environment (D-01/D-11).
    """
    if path is None:
        env_path = os.environ.get("GATEWAY_CONFIG")
        path = Path(env_path) if env_path else Path("config.toml")
    # Resolve to absolute so the path doesn't drift under a later cwd change
    # (IN-04). ``resolve(strict=False)`` works even when the file does not yet
    # exist (the first-run key-generation path).
    path = path.resolve(strict=False)
    data: dict[str, object] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))

    api_key = data.get("api_key")
    if not api_key or not isinstance(api_key, str):
        api_key = secrets.token_urlsafe(_KEY_BYTES)  # D-01 generate
        data["api_key"] = api_key
        # stdlib tomllib is read-only — tomli_w writes the key back (D-10).
        # Atomic + owner-only write of the sole plaintext secret (WR-03/WR-04):
        # mkstemp creates the temp file 0o600 in the same dir, os.replace() makes
        # the swap atomic so a crash mid-write can't truncate the only key copy.
        # On Windows the 0o600 bits are best-effort (largely ignored); effective
        # on the Linux prod target.
        rendered = tomli_w.dumps(data)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent or "."), prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(rendered)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        # Log the generation event ONCE (D-01) — never the secret itself; the
        # operator reads the key from the persisted TOML at `path` (CodeRabbit).
        _log.info("Generated a new API key and persisted it to %s", path)

    # Merge TOML-provided ops knobs into init kwargs, but ONLY for fields whose
    # ``GATEWAY_<NAME>`` env var is unset — env > TOML > default (issue #3).
    # Unknown TOML keys are silently ignored (mirrors model_config extra="ignore").
    # ``api_key`` is handled separately above; never bridge it from env.
    #
    # Case-insensitive presence check: pydantic-settings uses ``case_sensitive=False``
    # by default, so an env var set as e.g. ``GATEWAY_host`` is honored just the same
    # as ``GATEWAY_HOST``. Comparing uppercased names ensures we don't pass a TOML
    # init kwarg that would then beat a differently-cased env var (CodeRabbit PR #27).
    env_names_upper = {name.upper() for name in os.environ}
    toml_kwargs: dict[str, Any] = {}
    for field_name in Settings.model_fields:
        if field_name == "api_key" or field_name not in data:
            continue
        if f"GATEWAY_{field_name.upper()}" in env_names_upper:
            continue  # env wins
        toml_kwargs[field_name] = data[field_name]

    # api_key passed explicitly (beats env, D-01); ops knobs come from TOML when
    # not env-overridden (D-11). GATEWAY_API_KEY is ignored.
    return Settings(api_key=api_key, **toml_kwargs)
