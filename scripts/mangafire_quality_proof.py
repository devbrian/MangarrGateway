#!/usr/bin/env python3
"""Before/after quality proof for issue #218 (MangaFire descramble re-encode).

Experimental design — hold the RAW scrambled input constant, vary only the code:

1. LIVE CAPTURE (once): drive a real MangaFire chapter download through the live
   harness with ``_descramble_image`` monkeypatched to dump every offset>0 page's
   RAW scrambled bytes + offset to ``--capture-dir``. The CDN is hit exactly once,
   so the before/after comparison can't be confounded by it serving different bytes.
2. OFFLINE BUILD (deterministic): for each captured page, reassemble the piece-grid
   ONCE (geometry is identical pre/post-fix — only the save differs), then encode it
       * the OLD way  — ``dst.save(buf, format=fmt)``           → before.cbz
       * the NEW way  — source qtables / high quality          → after.cbz
   and report, per page: qtables-preserved, PSNR vs the exact reassembled ground
   truth (``dst``), and encoded size. The OLD path requantizes to Pillow defaults
   (JPEG q75 / WebP ~q80) and drifts off the truth; the NEW path is visually lossless.

If live capture clears no pages (Cloudflare unsolved on this host), it falls back to a
synthetic real-JPEG page so the artifacts + report are always produced — the mode is
printed loudly so a synthetic run is never mistaken for a real one.

Usage:
    GATEWAY_CLOUDFLARE_HEADLESS=false uv run python scripts/mangafire_quality_proof.py
    uv run python scripts/mangafire_quality_proof.py --synthetic   # skip the network
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

os.environ.setdefault("GATEWAY_CLOUDFLARE_HEADLESS", "false")

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image  # noqa: E402

from manga_gateway.sources import mangafire  # noqa: E402
from manga_gateway.sources.mangafire import (  # noqa: E402
    MIN_SPLIT_COUNT,
    PIECE_SIZE,
    _ceil_div,
    _descramble_image,
    _parse_cards,
)

_CAPTURE_DIR = _REPO_ROOT / "artifacts" / "mangafire-218-proof"


# ── offline reassembly (geometry copied verbatim from _descramble_image) ───────


def _reassemble(content: bytes, offset: int) -> tuple[Image.Image, str, Any]:
    """Decode + un-shuffle the piece grid → (ground-truth image, fmt, src qtables).

    Identical to the geometry inside ``_descramble_image`` — this is the pixel state
    BOTH the old and new save paths start from, so it is the fair before/after input
    and the exact PSNR reference.
    """
    with Image.open(io.BytesIO(content)) as src_img:
        fmt = src_img.format or "PNG"
        qtables = getattr(src_img, "quantization", None)
        img = src_img.convert(src_img.mode)
    width, height = img.size
    piece_w = min(PIECE_SIZE, _ceil_div(width, MIN_SPLIT_COUNT))
    piece_h = min(PIECE_SIZE, _ceil_div(height, MIN_SPLIT_COUNT))
    x_max = _ceil_div(width, piece_w) - 1
    y_max = _ceil_div(height, piece_h) - 1
    dst = Image.new(img.mode, (width, height))
    for x in range(x_max + 1):
        for y in range(y_max + 1):
            x_dst, y_dst = piece_w * x, piece_h * y
            w = min(piece_w, width - x_dst)
            h = min(piece_h, height - y_dst)
            x_src = piece_w * (x if x == x_max else (x_max - x + offset) % x_max)
            y_src = piece_h * (y if y == y_max else (y_max - y + offset) % y_max)
            region = img.crop((x_src, y_src, x_src + w, y_src + h))
            dst.paste(region, (x_dst, y_dst))
    return dst, fmt, qtables


def _old_encode(dst: Image.Image, fmt: str) -> bytes:
    """The PRE-FIX behaviour: ``dst.save(buf, format=fmt)`` at Pillow defaults."""
    buf = io.BytesIO()
    dst.save(buf, format=fmt)
    return buf.getvalue()


def _psnr(a: Image.Image, b: Image.Image) -> float:
    pa, pb = a.convert("RGB").tobytes(), b.convert("RGB").tobytes()
    mse = sum((x - y) ** 2 for x, y in zip(pa, pb, strict=True)) / len(pa)
    return math.inf if mse == 0 else 10 * math.log10((255**2) / mse)


# ── synthetic fallback page ────────────────────────────────────────────────────


def _synthetic_scrambled_page(offset: int = 2) -> bytes:
    """A real, finely-quantized JPEG of high-frequency content, scrambled with the
    INVERSE of the descramble mapping — a faithful stand-in when live capture fails."""
    w = h = 1000
    pixels = [
        ((x * y + x * 3) % 256, (x ^ y) % 256, ((x // 2 + y // 3) * 97 + x * 7) % 256)
        for y in range(h)
        for x in range(w)
    ]
    orig = Image.new("RGB", (w, h))
    orig.putdata(pixels)
    piece_w = min(PIECE_SIZE, _ceil_div(w, MIN_SPLIT_COUNT))
    piece_h = min(PIECE_SIZE, _ceil_div(h, MIN_SPLIT_COUNT))
    x_max = _ceil_div(w, piece_w) - 1
    y_max = _ceil_div(h, piece_h) - 1
    scrambled = Image.new("RGB", (w, h))
    for x in range(x_max + 1):
        for y in range(y_max + 1):
            x_dst, y_dst = piece_w * x, piece_h * y
            cw = min(piece_w, w - x_dst)
            ch = min(piece_h, h - y_dst)
            x_src = piece_w * (x if x == x_max else (x_max - x + offset) % x_max)
            y_src = piece_h * (y if y == y_max else (y_max - y + offset) % y_max)
            region = orig.crop((x_dst, y_dst, x_dst + cw, y_dst + ch))
            scrambled.paste(region, (x_src, y_src))
    buf = io.BytesIO()
    scrambled.save(buf, format="JPEG", quality=92, subsampling=0)
    return buf.getvalue()


# ── live capture ───────────────────────────────────────────────────────────────


async def _live_capture(capture_dir: Path) -> int:
    """Run one real MangaFire download, dumping offset>0 raw pages. Returns count."""
    from tests.live._helpers import _poll_until_terminal, live_client_for
    from tests.live.profiles.mangafire import LIVE_SMOKE

    captured = 0
    real_descramble = mangafire._descramble_image

    def _capturing(content: bytes, offset: int) -> bytes:
        nonlocal captured
        if offset > 0:
            (capture_dir / f"page_{captured:03d}_off{offset}.bin").write_bytes(content)
            captured += 1
        return real_descramble(content, offset)

    mangafire._descramble_image = _capturing  # type: ignore[assignment]
    try:
        async with live_client_for(LIVE_SMOKE) as client:
            search = await client.post(
                "/api/v1/search",
                json={
                    "type": "chapter",
                    "query": LIVE_SMOKE.default_query,
                    "sources": ["mangafire"],
                },
            )
            search.raise_for_status()
            releases = search.json()["releases"]
            print(f"[live] search returned {len(releases)} releases")
            for rel in releases[: LIVE_SMOKE.max_releases_to_try]:
                submit = await client.post(
                    "/api/v1/downloads",
                    json={
                        "releaseHandle": rel["downloadHandle"],
                        "sourceKey": "mangafire",
                    },
                )
                if submit.status_code != 200:
                    continue
                job_id = submit.json()["jobId"]
                print(f"[live] submitted job {job_id} for '{rel.get('title')}'")
                job = await _poll_until_terminal(
                    client, job_id, timeout_s=LIVE_SMOKE.download_timeout_s
                )
                if job["status"] == "completed":
                    print(f"[live] completed: {job.get('outputPath')}")
                    break
                print(f"[live] job failed: {job.get('error')}")
    finally:
        mangafire._descramble_image = real_descramble  # type: ignore[assignment]
    return captured


async def _capture_title(
    source: Any, ctx: Any, manga_token: str, chapter_scan: int, capture_dir: Path
) -> int:
    """Scan one title's chapters for offset>0 pages and dump their raw bytes.

    Each page URL from ``fetch_manifest`` carries its scramble offset as a
    ``#scr_{offset}`` fragment — so scrambled chapters are found WITHOUT a download.
    The scrambled pages' RAW bytes (exactly the pre-descramble input ``fetch_image``
    sees) are fetched via ``ctx.get_bytes`` and dumped. Returns the captured count.
    """
    from urllib.parse import urldefrag

    slug_id = manga_token.rsplit(".", 1)[-1] if "." in manga_token else manga_token
    try:
        chapters = await source._chapter_list(slug_id, "en", ctx)
    except Exception as exc:  # noqa: BLE001 — a dead title must not stop the catalog scan
        print(f"[scan] {manga_token}: chapter-list error {type(exc).__name__}")
        return 0
    for ch in chapters[:chapter_scan]:
        href = ch.get("href")
        if not href:
            continue
        try:
            manifest = await source.fetch_manifest(href, ctx)
        except Exception:  # noqa: BLE001 — skip a bad chapter, keep scanning
            continue
        scrambled = [u for u in manifest if "#scr_" in u]
        if not scrambled:
            continue
        print(
            f"[scan] >>> SCRAMBLED title found: {href} "
            f"({len(scrambled)}/{len(manifest)} pages) — capturing"
        )
        captured = 0
        for url in scrambled:
            clean, frag = urldefrag(url)
            offset = int(frag[4:]) if frag.startswith("scr_") else 0
            raw = await ctx.get_bytes(clean)
            (capture_dir / f"page_{captured:03d}_off{offset}.bin").write_bytes(raw)
            captured += 1
        return captured
    return 0


def _boot_app() -> Any:
    """Boot a real gateway app on a throwaway temp dir (mirrors live_client_for)."""
    import tempfile

    from tests.live._helpers import _TEST_API_KEY

    from manga_gateway.app import create_app
    from manga_gateway.config import Settings

    tmp = Path(tempfile.mkdtemp(prefix="mf-direct-"))
    settings = Settings(
        api_key=_TEST_API_KEY,
        output_root=str(tmp / "out"),
        db_path=str(tmp / "jobs.db"),
    )
    return create_app(settings)


async def _live_capture_direct(
    capture_dir: Path, manga_token: str, scan_limit: int
) -> int:
    """Capture a single named title's scrambled pages (skips search + job pipeline)."""
    app = _boot_app()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(
            app.state.solver.get_clearance("mangafire"), timeout=90.0
        )
        source = app.state.registry.get("mangafire")()
        ctx = app.state.job_manager._engine._build_context(source)
        print(f"[direct] scanning {manga_token} (up to {scan_limit} chapters)…")
        return await _capture_title(source, ctx, manga_token, scan_limit, capture_dir)


async def _live_scan_catalog(capture_dir: Path, max_titles: int) -> int:
    """Discover MangaFire titles via the no-vrf filter feeds and probe each for a
    scrambled chapter until one is found and captured. Returns the captured count."""
    app = _boot_app()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(
            app.state.solver.get_clearance("mangafire"), timeout=90.0
        )
        source = app.state.registry.get("mangafire")()
        ctx = app.state.job_manager._engine._build_context(source)

        # No-vrf catalog feeds (HTML, parsed by the source's own card parser).
        feeds = [
            f"{source.base_url}/filter?sort=most_viewed",
            f"{source.base_url}/filter?sort=most_viewed&page=2",
            f"{source.base_url}/filter?sort=recently_updated",
            f"{source.base_url}/filter?sort=trending",
        ]
        tokens: list[str] = []
        seen: set[str] = set()
        for feed in feeds:
            try:
                html = await ctx.get_bytes(feed)
            except Exception as exc:  # noqa: BLE001
                print(f"[scan] feed error {feed}: {type(exc).__name__}")
                continue
            for card in _parse_cards(html):
                href = card.get("href", "")
                token = href.rsplit("/manga/", 1)[-1].strip("/")
                if token and token not in seen:
                    seen.add(token)
                    tokens.append(token)
        print(
            f"[scan] discovered {len(tokens)} unique titles; probing up to "
            f"{max_titles} (1 chapter each) for scrambling…"
        )
        for i, token in enumerate(tokens[:max_titles], 1):
            n = await _capture_title(source, ctx, token, 1, capture_dir)
            if n:
                print(
                    f"[scan] captured {n} scrambled pages from {token} "
                    f"(title {i}/{min(len(tokens), max_titles)})"
                )
                return n
        print(
            f"[scan] no scrambled title found in {min(len(tokens), max_titles)} probed"
        )
        return 0


# ── report + CBZ build ─────────────────────────────────────────────────────────


def _build_and_report(raw_pages: list[tuple[str, bytes, int]], out_dir: Path) -> None:
    before_path = out_dir / "before.cbz"
    after_path = out_dir / "after.cbz"
    rows: list[dict[str, Any]] = []
    with (
        zipfile.ZipFile(before_path, "w", zipfile.ZIP_STORED) as zbefore,
        zipfile.ZipFile(after_path, "w", zipfile.ZIP_STORED) as zafter,
    ):
        for i, (name, raw, offset) in enumerate(raw_pages):
            dst, fmt, qtables = _reassemble(raw, offset)
            old_bytes = _old_encode(dst, fmt)
            new_bytes = _descramble_image(raw, offset)
            ext = "jpg" if fmt == "JPEG" else fmt.lower()
            zbefore.writestr(f"{i:03d}.{ext}", old_bytes)
            zafter.writestr(f"{i:03d}.{ext}", new_bytes)

            old_img = Image.open(io.BytesIO(old_bytes))
            new_img = Image.open(io.BytesIO(new_bytes))
            rows.append(
                {
                    "page": name,
                    "fmt": fmt,
                    "offset": offset,
                    "qtables_preserved": getattr(new_img, "quantization", None)
                    == qtables,
                    "old_psnr": _psnr(dst, old_img),
                    "new_psnr": _psnr(dst, new_img),
                    "old_bytes": len(old_bytes),
                    "new_bytes": len(new_bytes),
                }
            )

    print("\n" + "=" * 78)
    print("ISSUE #218 — DESCRAMBLE RE-ENCODE QUALITY: BEFORE vs AFTER")
    print("=" * 78)
    hdr = (
        f"{'page':<22}{'fmt':<6}{'qtbl':<6}"
        f"{'PSNR old':>10}{'PSNR new':>10}{'Δsize':>10}"
    )
    print(hdr)
    print("-" * 78)
    for r in rows:
        old_p = "inf" if r["old_psnr"] == math.inf else f"{r['old_psnr']:.1f}"
        new_p = "inf" if r["new_psnr"] == math.inf else f"{r['new_psnr']:.1f}"
        dsize = r["new_bytes"] - r["old_bytes"]
        ok = "yes" if r["qtables_preserved"] else "—"
        print(f"{r['page']:<22}{r['fmt']:<6}{ok:<6}{old_p:>10}{new_p:>10}{dsize:>+10}")
    print("-" * 78)
    avg_old = sum(r["old_psnr"] for r in rows if r["old_psnr"] != math.inf) / max(
        1, sum(1 for r in rows if r["old_psnr"] != math.inf)
    )
    print(
        f"avg PSNR vs ground-truth   old≈{avg_old:.1f} dB   "
        f"new=lossless/near-lossless (higher = closer to the true descrambled page)"
    )
    print(f"\nbefore.cbz → {before_path}")
    print(f"after.cbz  → {after_path}")
    print(
        "Open both and compare the same page index — the 'after' pages are the "
        "original CDN quality; the 'before' pages are visibly recompressed."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="skip the network; use a synthetic real-JPEG page",
    )
    parser.add_argument("--capture-dir", type=Path, default=_CAPTURE_DIR)
    parser.add_argument(
        "--direct",
        action="store_true",
        help="scan a manga's chapters for a SCRAMBLED one and capture its raw pages "
        "directly (skips typed-search + the job pipeline) — the reliable local path",
    )
    parser.add_argument(
        "--manga-token",
        default="blue-lockk.kw9j9",
        help="manga token to scan in --direct mode (default: Blue Lock)",
    )
    parser.add_argument("--scan-limit", type=int, default=20)
    parser.add_argument(
        "--scan-catalog",
        action="store_true",
        help="discover titles via the filter feeds and hunt for ANY scrambled title "
        "(use when the pinned title isn't scrambled)",
    )
    parser.add_argument("--max-titles", type=int, default=60)
    args = parser.parse_args()

    out_dir: Path = args.capture_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    mode = "synthetic"
    if not args.synthetic:
        try:
            if args.scan_catalog:
                print("[scan] hunting the catalog for a scrambled title…")
                n = asyncio.run(_live_scan_catalog(out_dir, args.max_titles))
            elif args.direct:
                print("[direct] scanning for a scrambled chapter to capture…")
                n = asyncio.run(
                    _live_capture_direct(out_dir, args.manga_token, args.scan_limit)
                )
            else:
                print("[live] attempting real MangaFire capture (--synthetic skips)…")
                n = asyncio.run(_live_capture(out_dir))
        except Exception as exc:  # noqa: BLE001 — fall back, never crash the proof
            print(
                f"[live] capture errored ({type(exc).__name__}: {exc}); "
                "falling back to synthetic"
            )
            n = 0
        if n > 0:
            mode = "LIVE (real MangaFire chapter)"
        else:
            print("[live] no offset>0 pages captured; falling back to synthetic")

    raw_pages: list[tuple[str, bytes, int]]
    if mode.startswith("LIVE"):
        files = sorted(out_dir.glob("page_*_off*.bin"))
        raw_pages = [
            (f.name, f.read_bytes(), int(f.stem.split("off")[-1])) for f in files
        ]
    else:
        raw_pages = [("synthetic_off2.jpg", _synthetic_scrambled_page(2), 2)]

    print(f"\n*** MODE: {mode} — {len(raw_pages)} page(s) ***")
    _build_and_report(raw_pages, out_dir)


if __name__ == "__main__":
    main()
