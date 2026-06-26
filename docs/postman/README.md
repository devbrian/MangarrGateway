# Postman — Manga Gateway e2e

Hand-authored Postman v2.1 collection that exercises the full
**search → handle → download → poll → CBZ → delete** loop against a local
Manga Gateway. Built against `manga-gateway.openapi.yaml` (the contract of record).

## Files

| File | Purpose |
|------|---------|
| `Manga-Gateway.postman_collection.json` | 9-request collection covering every contract endpoint |
| `Manga-Gateway.local.postman_environment.json` | Variables (`baseUrl`, `apiKey`, `sourceKey`, `searchQuery`, …) |

## Setup

1. **Run the gateway locally.** From the repo root:

   ```powershell
   uv run python -m manga_gateway
   ```

   This is the module entrypoint (`src/manga_gateway/__main__.py`) — it loads
   `config.toml` for the API key + bind host/port and runs uvicorn as
   a single process. Never `--workers N`: the shared anti-bot session,
   in-memory caches, and job queue all assume one process per CLAUDE.md.

   If you prefer driving uvicorn directly, the app is a **factory** (no
   module-level `app = …` attribute), so use:

   ```powershell
   uv run uvicorn --factory manga_gateway.app:create_app --host 127.0.0.1 --port 9191
   ```

2. **Import both files into Postman:**
   - File → Import → drop the two JSON files.
   - In the top-right environment selector, pick **"Manga Gateway — local"**.

3. **Edit the environment:**
   - `apiKey` — paste the value from your `config.toml` config (the same
     value Mangarr uses). Sent via the collection-level `X-Api-Key` auth.
   - `sourceKey` — defaults to `comix`, which exercises the
     `cloudflare+encrypted` path (requires a warm solver and live network).
     Switch to `mangadex` (solver-free and fast — no anti-bot, but still
     needs the gateway plus upstream network) for the simplest run.
   - `searchQuery` — defaults to `the forgotten field` (works on both
     mangadex + comix); swap as needed.

## Running it

### Step-through (manual)

Walk requests 1 → 9 in order. Each request's **Tests** tab captures the
output the next request needs (`downloadHandle`, `jobId`, etc.) into the
environment automatically.

### Collection Runner (fully automated)

Runner → select the collection → select the environment → **Run**.

The polling step (`6. GET /downloads — poll until terminal`) loops itself
via `postman.setNextRequest`, sleeping 1 s between iterations, until your
job leaves a live state. Capped at 120 iterations (~2 min) so a stuck job
fails the run instead of hanging.

A green run produces: 200s across the board, `pollCount` increments,
`outputPath` populated on the `/downloads/{jobId}` response — the CBZ is
on disk at that path.

## Flow diagram

```
┌─────────────────┐  GET   ┌─────────────────────────────────────┐
│ 1. /version     │ ─────► │ confirms gateway up + API key works │
└─────────────────┘        └─────────────────────────────────────┘
┌─────────────────┐  GET   ┌──────────────────────────────────────────┐
│ 2. /caps        │ ─────► │ enumerates sources + antibot levels      │
└─────────────────┘        └──────────────────────────────────────────┘
┌─────────────────┐  POST  ┌──────────────────────────────────────────┐
│ 3. /search      │ ─────► │ returns releases[]; captures handle      │
└─────────────────┘        └──────────────────────────────────────────┘
┌─────────────────┐  POST  ┌──────────────────────────────────────────┐
│ 5. /downloads   │ ─────► │ accepts handle, returns jobId            │
└─────────────────┘        └──────────────────────────────────────────┘
┌─────────────────┐  GET   ┌──────────────────────────────────────────┐
│ 6. /downloads   │ ─loop► │ resolving → downloading → archiving →    │
│   (poll)        │        │ completed (or failed)                    │
└─────────────────┘        └──────────────────────────────────────────┘
┌─────────────────┐  GET   ┌──────────────────────────────────────────┐
│ 7. /downloads/  │ ─────► │ asserts completed + outputPath populated │
│    {jobId}      │        │ (the CBZ on disk)                        │
└─────────────────┘        └──────────────────────────────────────────┘
┌─────────────────┐ DELETE ┌──────────────────────────────────────────┐
│ 8. /downloads/  │ ─────► │ removes the job from the queue           │
│    {jobId}      │        │ (deleteData=true also unlinks the CBZ)   │
└─────────────────┘        └──────────────────────────────────────────┘
```

(`4. /recent` and `9. /status` are auxiliary — not required for the e2e.)

## Notes

- The collection sends the API key via the **collection-level auth** block,
  not per-request, so adding new requests inherits authentication for free.
- The `downloadHandle` is opaque — Mangarr (and you) never inspect it. The
  gateway resolves it into a page manifest internally (R6); the manifest
  never crosses back over the wire.
- `POST /downloads` is **idempotent on `releaseHandle`** (R5) — resubmitting
  the same handle returns the same `jobId` while a live job exists for it (or a
  completed job whose CBZ is still on disk).
- `GET /downloads` is poll-friendly (served from in-memory projection, no
  disk re-scan). The contract assumes ~1 min Mangarr poll cadence; the
  collection runs at 1 s for fast feedback.
- For Comix: the deterministic gate stubs the solver, but Postman against a
  live gateway exercises real Cloudflare. If the gateway hasn't warmed its
  solver yet, the first `/downloads` submission may sit in `resolving` for
  ~10-30 s while clearance is obtained.
