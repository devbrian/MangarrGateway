"""locate_checkbox proven LAYOUT-DRIVEN and OOPIF-aware.

The real Cloudflare managed challenge renders its checkbox inside a cross-origin
``challenges.cloudflare.com`` OOPIF — the PRIMARY path reads that iframe's owner
box model. Differently-shaped iframe rects (mangadot vs kagane) yield DIFFERENT
physical tap coordinates (no hardcoded coordinate), and CSS px scale to physical
px PER AXIS (URL-bar-shrunk vertical viewport ⇒ x_scale ≠ y_scale).
"""

from __future__ import annotations

import json

from android_solver.turnstile import CHECKBOX_X_OFFSET_CSS, locate_checkbox

# OOPIF iframe content quads (4 (x, y) corners) + heights. The live mangadot
# iframe measured [16,358,316,358,316,423,16,423] h=65; kagane's iframe sits at a
# different layout position → a distinct rect.
_MANGADOT_IFRAME_QUAD = [16, 358, 316, 358, 316, 423, 16, 423]
_MANGADOT_IFRAME_HEIGHT = 65
_KAGANE_IFRAME_QUAD = [40, 500, 340, 500, 340, 560, 40, 560]
_KAGANE_IFRAME_HEIGHT = 60

# Main-frame content quads (secondary path): 4 (x, y) corners.
_MANGADOT_DOM_QUAD = [40, 960, 106, 960, 106, 994, 40, 994]  # center (73, 977)
_KAGANE_DOM_QUAD = [200, 300, 260, 300, 260, 340, 200, 340]  # center (230, 320)


def _expected_oopif_point(
    quad: list[int], height: int, x_scale: float, y_scale: float
) -> tuple[int, int]:
    left = min(quad[i] for i in range(0, 8, 2))
    top = min(quad[i] for i in range(1, 8, 2))
    cssx = left + CHECKBOX_X_OFFSET_CSS
    cssy = top + height / 2.0
    return (round(cssx * x_scale), round(cssy * y_scale))


class FakeOopifWs:
    """Mock CDP socket answering the OOPIF frame-owner recipe.

    Page.getFrameTree → DOM.getFrameOwner → DOM.getBoxModel. ``frame_url`` controls
    whether the child frame is recognised as the Cloudflare OOPIF.
    """

    def __init__(
        self,
        *,
        content_quad: list[int],
        height: int,
        frame_url: str = "https://challenges.cloudflare.com/cdn-cgi/challenge",
        frame_id: str = "CF_FRAME_1",
        backend_node_id: int = 99,
    ) -> None:
        self._results = {
            "Page.getFrameTree": {
                "frameTree": {
                    "frame": {"id": "MAIN", "url": "https://mangadot.net/"},
                    "childFrames": [
                        {"frame": {"id": frame_id, "url": frame_url}},
                    ],
                }
            },
            "DOM.getFrameOwner": {"backendNodeId": backend_node_id},
            "DOM.getBoxModel": {"model": {"content": content_quad, "height": height}},
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


class FakeDomWs:
    """Mock CDP socket with NO Cloudflare OOPIF — only a main-frame DOM rect.

    Page.getFrameTree resolves to ``{}`` (no cf child frame), so the OOPIF path
    yields None and the SECONDARY main-frame DOM.querySelector path runs.
    """

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


# ── PRIMARY: OOPIF frame-owner path ──────────────────────────────────────────


def test_oopif_is_layout_driven_not_hardcoded() -> None:
    mangadot = locate_checkbox(
        FakeOopifWs(content_quad=_MANGADOT_IFRAME_QUAD, height=_MANGADOT_IFRAME_HEIGHT),
        x_scale=2.0,
        y_scale=2.586,
    )
    kagane = locate_checkbox(
        FakeOopifWs(content_quad=_KAGANE_IFRAME_QUAD, height=_KAGANE_IFRAME_HEIGHT),
        x_scale=2.0,
        y_scale=2.586,
    )
    # Distinct iframe layouts → distinct physical coordinates (not hardcoded).
    assert mangadot is not None
    assert kagane is not None
    assert mangadot != kagane


def test_oopif_dual_axis_scaling_matches_formula() -> None:
    x_scale, y_scale = 2.0, 2.586
    coords = locate_checkbox(
        FakeOopifWs(content_quad=_MANGADOT_IFRAME_QUAD, height=_MANGADOT_IFRAME_HEIGHT),
        x_scale=x_scale,
        y_scale=y_scale,
    )
    expected = _expected_oopif_point(
        _MANGADOT_IFRAME_QUAD, _MANGADOT_IFRAME_HEIGHT, x_scale, y_scale
    )
    assert coords == expected
    # Live-cleared neighborhood: cssx 46, cssy 390.5 → ~(92, 1010).
    assert coords == (92, 1010)


def test_oopif_returns_none_without_cf_frame() -> None:
    # Child frame URL is NOT challenges.cloudflare.com ⇒ OOPIF path yields None,
    # and with no main-frame DOM doc either ⇒ overall None.
    ws = FakeOopifWs(
        content_quad=_MANGADOT_IFRAME_QUAD,
        height=_MANGADOT_IFRAME_HEIGHT,
        frame_url="https://mangadot.net/sub",
    )
    assert locate_checkbox(ws) is None


# ── SECONDARY: main-frame DOM path (no OOPIF present) ─────────────────────────


def test_main_frame_dom_is_layout_driven() -> None:
    mangadot = locate_checkbox(FakeDomWs(_MANGADOT_DOM_QUAD))
    kagane = locate_checkbox(FakeDomWs(_KAGANE_DOM_QUAD))
    assert mangadot == (73, 977)
    assert kagane == (230, 320)
    assert mangadot != kagane


def test_main_frame_dom_dual_axis_scaling() -> None:
    coords = locate_checkbox(FakeDomWs(_MANGADOT_DOM_QUAD), x_scale=2.0, y_scale=3.0)
    # center (73, 977) → (146, 2931)
    assert coords == (146, 2931)


def test_main_frame_skips_unmatched_selector() -> None:
    # querySelector returns 0 (no match) for every selector ⇒ DOM path yields None.
    ws = FakeDomWs(_MANGADOT_DOM_QUAD, match_node=0)
    assert locate_checkbox(ws) is None


# ── TERTIARY: screenshot fallback + nothing-located ──────────────────────────


def test_locate_checkbox_falls_back_to_screenshot() -> None:
    calls: list[bytes] = []

    def fake_locator(png: bytes) -> tuple[int, int]:
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
