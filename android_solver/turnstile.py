"""Dynamic Turnstile-checkbox locator (returns hardware-tap coordinates).

LOCKED REQUIREMENT (10-CONTEXT <decisions>): the checkbox is located
DYNAMICALLY, never by a hardcoded coordinate. A literal ``(73, 977)`` cleared
mangadot.net but MISSED kagane.to — their challenge-page layouts differ — so the
tap point MUST be derived from the live page.

Mechanisms (in order):
  1. PRIMARY — CDP OOPIF frame-owner rect. The real Cloudflare "Just a moment…"
     managed challenge renders the clickable checkbox inside a CROSS-ORIGIN
     out-of-process iframe (OOPIF) whose URL contains ``challenges.cloudflare.com``.
     The MAIN frame's ``DOM.querySelector`` only ever finds the HIDDEN
     ``<input name="cf-turnstile-response">`` (box model = None) and
     ``DOM.querySelectorAll(root,"iframe")`` returns 0, so the main-frame selector
     path can NEVER locate the widget. Instead we: ``Page.getFrameTree`` → find
     the child frame whose ``frame.url`` contains ``challenges.cloudflare.com`` →
     ``DOM.getFrameOwner`` for its ``backendNodeId`` → ``DOM.getBoxModel`` for the
     iframe's content quad. The checkbox sits ~``CHECKBOX_X_OFFSET_CSS`` CSS px from
     the iframe's LEFT edge, vertically centered. Layout-driven, so mangadot and
     kagane each get their own coordinates.
  2. SECONDARY — main-frame DOM rect: ``DOM.getDocument`` → ``DOM.querySelector``
     for a ``cf-chl-widget`` / Turnstile host element → ``DOM.getBoxModel`` content
     quad CENTER. Kept for layouts that DO expose the widget in the main frame;
     used only when the OOPIF path returns ``None``.
  3. TERTIARY — screenshot: locate the widget over ``AdbDevice.screencap()`` PNG
     bytes when both DOM paths yield nothing.

Scaling — CDP box-model coordinates are CSS/LAYOUT pixels, but
``adb shell input tap`` needs PHYSICAL SCREEN pixels. The factor is NOT a single
``device_pixel_ratio``: it differs PER AXIS because webview_shell's URL bar
shrinks the vertical viewport. Verified live: screen 720x1280,
``window.innerWidth`` 360, ``window.innerHeight`` 495 → ``x_scale`` 2.000,
``y_scale`` 2.586. We therefore scale ``cssx`` by ``x_scale`` and ``cssy`` by
``y_scale`` independently; the caller computes the two scales from the physical
screen size and the live viewport.

R1: imports only the sibling ``cdp`` helper + stdlib — nothing from ``src/``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from android_solver.cdp import WebSocketLike, cdp_call

_log = logging.getLogger("android_solver.turnstile")

# Cloudflare-managed-challenge OOPIF host: the clickable checkbox lives in the
# child frame whose URL contains this substring.
_CF_FRAME_URL_MARKER = "challenges.cloudflare.com"

# CSS px from the OOPIF iframe's LEFT edge to the checkbox CENTER. The Turnstile
# checkbox is left-aligned inside the widget iframe (verified live: a 16→316 CSS
# content quad put the box ~30 px in). Vertical center is height/2.
CHECKBOX_X_OFFSET_CSS = 30

# Candidate selectors for a MAIN-FRAME Turnstile host element (secondary path).
# The first match wins; ordering goes from the most specific Cloudflare widget id
# to the generic Turnstile container.
_TURNSTILE_SELECTORS: tuple[str, ...] = (
    "[id^='cf-chl-widget']",
    ".cf-turnstile",
    "#challenge-stage",
    "div[class*='turnstile']",
)

# Command-id offset so the secondary main-frame path never collides with the
# OOPIF path's ids on the shared socket.
_SECONDARY_CMD_OFFSET = 50

Coordinate = tuple[int, int]
ScreenshotLocator = Callable[[bytes], Coordinate | None]


def locate_checkbox(
    ws: WebSocketLike | None = None,
    *,
    screencap: bytes | None = None,
    screenshot_locator: ScreenshotLocator | None = None,
    x_scale: float = 1.0,
    y_scale: float = 1.0,
    checkbox_x_offset: int = CHECKBOX_X_OFFSET_CSS,
    selectors: tuple[str, ...] = _TURNSTILE_SELECTORS,
    base_command_id: int = 100,
) -> Coordinate | None:
    """Locate the Turnstile checkbox; return ``(x, y)`` PHYSICAL-pixel tap coords.

    PRIMARY path reads the cross-origin OOPIF frame-owner rect (layout-driven);
    SECONDARY path reads the main-frame DOM rect; both scale CSS px → physical px
    by the per-axis ``x_scale`` / ``y_scale``. If both DOM paths find nothing and
    ``screencap`` bytes are supplied, falls back to the screenshot locator.
    Returns ``None`` when no path locates the widget.
    """
    if ws is not None:
        coords = _locate_via_oopif_frame(
            ws,
            x_scale=x_scale,
            y_scale=y_scale,
            checkbox_x_offset=checkbox_x_offset,
            base_command_id=base_command_id,
        )
        if coords is not None:
            _log.info("located turnstile checkbox via OOPIF frame at %s", coords)
            return coords

        coords = _locate_via_cdp_dom(
            ws,
            selectors=selectors,
            x_scale=x_scale,
            y_scale=y_scale,
            base_command_id=base_command_id + _SECONDARY_CMD_OFFSET,
        )
        if coords is not None:
            _log.info("located turnstile checkbox via main-frame DOM at %s", coords)
            return coords

    if screencap is not None:
        locator = screenshot_locator or _default_screenshot_locator
        coords = locator(screencap)
        if coords is not None:
            _log.info("located turnstile checkbox via screenshot at %s", coords)
            return coords

    _log.warning("turnstile checkbox not located (OOPIF + DOM + screenshot empty)")
    return None


def _locate_via_oopif_frame(
    ws: WebSocketLike,
    *,
    x_scale: float,
    y_scale: float,
    checkbox_x_offset: int,
    base_command_id: int,
) -> Coordinate | None:
    """PRIMARY: locate the checkbox inside the cross-origin Cloudflare OOPIF.

    Walks the frame tree for the ``challenges.cloudflare.com`` child frame, reads
    its iframe owner box model, and offsets to the checkbox center. Returns the
    PHYSICAL-pixel tap point, or ``None`` if no cf frame / no box is present.
    """
    cmd = base_command_id
    tree = cdp_call(ws, "Page.getFrameTree", command_id=cmd)
    frame_id = _find_cf_frame_id(tree.get("frameTree"))
    if not frame_id:
        return None

    cmd += 1
    owner = cdp_call(ws, "DOM.getFrameOwner", {"frameId": frame_id}, command_id=cmd)
    backend_node_id = owner.get("backendNodeId")
    if backend_node_id is None:
        return None

    cmd += 1
    box = cdp_call(
        ws,
        "DOM.getBoxModel",
        {"backendNodeId": backend_node_id},
        command_id=cmd,
    )
    model = box.get("model", {})
    return _scaled_checkbox_point(
        model.get("content"),
        model.get("height"),
        x_scale=x_scale,
        y_scale=y_scale,
        checkbox_x_offset=checkbox_x_offset,
    )


def _find_cf_frame_id(frame_tree: Any) -> str | None:
    """DFS the CDP frame tree for the ``challenges.cloudflare.com`` frame id."""
    if not isinstance(frame_tree, dict):
        return None
    frame = frame_tree.get("frame", {})
    url = frame.get("url", "") if isinstance(frame, dict) else ""
    if isinstance(url, str) and _CF_FRAME_URL_MARKER in url:
        frame_id = frame.get("id")
        if frame_id:
            return str(frame_id)
    for child in frame_tree.get("childFrames", []) or []:
        found = _find_cf_frame_id(child)
        if found is not None:
            return found
    return None


def _scaled_checkbox_point(
    quad: Any,
    height: Any,
    *,
    x_scale: float,
    y_scale: float,
    checkbox_x_offset: int,
) -> Coordinate | None:
    """Checkbox center from the OOPIF content quad → physical px (dual-axis).

    The checkbox sits ``checkbox_x_offset`` CSS px from the iframe LEFT edge,
    vertically centered (top + height/2). CSS px are scaled independently per
    axis (URL-bar-shrunk viewport ⇒ x_scale ≠ y_scale).
    """
    if not isinstance(quad, (list, tuple)) or len(quad) < 8:
        return None
    if not isinstance(height, (int, float)):
        return None
    xs = [float(quad[i]) for i in range(0, 8, 2)]
    ys = [float(quad[i]) for i in range(1, 8, 2)]
    left = min(xs)
    top = min(ys)
    cssx = left + float(checkbox_x_offset)
    cssy = top + float(height) / 2.0
    return (round(cssx * x_scale), round(cssy * y_scale))


def _locate_via_cdp_dom(
    ws: WebSocketLike,
    *,
    selectors: tuple[str, ...],
    x_scale: float,
    y_scale: float,
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
        center = _quad_center(quad, x_scale=x_scale, y_scale=y_scale)
        if center is not None:
            return center
    return None


def _quad_center(quad: Any, *, x_scale: float, y_scale: float) -> Coordinate | None:
    """Center of a CDP content quad (8 numbers: 4 (x, y) corners) → physical px.

    Dual-axis scaled (cx by x_scale, cy by y_scale) — same URL-bar-shrunk-viewport
    rationale as the OOPIF path.
    """
    if not isinstance(quad, (list, tuple)) or len(quad) < 8:
        return None
    xs = [float(quad[i]) for i in range(0, 8, 2)]
    ys = [float(quad[i]) for i in range(1, 8, 2)]
    cx = (sum(xs) / 4.0) * x_scale
    cy = (sum(ys) / 4.0) * y_scale
    return (round(cx), round(cy))


def _default_screenshot_locator(_png: bytes) -> Coordinate | None:
    """Placeholder screenshot fallback.

    A real image-based locate (template/contrast match over the PNG) is wired in
    a later plan when a fixture screenshot is available. Returning ``None`` keeps
    the DOM paths authoritative; callers may inject their own locator.
    """
    return None
