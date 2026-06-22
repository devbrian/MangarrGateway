"""Config + key-provisioning tests (Task 3).

Covers D-01 (auto-generate-and-persist, NO env key path), D-10 (TOML is the
source of truth), D-11 (env overrides ONLY ops knobs), and Pitfall 4 (the
GATEWAY_API_KEY landmine — a regression test asserts it is ignored).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from manga_gateway.config import Settings, load_settings


def test_generates_and_persists_key_on_empty_dir(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    assert not cfg.exists()

    settings = load_settings(cfg)

    # A url-safe key of >= 32 chars was generated...
    assert len(settings.api_key) >= 32
    # ...and persisted into the TOML file.
    assert cfg.exists()
    persisted = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert persisted["api_key"] == settings.api_key


def test_key_is_idempotent_across_loads(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    first = load_settings(cfg)
    second = load_settings(cfg)
    # Second load reads the SAME persisted key — never regenerates (D-01/D-10).
    assert first.api_key == second.api_key


def test_logs_key_once_at_generation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "config.toml"
    with caplog.at_level("INFO", logger="manga_gateway"):
        settings = load_settings(cfg)
    # Assert a generation EVENT was logged once — never the secret value itself
    # (logging the raw key would be a credential leak; CodeRabbit).
    generation_logs = [
        r for r in caplog.records if "Generated a new API key" in r.getMessage()
    ]
    assert len(generation_logs) == 1
    # The raw key must NOT appear in any log message.
    assert not [r for r in caplog.records if settings.api_key in r.getMessage()]
    # A second load (key already present) must NOT log a generation event again.
    caplog.clear()
    with caplog.at_level("INFO", logger="manga_gateway"):
        load_settings(cfg)
    assert not [
        r for r in caplog.records if "Generated a new API key" in r.getMessage()
    ]


def test_env_api_key_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Pitfall 4 / D-01): GATEWAY_API_KEY must NOT set the key."""
    cfg = tmp_path / "config.toml"
    # Pre-seed the file with a known key.
    file_key = "file-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    cfg.write_text(f'api_key = "{file_key}"\n', encoding="utf-8")

    monkeypatch.setenv("GATEWAY_API_KEY", "env-key-should-be-ignored")
    settings = load_settings(cfg)

    assert settings.api_key == file_key
    assert settings.api_key != "env-key-should-be-ignored"


def test_gateway_config_env_var_selects_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GATEWAY_CONFIG`` overrides the default ``./config.toml`` lookup (IN-04).

    Without this, running ``python -m manga_gateway`` from two different
    directories silently generates two independent config + API key files."""
    cfg = tmp_path / "elsewhere" / "gw.toml"
    cfg.parent.mkdir()
    monkeypatch.setenv("GATEWAY_CONFIG", str(cfg))
    # CWD does NOT have a config.toml; without GATEWAY_CONFIG this would have
    # generated ``<cwd>/config.toml`` instead of the env-pointed path.
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert cfg.exists()
    assert not (tmp_path / "config.toml").exists()
    assert len(settings.api_key) >= 32


def test_relative_path_is_resolved_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_settings`` calls ``path.resolve()`` so a relative ``path`` binds
    to the CURRENT cwd at the moment of the call (IN-04). The per-call resolve
    is the well-defined contract; cross-cwd stability is what ``GATEWAY_CONFIG``
    is for."""
    monkeypatch.chdir(tmp_path)
    # First call: relative path resolves under tmp_path → fresh key.
    first = load_settings(Path("config.toml"))
    persisted = tmp_path / "config.toml"
    assert persisted.exists()

    # chdir elsewhere and reload by the same RELATIVE name — resolution against
    # the new cwd yields a DISTINCT file with a freshly generated key.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    second = load_settings(Path("config.toml"))
    assert (other / "config.toml").exists()
    assert second.api_key != first.api_key

    # Loading the original absolute path still returns the original key — the
    # per-call resolve never drifted away from a fully-qualified path.
    assert load_settings(persisted).api_key == first.api_key


def test_env_overrides_ops_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env DOES override host/port/output_root (D-11)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('api_key = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8")

    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
    monkeypatch.setenv("GATEWAY_PORT", "8080")
    monkeypatch.setenv("GATEWAY_OUTPUT_ROOT", "/mnt/custom")

    settings = load_settings(cfg)

    assert settings.host == "0.0.0.0"
    assert settings.port == 8080
    assert settings.output_root == "/mnt/custom"


def test_toml_sets_ops_knobs_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #3: TOML values for host/port/url_base/output_root take effect when
    no ``GATEWAY_<NAME>`` env var is set — env > TOML > default."""
    # Strip any inherited env that would clobber TOML.
    for name in (
        "GATEWAY_HOST",
        "GATEWAY_PORT",
        "GATEWAY_URL_BASE",
        "GATEWAY_OUTPUT_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'api_key = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        'host = "10.0.0.5"\n'
        "port = 7777\n"
        'url_base = "/manga"\n'
        'output_root = "/srv/manga"\n',
        encoding="utf-8",
    )

    settings = load_settings(cfg)

    assert settings.host == "10.0.0.5"
    assert settings.port == 7777
    assert settings.url_base == "/manga"
    assert settings.output_root == "/srv/manga"


def test_env_beats_toml_per_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #3: env wins on a per-field basis — TOML still supplies fields the
    env didn't set, defaults supply the rest."""
    monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")  # env wins for host only
    monkeypatch.delenv("GATEWAY_PORT", raising=False)
    monkeypatch.delenv("GATEWAY_URL_BASE", raising=False)
    monkeypatch.delenv("GATEWAY_OUTPUT_ROOT", raising=False)

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'api_key = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        'host = "10.0.0.5"\n'
        "port = 7777\n"
        'url_base = "/manga"\n',
        encoding="utf-8",
    )

    settings = load_settings(cfg)

    assert settings.host == "0.0.0.0"  # env override
    assert settings.port == 7777  # TOML value (env unset)
    assert settings.url_base == "/manga"  # TOML value
    assert settings.output_root == "/data/manga"  # default (neither set)


def test_lowercase_env_var_still_beats_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (CodeRabbit PR #27): pydantic-settings is ``case_sensitive=False``
    by default, so a lowercase ``GATEWAY_host`` is a valid env override. The
    TOML→init-kwarg guard must therefore detect env vars case-insensitively —
    otherwise the TOML value gets passed as an init kwarg and beats the env."""
    for name in ("GATEWAY_HOST", "GATEWAY_host", "GATEWAY_Host"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_host", "0.0.0.0")  # lowercase env var

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'api_key = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\nhost = "10.0.0.5"\n',
        encoding="utf-8",
    )

    settings = load_settings(cfg)

    # Env wins even when its name casing doesn't match the field exactly.
    assert settings.host == "0.0.0.0"


def test_unknown_toml_keys_are_ignored(tmp_path: Path) -> None:
    """Issue #3: TOML keys that don't match a Settings field are silently
    dropped — mirrors ``model_config = ConfigDict(extra='ignore')``."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'api_key = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        'made_up_key = "ignored"\n'
        "port = 7777\n",
        encoding="utf-8",
    )

    settings = load_settings(cfg)

    assert settings.port == 7777
    assert not hasattr(settings, "made_up_key")


# ── Search-result / chapter-link caching knobs (Phase 9, D-07/D-08/D-09) ───────

_DUMMY_KEY = "k-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_enum_cache_defaults() -> None:
    """D-07/D-08: kill-switch defaults ON; maxsize 512; ttl 1800 (30 min)."""
    settings = Settings(api_key=_DUMMY_KEY)
    assert settings.enum_cache_enabled is True
    assert settings.enum_cache_maxsize == 512
    assert settings.enum_cache_ttl_seconds == 1800


def test_enum_cache_ttl_ceiling_rejects_above_handle_ttl() -> None:
    """D-09/CACHE-05: the enum-cache TTL ceiling is ANCHORED to handle_ttl_seconds.

    The ceiling is no longer the hard-coded 3600 — it follows the configured handle
    TTL (release-no-longer-resolvable). With the default 21600s handle TTL, a value
    that used to be rejected (3601) is now accepted; only a value above the ACTUAL
    handle TTL fails fast.
    """
    # 21600 (the default handle TTL ceiling) is allowed...
    at_ceiling = Settings(api_key=_DUMMY_KEY, enum_cache_ttl_seconds=21600)
    assert at_ceiling.enum_cache_ttl_seconds == 21600
    # ...3601 — previously rejected against the 3600 ceiling — is now FINE.
    raised = Settings(api_key=_DUMMY_KEY, enum_cache_ttl_seconds=3601)
    assert raised.enum_cache_ttl_seconds == 3601
    # ...but one second over the configured handle TTL is rejected at construction.
    with pytest.raises(ValidationError, match="21600"):
        Settings(api_key=_DUMMY_KEY, enum_cache_ttl_seconds=21601)


def test_enum_cache_ttl_ceiling_tracks_lowered_handle_ttl() -> None:
    """A LOWERED handle TTL re-anchors the ceiling downward (not a fixed 3600)."""
    ok = Settings(
        api_key=_DUMMY_KEY, handle_ttl_seconds=1800, enum_cache_ttl_seconds=1800
    )
    assert ok.enum_cache_ttl_seconds == 1800
    with pytest.raises(ValidationError, match="1800"):
        Settings(
            api_key=_DUMMY_KEY, handle_ttl_seconds=1800, enum_cache_ttl_seconds=1801
        )


def test_handle_ttl_default_is_six_hours() -> None:
    """HDL-02: default raised from 3600 → 21600 (6h), the confirmed fix for the
    'release no longer resolvable' production misses."""
    settings = Settings(api_key=_DUMMY_KEY)
    assert settings.handle_ttl_seconds == 21600


def test_enum_cache_empty_ttl_default_and_ceiling() -> None:
    """Mode-E (debug comix-warm-hydration-wait): the empty (negative-cache) TTL
    defaults to 60s and MUST stay <= the full enum-cache TTL so a transient empty
    expires SOONER than a real result."""
    settings = Settings(api_key=_DUMMY_KEY)
    assert settings.enum_cache_empty_ttl_seconds == 60
    # Equal to the full TTL is allowed (the boundary)...
    ok = Settings(
        api_key=_DUMMY_KEY,
        enum_cache_ttl_seconds=1800,
        enum_cache_empty_ttl_seconds=1800,
    )
    assert ok.enum_cache_empty_ttl_seconds == 1800
    # ...one second above the full TTL is rejected at construction (self-defeating).
    with pytest.raises(ValidationError, match="enum_cache_empty_ttl_seconds"):
        Settings(
            api_key=_DUMMY_KEY,
            enum_cache_ttl_seconds=1800,
            enum_cache_empty_ttl_seconds=1801,
        )


def test_handle_ttl_rejects_below_floor() -> None:
    """HDL-02: ge=1800 — the TTL must stay >= the 30-min Mangarr floor."""
    with pytest.raises(ValidationError):
        Settings(api_key=_DUMMY_KEY, handle_ttl_seconds=1799)


def test_env_overrides_handle_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-11: GATEWAY_HANDLE_TTL_SECONDS env var overrides the default."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'api_key = "{_DUMMY_KEY}"\n', encoding="utf-8")

    monkeypatch.setenv("GATEWAY_HANDLE_TTL_SECONDS", "43200")

    settings = load_settings(cfg)

    assert settings.handle_ttl_seconds == 43200


def test_handle_db_path_default() -> None:
    """D-16 reversal: the handle store now has a SQLite path knob (relative default)."""
    settings = Settings(api_key=_DUMMY_KEY)
    assert settings.handle_db_path == "handles.db"


def test_enum_cache_maxsize_rejects_non_positive() -> None:
    """T-09-05: ge=1 — a zero/negative maxsize is rejected at construction."""
    with pytest.raises(ValidationError):
        Settings(api_key=_DUMMY_KEY, enum_cache_maxsize=0)


def test_enum_cache_ttl_rejects_non_positive() -> None:
    """T-09-05: ge=1 — a zero/negative ttl is rejected at construction."""
    with pytest.raises(ValidationError):
        Settings(api_key=_DUMMY_KEY, enum_cache_ttl_seconds=0)


def test_env_overrides_enum_cache_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-08/D-11: GATEWAY_ENUM_CACHE_* env vars override the defaults."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'api_key = "{_DUMMY_KEY}"\n', encoding="utf-8")

    monkeypatch.setenv("GATEWAY_ENUM_CACHE_ENABLED", "0")
    monkeypatch.setenv("GATEWAY_ENUM_CACHE_TTL_SECONDS", "900")
    monkeypatch.setenv("GATEWAY_ENUM_CACHE_MAXSIZE", "64")

    settings = load_settings(cfg)

    assert settings.enum_cache_enabled is False
    assert settings.enum_cache_ttl_seconds == 900
    assert settings.enum_cache_maxsize == 64


def test_handle_maxsize_default() -> None:
    """HDL-02/GAP-2: raised default cap (was 10_000 → 739 production rejections)."""
    settings = Settings(api_key=_DUMMY_KEY)
    assert settings.handle_maxsize == 200_000


def test_handle_maxsize_rejects_non_positive() -> None:
    """T-txd-02: ge=1 — a zero/negative maxsize is rejected at construction."""
    with pytest.raises(ValidationError):
        Settings(api_key=_DUMMY_KEY, handle_maxsize=0)


def test_env_overrides_handle_maxsize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-11: GATEWAY_HANDLE_MAXSIZE env var overrides the default."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'api_key = "{_DUMMY_KEY}"\n', encoding="utf-8")

    monkeypatch.setenv("GATEWAY_HANDLE_MAXSIZE", "50000")

    settings = load_settings(cfg)

    assert settings.handle_maxsize == 50000


def test_warm_retry_defaults() -> None:
    """#153: eager-warm retry defaults — 3 attempts, 2.0s base backoff."""
    settings = Settings(api_key=_DUMMY_KEY)
    assert settings.cloudflare_warm_attempts == 3
    assert settings.cloudflare_warm_retry_seconds == 2.0


def test_warm_attempts_rejects_non_positive() -> None:
    """#153: ge=1 — at least one attempt is required."""
    with pytest.raises(ValidationError):
        Settings(api_key=_DUMMY_KEY, cloudflare_warm_attempts=0)


def test_warm_retry_seconds_rejects_negative() -> None:
    """#153: ge=0.0 — negative backoff is rejected at construction."""
    with pytest.raises(ValidationError):
        Settings(api_key=_DUMMY_KEY, cloudflare_warm_retry_seconds=-1.0)


def test_env_overrides_warm_retry_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-11/#153: GATEWAY_CLOUDFLARE_WARM_* env vars override the defaults."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'api_key = "{_DUMMY_KEY}"\n', encoding="utf-8")

    monkeypatch.setenv("GATEWAY_CLOUDFLARE_WARM_ATTEMPTS", "5")
    monkeypatch.setenv("GATEWAY_CLOUDFLARE_WARM_RETRY_SECONDS", "0.5")

    settings = load_settings(cfg)

    assert settings.cloudflare_warm_attempts == 5
    assert settings.cloudflare_warm_retry_seconds == 0.5
