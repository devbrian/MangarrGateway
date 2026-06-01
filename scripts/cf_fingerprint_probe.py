"""Cloudflare fingerprint probe — datacenter-IP Chromium clearance investigation.

WHY: bare headless Patchright/Chromium is blocked by Comix's Cloudflare encrypted
challenge on a DATACENTER IP (confirmed on the GitHub Actions ubuntu-latest runner;
the #35 / comix-parallel-engine-probe finding). Camoufox/Firefox clears CF on the
SAME datacenter IP, which isolates the block to Chromium's *fingerprint* (CF detects
headless Chrome at the binary level and scrutinises datacenter ASNs harder).

This probe runs SEVERAL Chromium launch configs against comix.to in one CI run and
reports, per config, whether a ``cf_clearance`` cookie is obtained within the
budget. The goal is to find the minimal CLEAN config (real Chrome channel +
headed-under-Xvfb is the leading hypothesis) that clears CF on a datacenter IP, so
Patchright can stay the single default that also runs the parallel path in CI.

EXPERIMENTAL investigation tooling (not application src). Run under Xvfb so the
headed configs have a display:

    xvfb-run -a uv run python scripts/cf_fingerprint_probe.py

Exit code 0 if ANY config cleared CF, 1 if none did.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from patchright.async_api import async_playwright

CHALLENGE_URL = "https://comix.to/"
CLEAR_TIMEOUT_S = 45.0  # budget to obtain cf_clearance per config
GOTO_TIMEOUT_MS = 45_000
POLL_S = 0.5
PACE_S = 4.0  # gap between configs to avoid stacking CF hits

# Each config varies the two levers the research points at: the browser binary
# (bundled Chromium vs real Google Chrome channel) and headless vs headed. Headed
# configs rely on the Xvfb display the workflow provides.
CONFIGS: list[dict[str, object]] = [
    {"name": "chromium-headless", "channel": None, "headless": True},
    {"name": "chromium-headed-xvfb", "channel": None, "headless": False},
    {"name": "chrome-headless", "channel": "chrome", "headless": True},
    {"name": "chrome-headed-xvfb", "channel": "chrome", "headless": False},
]


async def _probe(cfg: dict[str, object], idx: int) -> dict[str, object]:
    user_data_dir = f"/tmp/cf-probe-{idx}"
    started = time.monotonic()
    out: dict[str, object] = {
        "name": cfg["name"],
        "channel": cfg["channel"],
        "headless": cfg["headless"],
    }
    try:
        async with async_playwright() as pw:
            launch_kwargs: dict[str, object] = {
                "headless": cfg["headless"],
                "no_viewport": True,
            }
            if cfg["channel"]:
                launch_kwargs["channel"] = cfg["channel"]
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir, **launch_kwargs
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(
                    CHALLENGE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS
                )
                deadline = time.monotonic() + CLEAR_TIMEOUT_S
                cleared = False
                while time.monotonic() < deadline:
                    cookies = await ctx.cookies()
                    if any(
                        c.get("name") == "cf_clearance" and c.get("value")
                        for c in cookies
                    ):
                        cleared = True
                        break
                    await asyncio.sleep(POLL_S)
                try:
                    title = (await page.title())[:70]
                except Exception:  # noqa: BLE001 - title is best-effort diagnostics
                    title = "<title unavailable>"
                out.update(
                    cleared=cleared,
                    seconds=round(time.monotonic() - started, 2),
                    title=title,
                )
            finally:
                await ctx.close()
    except Exception as exc:  # noqa: BLE001 - probe records the failure, never raises
        out.update(
            cleared=False,
            seconds=round(time.monotonic() - started, 2),
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
    return out


# A real comix /title page + its chapter-list selector — the "resolving" op the
# perf budget (test_comix_perf_multi_live) measures. Used by the timing probe to
# separate one-time cold cost (launch + warm + first nav) from steady-state navs.
COMIX_TITLE_URL = "https://comix.to/title/mr3m0-the-forgotten-field"
CHAPTER_LIST_SELECTOR = "a.mchap-row__primary"


async def _timing_probe() -> dict[str, object]:
    """On chrome-headed-xvfb, split cold cost from steady-state nav cost.

    Times: (1) launch + cold warm (cf_clearance), then (2) three SEQUENTIAL navs
    to a comix /title page waiting for the chapter-list selector — mirroring the
    per-chapter "resolving" browser op the perf budget measures. If nav 1 >> navs
    2/3, the perf overage is one-time browser/Xvfb startup (a budget concern); if
    all navs are large and ~equal, headed comix resolving is inherently slow.
    """
    out: dict[str, object] = {"config": "chrome-headed-xvfb"}
    async with async_playwright() as pw:
        t0 = time.monotonic()
        ctx = await pw.chromium.launch_persistent_context(
            "/tmp/cf-timing", channel="chrome", headless=False, no_viewport=True
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(CHALLENGE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
            deadline = time.monotonic() + CLEAR_TIMEOUT_S
            while time.monotonic() < deadline:
                cookies = await ctx.cookies()
                if any(
                    c.get("name") == "cf_clearance" and c.get("value") for c in cookies
                ):
                    break
                await asyncio.sleep(POLL_S)
            out["launch_plus_warm_s"] = round(time.monotonic() - t0, 2)
            navs: list[object] = []
            for _ in range(3):
                t_nav = time.monotonic()
                try:
                    p = await ctx.new_page()
                    await p.goto(
                        COMIX_TITLE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS
                    )
                    await p.wait_for_selector(
                        CHAPTER_LIST_SELECTOR, timeout=GOTO_TIMEOUT_MS
                    )
                    navs.append(round(time.monotonic() - t_nav, 2))
                    await p.close()
                except Exception as exc:  # noqa: BLE001
                    navs.append(f"ERR {type(exc).__name__}: {exc}"[:120])
                await asyncio.sleep(2)
            out["title_resolve_navs_s"] = navs
        finally:
            await ctx.close()
    return out


async def main() -> None:
    print(f"[probe] DISPLAY={os.environ.get('DISPLAY', '<unset>')}", flush=True)
    print(
        "[probe] timing run (chrome-headed-xvfb): cold cost vs steady-state navs",
        flush=True,
    )
    result = await _timing_probe()
    print("\n==== HEADED TIMING PROBE (JSON) ====", flush=True)
    print(json.dumps(result, indent=2), flush=True)
    navs = result.get("title_resolve_navs_s")
    ok = isinstance(navs, list) and any(isinstance(n, (int, float)) for n in navs)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
