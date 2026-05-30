#!/usr/bin/env python3
"""One-shot comix.to recon — drive headed Patchright, capture endpoints + JS + cipher pair.

Usage:
    uv run python scripts/comix_recon.py

You drive the browser; the script records every HTTP request, snapshots every
loaded JS file, and captures the final DOM/cookies on exit. Output lands in
``_recon_out/`` (gitignored). Used to pin RESEARCH Open Q1/Q3 and the D-44
ciphertext+plaintext fixture pair before re-running tests/live/test_comix_live.py.

This is a throwaway recon tool — NOT part of the package, NOT shipped, NOT tested.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT_DIR = Path("_recon_out")
JS_DIR = OUT_DIR / "js"
RAW_DIR = OUT_DIR / "raw"
NETWORK_LOG = OUT_DIR / "network.jsonl"
SNAPSHOT = OUT_DIR / "snapshot.json"
USER_DATA = OUT_DIR / "userdata"

START_URL = "https://comix.to"

SKIP_BODY_PREFIXES = ("image/", "video/", "audio/", "font/")
JS_CT = ("application/javascript", "text/javascript", "application/x-javascript")
TEXT_CT = ("application/json", "text/", "application/xml") + JS_CT
RAW_HINTS = ("chapter", "page", "decrypt", "image-list", "manifest")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(s: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:n] or "_"


def hashed_name(url: str, suffix: str) -> str:
    h = hashlib.sha256(url.encode()).hexdigest()[:12]
    tail = slugify(url.rsplit("/", 1)[-1].split("?", 1)[0]) or "root"
    return f"{h}-{tail}{suffix}"


async def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    JS_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        print("ERROR: patchright not installed. Run: uv sync", file=sys.stderr)
        return 1

    # Belt-and-suspenders cleanup: an atexit hook closes the network log on
    # every exit path (normal return, exception in the async block,
    # KeyboardInterrupt during the manual browse window). Background
    # request/response handlers that fire late hit the `closed` guard in log()
    # so writes-after-close are silently dropped instead of raising.
    network_log_f = NETWORK_LOG.open("w", encoding="utf-8")
    atexit.register(lambda f=network_log_f: f.close() if not f.closed else None)
    seen_js: set[str] = set()
    saved_raw: set[str] = set()
    request_count = 0

    def log(rec: dict) -> None:
        nonlocal request_count
        request_count += 1
        if network_log_f.closed:
            return
        network_log_f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        network_log_f.flush()

    print(f"[recon] launching Patchright headed Chromium at {START_URL}")
    print(f"[recon] persistent context: {USER_DATA}")
    print(f"[recon] output dir:         {OUT_DIR}")
    print()

    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA),
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
        except Exception as e:
            print(f"ERROR: Patchright launch failed: {e}", file=sys.stderr)
            print(
                "If the browser binary is missing, run: "
                "uv run patchright install chromium",
                file=sys.stderr,
            )
            return 2

        async def on_request(req) -> None:
            try:
                log(
                    {
                        "t": utcnow(),
                        "kind": "request",
                        "url": req.url,
                        "method": req.method,
                        "resource_type": req.resource_type,
                        "headers": dict(req.headers),
                        "post_data": req.post_data,
                    }
                )
            except Exception as e:
                log({"t": utcnow(), "kind": "request_error", "err": str(e), "url": getattr(req, "url", "?")})

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
                    "headers": dict(resp.headers),
                }

                # Snapshot every JS file to disk so we can grep for the decrypt routine.
                if any(ct.startswith(c) for c in JS_CT) and resp.url not in seen_js:
                    seen_js.add(resp.url)
                    try:
                        text = await resp.text()
                        path = JS_DIR / hashed_name(resp.url, ".js")
                        path.write_text(text, encoding="utf-8", errors="replace")
                        rec["js_saved"] = str(path)
                        rec["js_size"] = len(text)
                    except Exception as e:
                        rec["js_error"] = str(e)

                # Text/JSON bodies — capture inline (truncated) so the network log is readable.
                if any(ct.startswith(c) for c in TEXT_CT):
                    try:
                        body = await resp.text()
                        rec["body_text"] = body[:64_000]
                        rec["body_truncated"] = len(body) > 64_000
                        rec["body_size"] = len(body)
                    except Exception as e:
                        rec["body_error"] = str(e)
                elif not any(ct.startswith(c) for c in SKIP_BODY_PREFIXES):
                    # Non-image binary — likely encrypted page payload. Save full bytes
                    # to disk for D-44, and log the head as hex for inline grepping.
                    try:
                        body = await resp.body()
                        rec["body_size"] = len(body)
                        rec["body_b64_head_hex"] = body[:4096].hex()
                        # Save full raw bytes only when the URL or content-type smells
                        # like a chapter / page / manifest endpoint — otherwise the
                        # disk would fill with unrelated tracking pings.
                        url_l = resp.url.lower()
                        if (
                            resp.url not in saved_raw
                            and (
                                any(h in url_l for h in RAW_HINTS)
                                or ct.startswith("application/octet-stream")
                            )
                        ):
                            saved_raw.add(resp.url)
                            path = RAW_DIR / hashed_name(resp.url, ".bin")
                            path.write_bytes(body)
                            rec["raw_saved"] = str(path)
                    except Exception as e:
                        rec["body_error"] = str(e)

                log(rec)
            except Exception as e:
                log({"t": utcnow(), "kind": "response_error", "err": str(e)})

        context.on("request", lambda r: asyncio.create_task(on_request(r)))
        context.on("response", lambda r: asyncio.create_task(on_response(r)))

        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"[recon] initial goto error (continuing — you may still navigate manually): {e}")

        print("=" * 72)
        print(" BROWSE THE SITE TO TRIGGER THE ENDPOINTS WE NEED:")
        print("   1. Solve any Cloudflare challenge (just wait it out).")
        print("   2. Search for a manga (any popular title).")
        print("   3. Click a series result, then a single chapter.")
        print("   4. Wait for the chapter pages to fully render.")
        print()
        print(" When done, press Enter in THIS TERMINAL to capture & exit.")
        print("=" * 72)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input)

        snapshot: dict = {"t": utcnow(), "pages": [], "cookies": [], "ua": None}
        try:
            snapshot["cookies"] = await context.cookies()
        except Exception as e:
            snapshot["cookies_error"] = str(e)

        for p in context.pages:
            try:
                ua = await p.evaluate("navigator.userAgent")
                if snapshot["ua"] is None:
                    snapshot["ua"] = ua
                img_urls = await p.evaluate(
                    "() => Array.from(document.images).map(i => i.src).filter(Boolean)"
                )
                # Try to read any obvious loader globals.
                globals_dump = await p.evaluate(
                    """() => {
                        const keys = ['chapter','pages','manga','config','app',
                                     '__NEXT_DATA__','__NUXT__','__INITIAL_STATE__'];
                        const out = {};
                        for (const k of keys) {
                            try {
                                const v = window[k];
                                if (v === undefined) continue;
                                out[k] = JSON.parse(JSON.stringify(v));
                            } catch (e) {
                                out[k] = `__non_serializable__: ${String(e)}`;
                            }
                        }
                        return out;
                    }"""
                )
                # Dump the rendered HTML too — useful if the cipher key sits in a data-* attr.
                html = await p.content()
                html_path = OUT_DIR / f"page_{slugify(p.url, 60)}.html"
                html_path.write_text(html, encoding="utf-8", errors="replace")
                snapshot["pages"].append(
                    {
                        "url": p.url,
                        "title": await p.title(),
                        "ua": ua,
                        "image_urls": img_urls[:200],
                        "image_count": len(img_urls),
                        "html_saved": str(html_path),
                        "globals": globals_dump,
                    }
                )
            except Exception as e:
                snapshot["pages"].append({"url": getattr(p, "url", "?"), "error": str(e)})

        SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))

        await context.close()

    # Sentinel close on the happy path; the registered atexit hook below is
    # the cleanup-on-exception belt-and-suspenders so late-firing background
    # handlers never write to a leaked file (CodeRabbit nit).
    network_log_f.close()

    print()
    print(f"[recon] {request_count} network records → {NETWORK_LOG}")
    print(f"[recon] {len(seen_js)} JS files       → {JS_DIR}")
    print(f"[recon] {len(saved_raw)} raw payloads  → {RAW_DIR}")
    print(f"[recon] DOM snapshot              → {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[recon] interrupted", file=sys.stderr)
        sys.exit(130)
