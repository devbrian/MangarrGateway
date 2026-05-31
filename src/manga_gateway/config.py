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
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger("manga_gateway")

_KEY_BYTES = 32  # secrets.token_urlsafe(32) -> >= 43 url-safe chars


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
        default=3, ge=1
    )  # D-30: global job bound, reported by /status
    # D-30 / WR-02: per-source ceiling, intentionally <= max_concurrent_chapters.
    # Without a distinct knob the per-source semaphore was sized to the global
    # bound and so never constrained anything for the single-registered-source
    # case. Defaulting to 1 makes the second source's first job queue behind the
    # first source's job in the obvious way; operators raise it per deployment.
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
    cloudflare_solve_concurrency: int = Field(
        default=1, ge=1
    )  # D-33/Pattern 7: solve cap; default 1 collapses to single-flight
    cloudflare_headless: bool = True  # Open Q2: run the stealth browser headless
    # D-34: persistent-context dir holding cf_clearance; resolved via pathlib at
    # the use site (Plan 04), NOT under output_root (T-04-03 — never logged).
    cloudflare_user_data_dir: str = "cloudflare-userdata"
    # D-37: watchdog re-clearance backoff schedule (min_hours, max_hours).
    cloudflare_watchdog_backoff_hours: tuple[int, int] = (1, 6)
    # Anti-bot engine selector (#35): which stealth browser drives the
    # CloudflareSolver. ``patchright`` (Chromium-based) is the dev/Windows
    # default — passes Cloudflare reliably on residential IPs. ``camoufox``
    # (Firefox-based, C++ fingerprint spoof) is the CI/Linux escalation;
    # Patchright's Chromium fingerprint is flagged by Cloudflare's encrypted
    # tier on ubuntu-latest runners, so CI flips this to ``camoufox`` via
    # ``GATEWAY_CLOUDFLARE_ENGINE=camoufox``. Both back the SAME
    # ``AntiBotSolver`` interface — swap is a config flip, not a rewrite
    # (CLAUDE.md "keep the browser behind an interface so this is a config
    # flip"). Camoufox requires ``uv run camoufox fetch`` to download its
    # Firefox binary before first use.
    cloudflare_engine: Literal["patchright", "camoufox"] = "patchright"
    # alias decouples the key from the GATEWAY_ env prefix (D-01).
    api_key: str = Field(alias="api_key")


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
