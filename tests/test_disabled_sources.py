"""GATEWAY_DISABLED_SOURCES — reversible source disable (#198/#202).

A source key listed in ``GATEWAY_DISABLED_SOURCES`` is skipped at registration,
so it never reaches ``/caps``, search, or the CF solver warm set. Default (unset)
registers every built-in source — the offline-test invariant the rest of the
suite depends on.
"""

from __future__ import annotations

import pytest

from manga_gateway.config import Settings
from manga_gateway.framework.registry import SourceRegistry
from manga_gateway.sources import register_builtin_sources

# The full built-in set (kept in sync with register_builtin_sources).
_ALL_SOURCES = frozenset(
    {
        "mangadex",
        "comix",
        "mangaball",
        "mangadot",
        "atsumaru",
        "weebcentral",
        "kagane",
        "mangafire",
        "projectsuki",
    }
)


def _registered(disabled: object = None) -> frozenset[str]:
    reg = SourceRegistry()
    register_builtin_sources(reg, disabled=disabled)  # type: ignore[arg-type]
    return frozenset(reg.keys())


def test_default_registers_all_builtin_sources() -> None:
    """No ``disabled`` arg → every built-in source registers (offline invariant)."""
    assert _registered() == _ALL_SOURCES


def test_disabled_subset_is_skipped() -> None:
    assert _registered({"kagane", "mangadot"}) == _ALL_SOURCES - {"kagane", "mangadot"}


def test_disabled_is_case_insensitive() -> None:
    assert _registered({"Kagane", "MANGADOT"}) == _ALL_SOURCES - {"kagane", "mangadot"}


def test_unknown_disabled_key_is_ignored() -> None:
    """A key that matches no source is a harmless no-op (no crash, all register)."""
    assert _registered({"does-not-exist"}) == _ALL_SOURCES


def test_empty_collection_registers_all() -> None:
    assert _registered(frozenset()) == _ALL_SOURCES


def test_disabled_source_keys_parses_comma_separated() -> None:
    settings = Settings(api_key="k", disabled_sources="kagane,mangadot")
    assert settings.disabled_source_keys() == frozenset({"kagane", "mangadot"})


def test_disabled_source_keys_strips_lowercases_and_drops_blanks() -> None:
    settings = Settings(api_key="k", disabled_sources=" Kagane , ManGadot ,, ")
    assert settings.disabled_source_keys() == frozenset({"kagane", "mangadot"})


def test_disabled_source_keys_empty_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env → empty set, so all sources register (the default behavior)."""
    monkeypatch.delenv("GATEWAY_DISABLED_SOURCES", raising=False)
    settings = Settings(api_key="k")
    assert settings.disabled_sources == ""
    assert settings.disabled_source_keys() == frozenset()


def test_settings_reads_disabled_sources_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GATEWAY_ prefix binds GATEWAY_DISABLED_SOURCES → disabled_sources."""
    monkeypatch.setenv("GATEWAY_DISABLED_SOURCES", "kagane, mangadot")
    settings = Settings(api_key="k")
    assert settings.disabled_source_keys() == frozenset({"kagane", "mangadot"})
