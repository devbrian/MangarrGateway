"""Focused unit test for ``CloudflareSolver.fetch_via_browser_paginated`` (#146).

Drives the generic browser-paginate primitive against a hand-rolled
FakeContext/FakePage double — NO real browser is launched (D-42). The fake
``evaluate`` dispatches on the script body so one page object can model the
extract read, the next-button probe, and the JS-click advance:

* the extract wrapper (``async () => { ... }``) returns the current pagination
  page's rows;
* the next-button probe (contains ``present``) returns ``{present, disabled}``;
* the JS-click (contains ``.click()``) advances the internal page index.

Coverage (plan Task 1 behavior):
  1. merged + deduped full list across ~3 pages (an id repeated across pages
     appears once);
  2. a low id present ONLY on the last page is returned (the full walk);
  3. stop on absent next;
  4. stop on no-new-rows (bounded poll budget, no infinite spin);
  5. ``max_pages`` emits a warning (caplog) and returns without spinning;
  6. ``route_limit_rewrite`` installs a handler that rewrites ``limit=20`` →
     the target on a matching URL and leaves a non-matching URL untouched.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from manga_gateway.framework import antibot
from manga_gateway.framework.antibot import BrowserFetchError, CloudflareSolver
from manga_gateway.framework.solver_lifecycle import BrowserLifecycle

_EXTRACT = "return window.__rows__;"
_NEXT = 'button[aria-label*="Next"]'


class _FakePaginatePage:
    """A FakePage modeling a paginated list behind a Next control.

    ``pages`` is a list of per-pagination-page row lists. ``evaluate`` returns
    the CURRENT page's rows for the extract script, a ``{present, disabled}``
    dict for the probe, and advances the page index on the click script.
    """

    def __init__(
        self,
        pages: list[list[dict[str, Any]]],
        *,
        next_always_present: bool = False,
        next_disabled: bool = False,
        click_advances: bool = True,
    ) -> None:
        self.pages = pages
        self.index = 0
        self.next_always_present = next_always_present
        self.next_disabled = next_disabled
        self.click_advances = click_advances
        self.routes: list[tuple[str, Any]] = []
        self.goto_calls: list[str] = []
        self.evaluate_scripts: list[str] = []
        self.closed = False

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, **_: object) -> None:
        self.goto_calls.append(url)

    async def wait_for_function(self, *_: object, **__: object) -> None:
        return None

    async def wait_for_selector(self, *_: object, **__: object) -> None:
        return None

    async def evaluate(self, script: str, *_args: object) -> object:
        self.evaluate_scripts.append(script)
        if script.startswith("async () => {"):
            return list(self.pages[self.index])
        if ".click()" in script:
            if self.click_advances and self.index < len(self.pages) - 1:
                self.index += 1
            return None
        if "present" in script:
            if self.next_always_present:
                return {"present": True, "disabled": self.next_disabled}
            has_next = self.index < len(self.pages) - 1
            return {"present": has_next, "disabled": self.next_disabled}
        raise AssertionError(f"unexpected evaluate script: {script!r}")

    async def close(self) -> None:
        self.closed = True


class _InfinitePaginatePage:
    """A page that ALWAYS has a present Next and yields fresh ids per click —
    would spin forever without the ``max_pages`` guard."""

    def __init__(self) -> None:
        self.index = 0
        self.closed = False
        self.routes: list[tuple[str, Any]] = []

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, **_: object) -> None:
        return None

    async def wait_for_function(self, *_: object, **__: object) -> None:
        return None

    async def wait_for_selector(self, *_: object, **__: object) -> None:
        return None

    async def evaluate(self, script: str, *_args: object) -> object:
        if script.startswith("async () => {"):
            return [{"id": f"ch-{self.index}-a"}, {"id": f"ch-{self.index}-b"}]
        if ".click()" in script:
            self.index += 1
            return None
        if "present" in script:
            return {"present": True, "disabled": False}
        raise AssertionError(f"unexpected evaluate script: {script!r}")

    async def close(self) -> None:
        self.closed = True


class _FakePaginateContext:
    def __init__(self, page: Any) -> None:
        self._page = page
        self.closed = False

    async def new_page(self) -> Any:
        return self._page

    async def cookies(self) -> list[dict[str, str]]:  # pragma: no cover — unused
        return []

    async def close(self) -> None:
        self.closed = True


def _solver_with(ctx: _FakePaginateContext) -> CloudflareSolver:
    async def _launch() -> _FakePaginateContext:
        return ctx

    async def _solve(_ctx: object, _key: str) -> antibot.Clearance:  # pragma: no cover
        return antibot.Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_launch, solve=_solve, solve_concurrency=1)
    return CloudflareSolver(lifecycle=lc)


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the bounded poll budget tiny so the no-new-rows stop is fast."""
    monkeypatch.setattr(antibot, "_PAGINATE_POLL_TICKS", 3)
    monkeypatch.setattr(antibot, "_PAGINATE_POLL_INTERVAL", 0.001)


# ─────────────────────────── (1) merged + deduped ───────────────────────────


async def test_paginated_merges_and_dedups_across_pages() -> None:
    pages = [
        [{"id": "100", "chapter": "100"}, {"id": "99", "chapter": "99"}],
        # "99" repeats across page boundary — must appear ONCE.
        [{"id": "99", "chapter": "99"}, {"id": "98", "chapter": "98"}],
        [{"id": "97", "chapter": "97"}, {"id": "96", "chapter": "96"}],
    ]
    page = _FakePaginatePage(pages)
    ctx = _FakePaginateContext(page)
    solver = _solver_with(ctx)

    result = await solver.fetch_via_browser_paginated(
        "https://x/title/abc", extract=_EXTRACT, next_selector=_NEXT
    )
    ids = [r["id"] for r in result]
    assert ids == ["100", "99", "98", "97", "96"]  # deduped, full walk
    assert page.closed is True
    await solver.aclose()


# ─────────────────── (2) low id ONLY on the last page ───────────────────


async def test_paginated_returns_low_id_only_on_last_page() -> None:
    pages = [
        [{"id": "200", "chapter": "200"}],
        [{"id": "150", "chapter": "150"}],
        [{"id": "5", "chapter": "5"}],  # the #146 case — only on the last page
    ]
    page = _FakePaginatePage(pages)
    ctx = _FakePaginateContext(page)
    solver = _solver_with(ctx)

    result = await solver.fetch_via_browser_paginated(
        "https://x/title/abc", extract=_EXTRACT, next_selector=_NEXT
    )
    ids = [r["id"] for r in result]
    assert "5" in ids
    assert ids == ["200", "150", "5"]
    await solver.aclose()


# ─────────────────────────── (3) stop on absent next ───────────────────────────


async def test_paginated_stops_when_next_absent() -> None:
    pages = [[{"id": "1", "chapter": "1"}]]  # single page, no next
    page = _FakePaginatePage(pages)
    ctx = _FakePaginateContext(page)
    solver = _solver_with(ctx)

    result = await solver.fetch_via_browser_paginated(
        "https://x/title/abc", extract=_EXTRACT, next_selector=_NEXT
    )
    assert [r["id"] for r in result] == ["1"]
    # No click was ever issued (next absent on page 1).
    assert not any(".click()" in s for s in page.evaluate_scripts)
    await solver.aclose()


async def test_paginated_stops_when_next_disabled() -> None:
    pages = [[{"id": "1", "chapter": "1"}], [{"id": "2", "chapter": "2"}]]
    # Next reports present BUT disabled → stop on page 1.
    page = _FakePaginatePage(pages, next_always_present=True, next_disabled=True)
    ctx = _FakePaginateContext(page)
    solver = _solver_with(ctx)

    result = await solver.fetch_via_browser_paginated(
        "https://x/title/abc", extract=_EXTRACT, next_selector=_NEXT
    )
    assert [r["id"] for r in result] == ["1"]
    assert not any(".click()" in s for s in page.evaluate_scripts)
    await solver.aclose()


# ─────────────────────────── (4) stop on no-new-rows ───────────────────────────


async def test_paginated_stops_when_click_adds_no_new_rows() -> None:
    pages = [[{"id": "1", "chapter": "1"}], [{"id": "2", "chapter": "2"}]]
    # Next is always present but clicking does NOT advance the page index, so
    # the rendered id-set never changes → bounded poll budget exhausts → stop.
    page = _FakePaginatePage(pages, next_always_present=True, click_advances=False)
    ctx = _FakePaginateContext(page)
    solver = _solver_with(ctx)

    result = await solver.fetch_via_browser_paginated(
        "https://x/title/abc", extract=_EXTRACT, next_selector=_NEXT
    )
    assert [r["id"] for r in result] == ["1"]  # only page 1 accumulated
    await solver.aclose()


# ─────────────────────────── (5) max_pages guard ───────────────────────────


async def test_paginated_max_pages_emits_warning_and_stops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = _InfinitePaginatePage()
    ctx = _FakePaginateContext(page)
    solver = _solver_with(ctx)

    with caplog.at_level(logging.WARNING, logger="manga_gateway"):
        result = await solver.fetch_via_browser_paginated(
            "https://x/title/abc",
            extract=_EXTRACT,
            next_selector=_NEXT,
            max_pages=4,
        )
    # Exactly max_pages pages walked (2 ids per page), then stopped — no spin.
    assert len(result) == 4 * 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("max_pages" in m and "4" in m for m in warnings)
    await solver.aclose()


# ─────────────────────────── (6) route limit-rewrite ───────────────────────────


async def test_paginated_installs_limit_rewrite_route_handler() -> None:
    pages = [[{"id": "1", "chapter": "1"}]]
    page = _FakePaginatePage(pages)
    ctx = _FakePaginateContext(page)
    solver = _solver_with(ctx)

    await solver.fetch_via_browser_paginated(
        "https://x/title/abc",
        extract=_EXTRACT,
        next_selector=_NEXT,
        route_limit_rewrite=("/chapters", 100),
    )
    # A route handler was installed BEFORE goto.
    assert len(page.routes) == 1
    pattern, handler = page.routes[0]
    assert pattern == "**/*"

    # Matching URL: limit=20 rewritten to limit=100, continue_ gets the new url.
    matching = SimpleNamespace(
        request=SimpleNamespace(
            url="https://x/api/v1/manga/abc/chapters?limit=20&page=1"
        ),
        continue_calls=[],
    )

    async def _matching_continue(**kwargs: object) -> None:
        matching.continue_calls.append(kwargs)

    matching.continue_ = _matching_continue  # type: ignore[attr-defined]
    await handler(matching)
    assert matching.continue_calls == [
        {"url": "https://x/api/v1/manga/abc/chapters?limit=100&page=1"}
    ]

    # Non-matching URL: continue_ untouched (no url kwarg).
    other = SimpleNamespace(
        request=SimpleNamespace(url="https://x/static/app.js?limit=20"),
        continue_calls=[],
    )

    async def _other_continue(**kwargs: object) -> None:
        other.continue_calls.append(kwargs)

    other.continue_ = _other_continue  # type: ignore[attr-defined]
    await handler(other)
    assert other.continue_calls == [{}]
    await solver.aclose()


# ─────────────────────────── dead-driver recycle reuse ───────────────────────────


async def test_paginated_surfaces_context_failure_as_browser_fetch_error() -> None:
    async def _bad_launch() -> object:
        raise RuntimeError("patchright failed to start")

    async def _solve(_ctx: object, _key: str) -> antibot.Clearance:  # pragma: no cover
        return antibot.Clearance(cookies={}, user_agent="ua")

    lc = BrowserLifecycle(launch=_bad_launch, solve=_solve, solve_concurrency=1)
    solver = CloudflareSolver(lifecycle=lc)
    with pytest.raises(BrowserFetchError) as exc:
        await solver.fetch_via_browser_paginated(
            "https://x/y", extract=_EXTRACT, next_selector=_NEXT
        )
    assert "context unavailable" in str(exc.value)
    await solver.aclose()
