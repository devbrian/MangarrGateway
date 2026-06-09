"""Dynamic Turnstile-checkbox locator (returns hardware-tap coordinates).

LOCKED REQUIREMENT (10-CONTEXT <decisions>): the checkbox is located
DYNAMICALLY, never by a hardcoded coordinate. A literal ``(73, 977)`` cleared
mangadot.net but MISSED kagane.to — their challenge-page layouts differ — so the
tap point MUST be derived from the live page.

Mechanisms (in order):
  1. PRIMARY — CDP DOM rect: ``DOM.getDocument`` → ``DOM.querySelector`` for the
     ``cf-chl-widget`` / Turnstile host element → ``DOM.getBoxModel``; the content
     quad's CENTER is the tap point. Layout-driven, so mangadot and kagane each
     get their own coordinates.
  2. FALLBACK — screenshot: locate the widget over ``AdbDevice.screencap()`` PNG
     bytes when the DOM path yields nothing (closed shadow root / detached host).

Coordinates are returned in DEVICE pixels (CDP CSS pixels scaled by
``device_pixel_ratio``) so they feed ``AdbDevice.input_tap`` directly.

R1: imports only the sibling ``cdp`` helper + stdlib — nothing from ``src/``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from android_solver.cdp import WebSocketLike, cdp_call

_log = logging.getLogger("android_solver.turnstile")

# Candidate selectors for the managed-Turnstile host element. The first match
# wins; ordering goes from the most specific Cloudflare widget id to the generic
# Turnstile container so BOTH mangadot's and kagane's markup resolve.
_TURNSTILE_SELECTORS: tuple[str, ...] = (
    "[id^='cf-chl-widget']",
    ".cf-turnstile",
    "#challenge-stage",
    "div[class*='turnstile']",
)

Coordinate = tuple[int, int]
ScreenshotLocator = Callable[[bytes], Coordinate | None]


def locate_checkbox(
    ws: WebSocketLike | None = None,
    *,
    screencap: bytes | None = None,
    screenshot_locator: ScreenshotLocator | None = None,
    device_pixel_ratio: float = 1.0,
    selectors: tuple[str, ...] = _TURNSTILE_SELECTORS,
    base_command_id: int = 100,
) -> Coordinate | None:
    """Locate the Turnstile checkbox; return ``(x, y)`` device-pixel tap coords.

    PRIMARY path uses the live ``ws`` CDP DOM rect (layout-driven). If that finds
    nothing and ``screencap`` bytes are supplied, falls back to the screenshot
    locator. Returns ``None`` when neither path locates the widget.
    """
    if ws is not None:
        coords = _locate_via_cdp_dom(
            ws,
            selectors=selectors,
            device_pixel_ratio=device_pixel_ratio,
            base_command_id=base_command_id,
        )
        if coords is not None:
            _log.info("located turnstile checkbox via CDP DOM at %s", coords)
            return coords

    if screencap is not None:
        locator = screenshot_locator or _default_screenshot_locator
        coords = locator(screencap)
        if coords is not None:
            _log.info("located turnstile checkbox via screenshot at %s", coords)
            return coords

    _log.warning("turnstile checkbox not located (DOM + screenshot both empty)")
    return None


def _locate_via_cdp_dom(
    ws: WebSocketLike,
    *,
    selectors: tuple[str, ...],
    device_pixel_ratio: float,
    base_command_id: int,
) -> Coordinate | None:
    document = cdp_call(ws, "DOM.getDocument", {"depth": 0}, command_id=base_command_id)
    root_id = document.get("root", {}).get("nodeId")
    if root_id is None:
        return None

    cmd = base_command_id
    for selector in selectors:
        cmd += 1
        found = cdp_call(
            ws,
            "DOM.querySelector",
            {"nodeId": root_id, "selector": selector},
            command_id=cmd,
        )
        node_id = found.get("nodeId")
        if not node_id:  # 0 or None ⇒ no match for this selector
            continue

        cmd += 1
        box = cdp_call(
            ws,
            "DOM.getBoxModel",
            {"nodeId": node_id},
            command_id=cmd,
        )
        quad = box.get("model", {}).get("content")
        center = _quad_center(quad, device_pixel_ratio)
        if center is not None:
            return center
    return None


def _quad_center(quad: Any, device_pixel_ratio: float) -> Coordinate | None:
    """Center of a CDP content quad (8 numbers: 4 (x, y) corners) → device px."""
    if not isinstance(quad, (list, tuple)) or len(quad) < 8:
        return None
    xs = [float(quad[i]) for i in range(0, 8, 2)]
    ys = [float(quad[i]) for i in range(1, 8, 2)]
    cx = (sum(xs) / 4.0) * device_pixel_ratio
    cy = (sum(ys) / 4.0) * device_pixel_ratio
    return (round(cx), round(cy))


def _default_screenshot_locator(_png: bytes) -> Coordinate | None:
    """Placeholder screenshot fallback.

    A real image-based locate (template/contrast match over the PNG) is wired in
    a later plan when a fixture screenshot is available. Returning ``None`` keeps
    the CDP DOM path authoritative; callers may inject their own locator.
    """
    return None
