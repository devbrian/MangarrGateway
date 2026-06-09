#!/usr/bin/env python3
"""Automated kagane.to recon — headed Patchright, clear CF, capture API shapes.

Throwaway recon tool (NOT shipped, NOT tested). Launches a headed Patchright
Chromium, waits out the Cloudflare managed challenge, records every network
request/response, dumps the JS bundle for route-map grepping, and then drives
the SPA programmatically (homepage feed + a search) to surface the real
search / chapter-list / manifest / image endpoint shapes.

Output → ``_recon_out_kagane/`` (gitignored).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

OUT_DIR = Path("_recon_out_kagane")
JS_DIR = OUT_DIR / "js"
NETWORK_LOG = OUT_DIR / "network.jsonl"
SNAPSHOT = OUT_DIR / "snapshot.json"
USER_DATA = OUT_DIR / "userdata"

START_URL = "https://kagane.to"
SEARCH_TERMS = ["one piece", "naruto"]

JS_CT = ("application/javascript", "text/javascript", "application/x-javascript")
TEXT_CT = ("application/json", "text/", "application/xml") + JS_CT


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def slugify(s: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:n] or "_"


async def wait_for_clear(page, timeout_s: float = 90.0) -> bool:
    """Poll until the Cloudflare interstitial is gone (title != 'Just a moment')."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            title = await page.title()
        except Exception:
            title = ""
        if title and "just a moment" not in title.lower():
            # Give the SPA a beat to hydrate.
            await asyncio.sleep(3)
            return True
        await asyncio.sleep(1.5)
    return False


async def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    JS_DIR.mkdir(exist_ok=True)

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        print("ERROR: patchright not installed. Run: uv sync", file=sys.stderr)
        return 1

    net_f = NETWORK_LOG.open("w", encoding="utf-8")
    seen_js: set[str] = set()

    def _write_line(rec: dict) -> None:
        net_f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        net_f.flush()

    async def log(rec: dict) -> None:
        # Offload the blocking write/flush so a high response volume can't stall
        # the event loop (and skew recon capture).
        if not net_f.closed:
            await asyncio.to_thread(_write_line, rec)

    print(f"[recon] launching headed Chromium at {START_URL}")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=False,
            viewport={"width": 1366, "height": 950},
        )

        async def on_response(resp) -> None:
            try:
                req = resp.request
                ct = (resp.headers.get("content-type") or "").lower()
                rec: dict = {
                    "t": utcnow(),
                    "kind": "response",
                    "url": resp.url,
                    "status": resp.status,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "content_type": ct,
                }
                if any(ct.startswith(c) for c in JS_CT) and resp.url not in seen_js:
                    seen_js.add(resp.url)
                    try:
                        text = await resp.text()
                        path = JS_DIR / (slugify(resp.url.rsplit("/", 1)[-1]) + ".js")
                        path.write_text(text, encoding="utf-8", errors="replace")
                        rec["js_saved"] = str(path)
                    except Exception as e:
                        rec["js_error"] = str(e)
                elif "application/json" in ct or (
                    req.resource_type in ("fetch", "xhr")
                ):
                    try:
                        body = await resp.text()
                        rec["body_text"] = body[:20_000]
                        rec["body_size"] = len(body)
                    except Exception as e:
                        rec["body_error"] = str(e)
                await log(rec)
            except Exception as e:
                await log({"t": utcnow(), "kind": "response_error", "err": str(e)})

        # Track spawned handlers so we can drain them before closing net_f /
        # the context — otherwise late-completing tasks drop their final log
        # lines (log() is gated on `not net_f.closed`).
        response_tasks: set[asyncio.Task] = set()

        def _spawn_on_response(r) -> None:
            task = asyncio.create_task(on_response(r))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        context.on("response", _spawn_on_response)

        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"[recon] initial goto error: {e}")

        print("[recon] waiting for Cloudflare challenge to clear...")
        cleared = await wait_for_clear(page)
        print(f"[recon] cleared={cleared} title={await page.title()!r}")

        cookies = await context.cookies()
        cf = [c["name"] for c in cookies if "cf" in c["name"].lower()]
        print(f"[recon] cookies={[c['name'] for c in cookies]} cf-ish={cf}")

        # Let homepage XHRs settle (popular/latest feed reveals the manga API).
        await asyncio.sleep(4)

        # Drive a search via the SPA's own client by probing candidate endpoints
        # from the page context (same-origin → carries cf_clearance).
        probe_results: dict = {}
        probe_paths = [
            "/api/v1/series?search=one+piece",
            "/api/v1/series?q=one+piece",
            "/api/series?search=one+piece",
            "/api/v1/manga?search=one+piece",
            "/api/v1/series",
            "/api/v1/series/latest",
            "/api/v1/series/recent",
            "/api/series",
            "/api/v1/book",
            "/api/v1/books",
            "/sitemap.xml",
        ]
        for p in probe_paths:
            try:
                res = await page.evaluate(
                    """async (path) => {
                        try {
                            const r = await fetch(path, {headers: {accept: 'application/json'}});
                            const txt = await r.text();
                            return {status: r.status, ct: r.headers.get('content-type'),
                                    body: txt.slice(0, 4000)};
                        } catch (e) { return {error: String(e)}; }
                    }""",
                    p,
                )
            except Exception as e:
                res = {"error": str(e)}
            probe_results[p] = res
            print(f"[probe] {p} -> {res.get('status')} {str(res.get('ct'))[:40]}")

        # Also try navigating the UI to a search results page to capture real XHRs.
        for term in SEARCH_TERMS:
            for route in (f"/search?q={term.replace(' ', '+')}", f"/search?query={term.replace(' ', '+')}"):
                try:
                    await page.goto(START_URL + route, wait_until="networkidle", timeout=30_000)
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"[recon] nav {route} error: {e}")

        snapshot = {
            "t": utcnow(),
            "cleared": cleared,
            "url": page.url,
            "title": await page.title(),
            "cookies": [c["name"] for c in cookies],
            "ua": await page.evaluate("navigator.userAgent"),
            "probe_results": probe_results,
            "home_html_head": (await page.content())[:8000],
        }
        SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))

        # Drain in-flight response handlers so their final log lines land before
        # we close net_f / the context.
        if response_tasks:
            await asyncio.wait(set(response_tasks), timeout=10)
        await context.close()

    net_f.close()
    print(f"[recon] network log → {NETWORK_LOG}")
    print(f"[recon] JS files     → {JS_DIR} ({len(seen_js)})")
    print(f"[recon] snapshot     → {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
