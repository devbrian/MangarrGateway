# Spike Manifest

## Idea

Replace Mangarr's two in-process integrations — the **in-process image downloader**
(`InProcessImageDownloadClient` + `ChapterDownloadService`) and the **in-process website
scrapers** (`MangaDexIndexer`, `ComixIndexer`, including the embedded headless-Chromium
`ComixPlaywrightSigner`) — with a **single external "manga-gateway" process** that Mangarr
talks to over an HTTP/JSON API, exactly the way Sonarr talks to SABnzbd/qBittorrent (download)
and Jackett/Torznab (indexing).

The gateway is **one process exposing two API surfaces**:

- a **search/indexer surface** (the Jackett/Torznab analog) — one connection that fans out to
  every manga aggregator site and returns normalized releases; and
- a **download surface** (the SABnzbd/qBittorrent analog) — accepts a grab, fetches the chapter
  pages, archives to CBZ, and reports queue/progress/completion back to Mangarr.

Because a manga *release* and its *page images* come from the **same secure website**, the
session + anti-bot/Cloudflare solving is **shared once** inside the gateway and never crosses
into Mangarr. This is the fundamental divergence from Sonarr, where the indexer and the
download client talk to entirely separate systems.

These spikes are **research/spec deliverables** (no implementation) intended to be handed to
external project teams: (1) the gateway's download API + the Mangarr-side download client,
(2) the gateway's search API + the Mangarr-side indexer, and (3) the Mangarr-internal
re-integration/migration plan to drive external integrations instead of in-process ones.

## Requirements

Non-negotiable design decisions that emerged from the user's choices. Honor these in the real build.

- **R1 — One combined gateway process.** A single external "manga-gateway" service exposes
  both the search API and the download API and shares one authenticated secure-site session and
  one anti-bot solver across both. (User decision, 2026-05-27.)
- **R2 — JSON REST over HTTP(S).** All gateway APIs are JSON over HTTP(S), API-key authenticated.
  Torznab/Newznab is the **conceptual model only** (caps handshake → gated search → normalized
  release list), not the wire format. No XML/RSS. (User decision, 2026-05-27.)
- **R3 — Mangarr registers the gateway as one indexer + one download client.** Both are
  `DownloadProtocol.Http`, both point at the same gateway host. The in-process MangaDex/Comix
  aggregators and the in-process image downloader are retired from Mangarr (their capability
  moves into the gateway).
- **R4 — Restore the Sonarr monitoring/import loop.** Mangarr deleted the entire
  `poll GetItems() → TrackedDownload → completed/failed → import → queue-projection` pipeline.
  It cannot drive an external download client without it. This is the largest piece of work and
  is spec'd in 003.
- **R5 — `DownloadId` is the join key.** The opaque id returned by `Download()` links
  grab → download-history → tracked-download → queue → import, exactly as in Sonarr.
- **R6 — release → manifest → per-image fetch replaces the single-file handoff.** A manga
  "download" is a page manifest the gateway resolves and fetches; because search and download
  share one process (R1), the manifest token never round-trips through Mangarr.

## Spikes

| # | Name | Type | Validates | Verdict | Tags |
|---|------|------|-----------|---------|------|
| 001 | external-download-client | spec | HTTP API for an out-of-process manga downloader + the Mangarr-side download client that drives it like SABnzbd/qBittorrent | DRAFTED | download, api, gateway, sonarr-parity |
| 002 | universal-indexer | spec | JSON caps+search API for a single external gateway that fans out to all aggregators (Jackett/Torznab analog) | DRAFTED | indexer, search, api, gateway, antibot |
| 003 | mangarr-reintegration | spec | What Mangarr must RESTORE (monitoring/track/complete-fail/queue/history loop) and REMOVE (in-process orchestrator) to switch to external integrations | DRAFTED | migration, refactor, queue, orchestration |

## Shared Artifacts

- `manga-gateway.openapi.yaml` — consolidated OpenAPI 3.1 contract for **both** gateway surfaces
  (search + download), since R1 makes them one process. Referenced by specs 001 and 002.

## Verdict legend

`DRAFTED` = spec written and self-consistent against the codebase evidence; awaiting user review.
(These are research specs, not runnable spikes, so the terminal state is a reviewed hand-off
document rather than VALIDATED/INVALIDATED.)
