"""MangaFireSource ``/caps`` advertisement + live-profile load (12-02 Task 3).

Registering ``MangaFireSource`` in ``sources/__init__.py`` auto-advertises it via
``registry.caps()`` (CAPS-02) with NO route/app change (D-15). These tests assert the
registration + the ``SourceCap`` shape, and that the live-smoke profile loads cleanly
via the live-collection ``_load_profile`` path (D-16/D-50 — the profile MUST ship in
the same PR as the registration).
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.sources.mangafire import MangaFireSource


def test_mangafire_class_declares_cloudflare_antibot() -> None:
    assert MangaFireSource.key == "mangafire"
    assert MangaFireSource.name == "MangaFire"
    assert MangaFireSource.antibot == "cloudflare"
    assert MangaFireSource.cloudflare_challenge_url == "https://mangafire.to/"
    # The JSON API is plaintext — no response-byte decrypt seam (260706-hgu).
    assert MangaFireSource.decrypt_scheme is None
    assert MangaFireSource.session_prep is None
    assert MangaFireSource.supports_search is True
    assert MangaFireSource.supports_recent is True
    assert MangaFireSource.id_types == []  # title-search fallback (SRCH-07)


def test_mangafire_downloads_run_concurrently() -> None:
    """Download jobs run concurrently (max_concurrent_jobs == 3): the manifest is a
    single cheap httpx GET (/api/chapters/{id}, 260706-hgu) with no reader nav, so there
    is no per-job contention to serialize. D-14 probe-measured safe value. Search/recent
    use the separate fan-out semaphore (_CHAPTERS_FANOUT_CONCURRENCY), not this knob."""
    assert MangaFireSource.max_concurrent_jobs == 3


def test_mangafire_is_registered_in_the_builtin_registry() -> None:
    from manga_gateway.framework.registry import SourceRegistry
    from manga_gateway.sources import register_builtin_sources

    registry = SourceRegistry()
    register_builtin_sources(registry)
    assert "mangafire" in registry.keys()
    assert registry.get("mangafire") is MangaFireSource


@pytest.mark.asyncio
async def test_caps_advertises_mangafire_source(client: httpx.AsyncClient) -> None:
    resp = await client.get("/caps")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    mf = next((s for s in sources if s["key"] == "mangafire"), None)
    assert mf is not None, "mangafire not advertised in /caps"
    assert mf["antibot"] == "cloudflare"
    assert mf["enabled"] is True
    assert mf["supportsSearch"] is True
    assert mf["supportsRecent"] is True
    assert mf["rateLimitPerMinute"] > 0
    assert mf["languages"]  # non-empty


def test_mangafire_live_profile_loads_without_d50_error() -> None:
    """D-16/D-50: the live-smoke profile resolves via the live-collection seam."""
    from tests.live.conftest import REGISTERED_KEYS, _load_profile
    from tests.live.profiles._base import LiveSmokeProfile

    assert "mangafire" in REGISTERED_KEYS
    profile = _load_profile("mangafire")
    assert isinstance(profile, LiveSmokeProfile)
    assert profile.source_key == "mangafire"
    assert profile.expected_caps_antibot == "cloudflare"
    assert profile.needs_solver_warm is True
    assert profile.default_query.strip()
