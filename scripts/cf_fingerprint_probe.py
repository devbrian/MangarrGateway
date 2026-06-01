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
            ctx = await pw.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(CHALLENGE_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
                deadline = time.monotonic() + CLEAR_TIMEOUT_S
                cleared = False
                while time.monotonic() < deadline:
                    cookies = await ctx.cookies()
                    if any(c.get("name") == "cf_clearance" and c.get("value") for c in cookies):
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


async def main() -> None:
    print(f"[probe] DISPLAY={os.environ.get('DISPLAY', '<unset>')}", flush=True)
    results: list[dict[str, object]] = []
    for idx, cfg in enumerate(CONFIGS):
        print(f"[probe] {cfg['name']} (channel={cfg['channel']} headless={cfg['headless']}) ...", flush=True)
        r = await _probe(cfg, idx)
        results.append(r)
        verdict = "PASS" if r.get("cleared") else "FAIL"
        print(
            f"  -> {verdict} {r.get('seconds')}s  {r.get('error') or 'title=' + str(r.get('title', ''))}",
            flush=True,
        )
        await asyncio.sleep(PACE_S)

    print("\n==== CF FINGERPRINT PROBE RESULTS (JSON) ====", flush=True)
    print(json.dumps(results, indent=2), flush=True)
    print("\n==== SUMMARY ====", flush=True)
    for r in results:
        verdict = "PASS" if r.get("cleared") else "FAIL"
        print(f"  {verdict}  {str(r['name']):24} {r.get('seconds')}s  {r.get('error', '')}", flush=True)

    any_cleared = any(r.get("cleared") for r in results)
    print(f"\n[probe] any config cleared CF: {any_cleared}", flush=True)
    sys.exit(0 if any_cleared else 1)


if __name__ == "__main__":
    asyncio.run(main())
