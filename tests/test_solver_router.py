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
from manga_gateway.sources.mangaball import MangaBallSource
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
        self.browser_calls: list[tuple[str, dict[str, object]]] = []
        self.paginated_calls: list[tuple[str, dict[str, object]]] = []
        self.parallel_calls: list[tuple[str, dict[str, object]]] = []
        self.typed_calls: list[tuple[str, dict[str, object]]] = []
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

    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — mirrors the backend's op-budget kwarg
    ) -> object:
        self.browser_calls.append(
            (url, {"extract": extract, "wait_for": wait_for, "timeout": timeout})
        )
        return f"{self._tag}-browser-{url}"

    async def fetch_via_browser_paginated(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        next_selector: str,
        route_limit_rewrite: tuple[str, int] | None = None,
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — mirrors the backend's op-budget kwarg
    ) -> list[object]:
        self.paginated_calls.append(
            (
                url,
                {
                    "extract": extract,
                    "wait_for": wait_for,
                    "next_selector": next_selector,
                    "route_limit_rewrite": route_limit_rewrite,
                    "max_pages": max_pages,
                    "timeout": timeout,
                },
            )
        )
        return [f"{self._tag}-paginated-{url}"]

    async def fetch_via_browser_parallel_pages(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        last_page_selector: str,
        page_param: str,
        route_rewrite: tuple[str, int],
        max_pages: int = 200,
        timeout: float = 30.0,  # noqa: ASYNC109 — mirrors the backend's op-budget kwarg
    ) -> list[object]:
        self.parallel_calls.append(
            (
                url,
                {
                    "extract": extract,
                    "wait_for": wait_for,
                    "last_page_selector": last_page_selector,
                    "page_param": page_param,
                    "route_rewrite": route_rewrite,
                    "max_pages": max_pages,
                    "timeout": timeout,
                },
            )
        )
        return [f"{self._tag}-parallel-{url}"]

    async def fetch_via_browser_typed(
        self,
        url: str,
        *,
        type_selector: str,
        type_text: str,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — mirrors the backend's op-budget kwarg
    ) -> object:
        self.typed_calls.append(
            (
                url,
                {
                    "type_selector": type_selector,
                    "type_text": type_text,
                    "extract": extract,
                    "wait_for": wait_for,
                    "timeout": timeout,
                },
            )
        )
        return f"{self._tag}-typed-{url}"

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
    # #243: mangaball joined the android leg after its CF escalation proved
    # unclearable by desktop Chromium from Linux (see mangaball-cloudflare-csrf-243).
    assert MangaBallSource.solver_engine == "android"


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


# ─────────────── off-Protocol browser primitives (D-41, comix gate) ─────────────


def test_router_exposes_comix_browser_primitives() -> None:
    """Exactly comix ``_solver_from_ctx``'s gate: the router must expose BOTH
    off-Protocol browser primitives so it is a faithful superset of CloudflareSolver.
    Plan 14-02 adds the inverse ``eval_in_webview`` primitive (routed to android)."""
    router, _, _ = _router()
    assert hasattr(router, "fetch_via_browser")
    assert hasattr(router, "fetch_via_browser_paginated")
    assert hasattr(router, "fetch_via_browser_parallel_pages")
    assert hasattr(router, "fetch_via_browser_typed")
    assert hasattr(router, "eval_in_webview")


# ─────────────── off-Protocol in-WebView eval (EVAL-02, comix gate) ─────────────


class _FakeEvalBackend:
    """A backend recording ``eval_in_webview`` calls so the router's INVERSE
    delegation (→ android, NOT patchright) can be pinned. Both fakes record, so the
    test proves the android fake was hit and the patchright fake was NOT."""

    def __init__(self, *, tag: str) -> None:
        self._tag = tag
        self.eval_calls: list[tuple[str, str, dict[str, object]]] = []

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — mirrors the backend's op-budget kwarg
    ) -> object:
        self.eval_calls.append(
            (challenge_url, js, {"wait_for": wait_for, "timeout": timeout})
        )
        return f"{self._tag}-eval"


@pytest.mark.asyncio
async def test_eval_in_webview_delegates_to_android() -> None:
    """The INVERSE of the browser passthroughs: ``eval_in_webview`` routes to the
    ANDROID backend (the one with the cleared WebView), never patchright."""
    patchright = _FakeEvalBackend(tag="pw")
    android = _FakeEvalBackend(tag="droid")
    router = SolverRouter(
        patchright=patchright,  # type: ignore[arg-type]
        android=android,  # type: ignore[arg-type]
        engine_by_source={},
    )
    result = await router.eval_in_webview(
        "https://comix.to/",
        "return await c.list({})",
        wait_for="() => true",
        timeout=42.0,
    )
    assert result == "droid-eval"
    assert android.eval_calls == [
        (
            "https://comix.to/",
            "return await c.list({})",
            {"wait_for": "() => true", "timeout": 42.0},
        )
    ]
    assert patchright.eval_calls == []  # patchright (browser engine) NEVER consulted


@pytest.mark.asyncio
async def test_fetch_via_browser_delegates_to_patchright() -> None:
    router, patchright, android = _router()
    result = await router.fetch_via_browser(
        "https://comix.to/x", extract="return 1", wait_for="#sel", timeout=12.0
    )
    assert result == "pw-browser-https://comix.to/x"
    assert patchright.browser_calls == [
        (
            "https://comix.to/x",
            {"extract": "return 1", "wait_for": "#sel", "timeout": 12.0},
        )
    ]
    assert android.browser_calls == []  # android (no browser) is NEVER consulted


@pytest.mark.asyncio
async def test_fetch_via_browser_paginated_delegates_to_patchright() -> None:
    router, patchright, android = _router()
    result = await router.fetch_via_browser_paginated(
        "https://comix.to/series",
        extract="return rows",
        wait_for="#ready",
        next_selector=".next",
        route_limit_rewrite=("api/v1/chapters", 500),
        max_pages=50,
        timeout=20.0,
    )
    assert result == ["pw-paginated-https://comix.to/series"]
    assert patchright.paginated_calls == [
        (
            "https://comix.to/series",
            {
                "extract": "return rows",
                "wait_for": "#ready",
                "next_selector": ".next",
                "route_limit_rewrite": ("api/v1/chapters", 500),
                "max_pages": 50,
                "timeout": 20.0,
            },
        )
    ]
    assert android.paginated_calls == []  # android (no browser) is NEVER consulted


@pytest.mark.asyncio
async def test_fetch_via_browser_parallel_pages_delegates_to_patchright() -> None:
    router, patchright, android = _router()
    result = await router.fetch_via_browser_parallel_pages(
        "https://comix.to/series",
        extract="return rows",
        wait_for="#ready",
        last_page_selector='button[aria-label*="Last"]',
        page_param="page",
        route_rewrite=("/chapters", 100),
        max_pages=50,
        timeout=20.0,
    )
    assert result == ["pw-parallel-https://comix.to/series"]
    assert patchright.parallel_calls == [
        (
            "https://comix.to/series",
            {
                "extract": "return rows",
                "wait_for": "#ready",
                "last_page_selector": 'button[aria-label*="Last"]',
                "page_param": "page",
                "route_rewrite": ("/chapters", 100),
                "max_pages": 50,
                "timeout": 20.0,
            },
        )
    ]
    assert android.parallel_calls == []  # android (no browser) is NEVER consulted


@pytest.mark.asyncio
async def test_fetch_via_browser_typed_delegates_to_patchright() -> None:
    router, patchright, android = _router()
    result = await router.fetch_via_browser_typed(
        "https://mangafire.to/home",
        type_selector=".search-inner input[name=keyword]",
        type_text="blue lock",
        extract="return vrf",
        wait_for="() => !!window.__vrf__",
        timeout=18.0,
    )
    assert result == "pw-typed-https://mangafire.to/home"
    assert patchright.typed_calls == [
        (
            "https://mangafire.to/home",
            {
                "type_selector": ".search-inner input[name=keyword]",
                "type_text": "blue lock",
                "extract": "return vrf",
                "wait_for": "() => !!window.__vrf__",
                "timeout": 18.0,
            },
        )
    ]
    assert android.typed_calls == []  # android (no browser) is NEVER consulted
