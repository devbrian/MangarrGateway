"""Offline unit tests for :class:`SolverRouter` (Plan 10-03, SRC-01/SRC-02).

Deterministic — fake patchright + android backends (no browser, no sidecar). The
tests pin the per-source engine dispatch (comix → patchright, mangadot/kagane →
android), the D-35 ``force_resolve`` pass-through, the union-of-failed-keys warm
contract, the close-both-even-if-one-raises aclose, the BOT-01 unmapped-key → None
fall-through, and that the router satisfies the runtime-checkable ``AntiBotSolver``
Protocol. Also asserts the source ``solver_engine`` class-attrs (mangadot/kagane =
"android"; the base default = "patchright").
"""

from __future__ import annotations

import pytest

from manga_gateway.framework.antibot import AntiBotSolver, Clearance
from manga_gateway.framework.base import Source
from manga_gateway.framework.solver_router import SolverRouter
from manga_gateway.sources.kagane import KaganeSource
from manga_gateway.sources.mangadot import MangadotSource


class _FakeBackend:
    """A minimal AntiBotSolver: returns a tagged Clearance for its OWN keys only."""

    def __init__(
        self, *, owns: set[str], tag: str, fails: list[str] | None = None
    ) -> None:
        self._owns = owns
        self._tag = tag
        self._fails = fails or []
        self.calls: list[tuple[str, bool]] = []
        self.warmed = 0
        self.closed = 0

    async def get_clearance(
        self, source_key: str, *, force_resolve: bool = False
    ) -> Clearance | None:
        self.calls.append((source_key, force_resolve))
        if source_key not in self._owns:
            return None
        return Clearance(
            cookies={"cf_clearance": f"{self._tag}-{source_key}"}, user_agent=self._tag
        )

    async def warm(self) -> list[str]:
        self.warmed += 1
        return list(self._fails)

    async def aclose(self) -> None:
        self.closed += 1


def _router(**over: object) -> tuple[SolverRouter, _FakeBackend, _FakeBackend]:
    patchright = _FakeBackend(owns={"comix"}, tag="pw")
    android = _FakeBackend(owns={"mangadot", "kagane"}, tag="droid")
    engine_map = {
        "comix": "patchright",
        "mangadot": "android",
        "kagane": "android",
    }
    kwargs: dict[str, object] = {
        "patchright": patchright,
        "android": android,
        "engine_by_source": engine_map,
    }
    kwargs.update(over)
    return SolverRouter(**kwargs), patchright, android  # type: ignore[arg-type]


def test_satisfies_antibot_protocol() -> None:
    router, _, _ = _router()
    assert isinstance(router, AntiBotSolver)


# ───────────────────────── source class-attr selection ─────────────────────────


def test_source_default_engine_is_patchright() -> None:
    assert Source.solver_engine == "patchright"


def test_mangadot_and_kagane_select_android() -> None:
    assert MangadotSource.solver_engine == "android"
    assert KaganeSource.solver_engine == "android"


# ───────────────────────────── per-source dispatch ─────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_comix_to_patchright() -> None:
    router, patchright, android = _router()
    clr = await router.get_clearance("comix")
    assert clr is not None
    assert clr.cookies == {"cf_clearance": "pw-comix"}
    assert patchright.calls == [("comix", False)]
    assert android.calls == []  # android backend untouched (R1 / comix unchanged)


@pytest.mark.asyncio
async def test_dispatch_mangadot_to_android() -> None:
    router, patchright, android = _router()
    clr = await router.get_clearance("mangadot")
    assert clr is not None
    assert clr.cookies == {"cf_clearance": "droid-mangadot"}
    assert android.calls == [("mangadot", False)]
    assert patchright.calls == []


@pytest.mark.asyncio
async def test_force_resolve_passes_through_to_backend() -> None:
    router, _, android = _router()
    await router.get_clearance("kagane", force_resolve=True)
    assert android.calls == [("kagane", True)]


@pytest.mark.asyncio
async def test_unmapped_key_returns_none() -> None:
    """BOT-01: an unmapped/non-cloudflare key falls through to the patchright backend,
    which returns None for a key it does not own."""
    router, patchright, _ = _router()
    assert await router.get_clearance("mangadex") is None
    assert patchright.calls == [("mangadex", False)]


# ───────────────────────────────── warm / aclose ───────────────────────────────


@pytest.mark.asyncio
async def test_warm_returns_union_of_failed_keys() -> None:
    patchright = _FakeBackend(owns={"comix"}, tag="pw", fails=["comix"])
    android = _FakeBackend(owns={"mangadot", "kagane"}, tag="droid", fails=["kagane"])
    router = SolverRouter(
        patchright=patchright,
        android=android,
        engine_by_source={"comix": "patchright", "kagane": "android"},
    )
    failed = await router.warm()
    assert patchright.warmed == 1
    assert android.warmed == 1
    assert set(failed) == {"comix", "kagane"}


@pytest.mark.asyncio
async def test_warm_dedupes_overlapping_failed_keys() -> None:
    patchright = _FakeBackend(owns=set(), tag="pw", fails=["x"])
    android = _FakeBackend(owns=set(), tag="droid", fails=["x"])
    router = SolverRouter(patchright=patchright, android=android, engine_by_source={})
    assert await router.warm() == ["x"]


@pytest.mark.asyncio
async def test_aclose_closes_both_even_if_one_raises() -> None:
    patchright = _FakeBackend(owns=set(), tag="pw")
    android = _FakeBackend(owns=set(), tag="droid")

    async def _boom() -> None:
        raise RuntimeError("patchright close failed")

    patchright.aclose = _boom  # type: ignore[method-assign]
    router = SolverRouter(patchright=patchright, android=android, engine_by_source={})
    await router.aclose()  # must NOT raise
    assert android.closed == 1  # android still closed despite patchright raising
