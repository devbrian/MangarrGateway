"""ComixSource ``/caps`` advertisement (04-03 Task 1, SRC-06 / criterion #1).

ComixSource is a declarative ``Source`` subclass on the SAME framework as MangaDex —
zero new networking/glue. These tests assert its registration + the ``SourceCap`` it
advertises in ``GET /caps``:

* ``antibot == "cloudflare+encrypted"`` (the new anti-bot level, CAPS-02).
* ``idTypes == []`` (title-search fallback, SRCH-07).
* ``rateLimitPerMinute == 120`` (probe-measured, PR #102).
* ``languages == ["en"]``.

No network, no browser — the caps document is built from the registry's declarative
class attributes (the reusability proof: a new cloudflare+encrypted source is just a
declarative subclass).
"""

from __future__ import annotations

import httpx
import pytest

from manga_gateway.framework.android_solver import AndroidSolver
from manga_gateway.sources.comix import ComixSource


@pytest.fixture(autouse=True)
def _no_real_android_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op ``AndroidSolver.warm`` for the caps assertions (the comix sibling of
    conftest's ``CloudflareSolver`` no-op).

    Phase 14 flipped comix to ``solver_engine = "android"``, so its lifespan warm
    routes through the AndroidSolver sidecar. In the deterministic suite no sidecar
    is configured, so the real warm raises (D-33) and the lifespan force-disables
    comix — which would flip ``/caps`` ``enabled`` to False here. Production HAS the
    sidecar (comix warms → enabled), so wipe warm to a no-op to assert the
    production-correct caps shape without touching the network.
    """

    async def _noop_warm(self: AndroidSolver) -> list[str]:
        return []

    monkeypatch.setattr(AndroidSolver, "warm", _noop_warm)


def test_comix_class_declares_encrypted_antibot() -> None:
    # criterion #1: declarative subclass — antibot level + identity attrs.
    assert ComixSource.antibot == "cloudflare+encrypted"
    # decrypt_scheme is inherited as None — Comix's live read path is browser-DOM
    # + plaintext httpx and never reaches framework.decrypt (issue #46 Option A).
    assert ComixSource.decrypt_scheme is None
    assert ComixSource.key == "comix"
    assert ComixSource.id_types == []  # title-search fallback (SRCH-07)
    assert ComixSource.languages == ["en"]
    assert ComixSource.rate_limit_per_minute == 120  # probe-measured (PR #102)


def test_comix_is_registered_in_the_builtin_registry() -> None:
    from manga_gateway.framework.registry import SourceRegistry
    from manga_gateway.sources import register_builtin_sources

    registry = SourceRegistry()
    register_builtin_sources(registry)
    assert "comix" in registry.keys()
    assert registry.get("comix") is ComixSource


@pytest.mark.asyncio
async def test_caps_advertises_comix_encrypted_source(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/caps")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    comix = next((s for s in sources if s["key"] == "comix"), None)
    assert comix is not None, "comix not advertised in /caps"
    assert comix["antibot"] == "cloudflare+encrypted"
    assert comix["idTypes"] == []
    assert comix["rateLimitPerMinute"] == 120
    assert comix["languages"] == ["en"]
    assert comix["name"] == "Comix"
    # enabled defaults True (no breaker tripped); dynamic per D-38.
    assert comix["enabled"] is True
