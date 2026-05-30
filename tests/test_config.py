"""Config + key-provisioning tests (Task 3).

Covers D-01 (auto-generate-and-persist, NO env key path), D-10 (TOML is the
source of truth), D-11 (env overrides ONLY ops knobs), and Pitfall 4 (the
GATEWAY_API_KEY landmine — a regression test asserts it is ignored).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from manga_gateway.config import load_settings


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
