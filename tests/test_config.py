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
