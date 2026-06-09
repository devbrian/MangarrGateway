"""locate_checkbox proven LAYOUT-DRIVEN: differently-shaped Turnstile rects yield
DIFFERENT tap coordinates (no hardcoded coordinate).

mangadot's and kagane's challenge widgets sit at different positions; a hardcoded
``(73, 977)`` cleared mangadot but missed kagane — this test pins that the locator
reads the live DOM rect instead.
"""

from __future__ import annotations

import json

from android_solver.turnstile import locate_checkbox

# Content quads: 4 (x, y) corners. mangadot's checkbox sits low-left (~73,977);
# kagane's sits elsewhere (~230,320) — distinct layouts → distinct centers.
_MANGADOT_QUAD = [40, 960, 106, 960, 106, 994, 40, 994]  # center (73, 977)
_KAGANE_QUAD = [200, 300, 260, 300, 260, 340, 200, 340]  # center (230, 320)


class FakeDomWs:
    """Mock CDP socket answering DOM.getDocument / querySelector / getBoxModel."""

    def __init__(self, content_quad: list[int], *, match_node: int = 42) -> None:
        self._results = {
            "DOM.getDocument": {"root": {"nodeId": 1}},
            "DOM.querySelector": {"nodeId": match_node},
            "DOM.getBoxModel": {"model": {"content": content_quad}},
        }
        self._pending: list[dict] = []

    def send(self, payload: str) -> None:
        msg = json.loads(payload)
        self._pending.append(
            {"id": msg["id"], "result": self._results.get(msg["method"], {})}
        )

    def recv(self) -> str:
        return json.dumps(self._pending.pop(0))

    def close(self) -> None:  # pragma: no cover - locate_checkbox does not close ws
        pass


def test_locate_checkbox_is_layout_driven_not_hardcoded() -> None:
    mangadot = locate_checkbox(FakeDomWs(_MANGADOT_QUAD))
    kagane = locate_checkbox(FakeDomWs(_KAGANE_QUAD))

    # Both located, and crucially the coordinates DIFFER per layout.
    assert mangadot == (73, 977)
    assert kagane == (230, 320)
    assert mangadot != kagane


def test_locate_checkbox_scales_by_device_pixel_ratio() -> None:
    coords = locate_checkbox(FakeDomWs(_MANGADOT_QUAD), device_pixel_ratio=2.0)
    assert coords == (146, 1954)


def test_locate_checkbox_skips_unmatched_selector() -> None:
    # querySelector returns 0 (no match) for every selector ⇒ DOM path yields None.
    ws = FakeDomWs(_MANGADOT_QUAD, match_node=0)
    assert locate_checkbox(ws) is None


def test_locate_checkbox_falls_back_to_screenshot() -> None:
    calls: list[bytes] = []

    def fake_locator(png: bytes):
        calls.append(png)
        return (321, 654)

    coords = locate_checkbox(
        ws=None,
        screencap=b"\x89PNGdata",
        screenshot_locator=fake_locator,
    )

    assert coords == (321, 654)
    assert calls == [b"\x89PNGdata"]


def test_locate_checkbox_returns_none_when_nothing_located() -> None:
    assert locate_checkbox(ws=None) is None
