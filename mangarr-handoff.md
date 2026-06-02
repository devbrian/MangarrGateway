# Manga Gateway → Mangarr Integration Hand-off

**Audience:** the Claude agent working in the **Mangarr** repo (the Sonarr-fork manga
library manager) that will build the *consumer* side — one `IIndexer` and one
`IDownloadClient` pointing at this gateway.

**Status:** the gateway is built, tested, and containerized (v1.0 shipped). This
document plus **`manga-gateway.openapi.yaml`** (the contract of record) is everything
you need to integrate. When this prose and the OpenAPI file disagree, **the OpenAPI
file wins** — it is generated-faithful to the running service (verified 2026-06-02).

---

## 1. The one-breath model

The gateway is **one process exposing two JSON-REST surfaces that share one
authenticated site session and one anti-bot/Cloudflare solver**:

- a **search/indexer surface** (your `IIndexer` — the Jackett/Torznab analog):
  `GET /caps`, `POST /search`, `GET /recent` → normalized releases, each carrying an
  opaque `downloadHandle`; and
- a **download surface** (your `IDownloadClient` — the SABnzbd/qBittorrent analog):
  `POST /downloads`, `GET /downloads`, `GET /downloads/{jobId}`,
  `DELETE /downloads/{jobId}`, `GET /status` → resolves a handle into a page manifest,
  fetches the chapter's images through the same session, packages to CBZ, and reports
  queue/progress/completion.

**The core contract (R1 + R5 + R6):** Mangarr submits the same opaque release handle
that search returned and gets back a packaged chapter — **without ever touching a site
session, an anti-bot challenge, or a page URL.** The session that *finds* a release is
the session that *downloads* it. The page manifest never crosses the wire to Mangarr.

You register the gateway as **one indexer + one download client**, both
`DownloadProtocol.Http`, both pointing at the **same host**.

---

## 2. Connecting

| Thing | Value |
|-------|-------|
| Base URL | `http://localhost:9191/api/v1` (default localhost bind) |
| Path prefix | **Every** endpoint is under `/api/v1`. `GET /version` → `GET /api/v1/version`. |
| Reverse-proxy prefix | If `url_base` is configured, prepend it (FastAPI `root_path`). |
| Auth | **API key, required on every endpoint.** Send it either as the `X-Api-Key` request header **or** the `?apikey=` query param. |
| API key source | Auto-generated and persisted to the gateway's `config.toml` on first start. The operator reads it from there; it is **never** set via env var. |
| Content type | `application/json` everywhere. No XML/RSS (Torznab is the conceptual model only). |

A missing/invalid key returns `401` with the standard error envelope (see §8). Auth is
applied globally, so `GET /version` is also key-gated — use it as your `Test()` probe
*with* the key.

---

## 3. Surface map

| Method & path (under `/api/v1`) | operationId | Maps to (Sonarr parlance) |
|---|---|---|
| `GET /version` | `getVersion` | `Test()` health probe (both surfaces) |
| `GET /caps` | `getCaps` | indexer capabilities handshake; cache 6–12 h |
| `POST /search` | `search` | `IIndexer` interactive/automatic search |
| `GET /recent` | `getRecent` | `IIndexer.FetchRecent` (RSS sync) |
| `POST /downloads` | `submitDownload` | `IDownloadClient.Download()` (grab) |
| `GET /downloads` | `getDownloads` | `IDownloadClient.GetItems()` (poll the queue) |
| `GET /downloads/{jobId}` | `getDownload` | `GetImportItem()` (refine OutputPath) |
| `DELETE /downloads/{jobId}` | `removeDownload` | `RemoveItem()` |
| `GET /status` | `getStatus` | `IDownloadClient.GetStatus()` |

---

## 4. The integration flow (end-to-end)

```
1. Test()           → GET /version            (200 {version,status:"ok"})
2. caps handshake   → GET /caps               (cache; lists sources + formats + limits)
3. search           → POST /search            (returns releases[], each w/ downloadHandle)
   recent sync      → GET /recent             (newest-first feed, same Release shape)
4. grab a release   → POST /downloads         (send releaseHandle + sourceKey; get jobId)
5. poll the queue   → GET /downloads          (~1/min; watch status + progress)
6. on completed     → GET /downloads/{jobId}  (read outputPath, import the CBZ)
7. after import     → DELETE /downloads/{jobId}?deleteData=false   (drop from queue)
```

**`jobId` is your join key (R5).** It is stable for the life of the job
(grab → history → tracked-download → queue → import). It is the value returned by
`POST /downloads` and the key in every queue projection.

**`downloadHandle` round-trip (R6).** A `Release.downloadHandle` from search is the
*exact* string you put in `SubmitRequest.releaseHandle`. It is an opaque,
gateway-issued token (not a URL). Do not parse, construct, or fetch it yourself.

---

## 5. Endpoint reference (request/response essentials)

Field names on the wire are **camelCase**. Below, `?` = optional/nullable.

### `GET /caps` → `Capabilities`
```jsonc
{
  "gatewayVersion": "0.1.0",
  "sources": [ /* SourceCap[] — see §7 for the live set */ ],
  "supportedSearchParams": ["q","mangadexId","anilistId","malId","chapter","volume","language","sourceKey"],
  "limits": { "defaultPageSize": 50, "maxPageSize": 100 },
  "downloadFormats": ["cbz","cbt","folder"]
}
```
`sources[].enabled` is **live**: a source whose anti-bot breaker has tripped reports
`enabled:false` in the same poll (the 12 h cache never masks a freshly-degraded
source). Cache the rest 6–12 h.

### `POST /search` → `ReleaseListResponse`
Request (`SearchRequest`, only `type` is required):
```jsonc
{
  "type": "manga" | "chapter",        // required
  "query": "Chainsaw Man",            // recommended even when ids present
  "ids": { "mangadexId": "…", "anilistId": 105398, "malId": 121496 },
  "chapter": 179.0,                   // decimal; for type=chapter
  "volume": 11,
  "languages": ["en"],
  "sources": ["mangadex","comix"],    // omit = all enabled sources
  "interactive": false,               // true widens candidate fan-out
  "limit": 50,
  "offset": 0
}
```
**Guard:** a request with neither a non-empty `query` nor a usable id
(`mangadexId`/`anilistId`/`malId`) returns `400 bad_request` *before* any fan-out.

Response:
```jsonc
{
  "releases": [ /* Release[] — see §6 */ ],
  "warnings": [ { "sourceKey": "comix", "code": "source_degraded", "message": "…" } ]
}
```
**Per-source failures are NOT HTTP errors.** A source that times out / errors / is
rate-limited yields a `warnings[]` entry while the call still returns `200` with
whatever the healthy sources produced. Always read `warnings[]`.

### `GET /recent` → `ReleaseListResponse`
Query params (all optional): `sources` (CSV of keys), `languages` (CSV of BCP-47),
`limit` (default & max **100** — clamped, never 400s on a bad value), `since`
(ISO-8601 date-time; a defensive newest-than cut is applied server-side). Results are
merged **newest-first by `publishDate`**. Same response shape and warnings semantics as
`/search`.

### `POST /downloads` → `SubmitResponse`
Request (`SubmitRequest`, required: `releaseHandle`, `sourceKey`):
```jsonc
{
  "releaseHandle": "<Release.downloadHandle from search>",  // required
  "sourceKey": "mangadex",                                  // required
  "downloadUrl": null,        // ADVISORY ONLY — gateway ignores it for fetch (SSRF guard)
  "title": "…",
  "mangaId": 42,              // Mangarr's manga id (used for output-folder naming)
  "mangaTitle": "Chainsaw Man",
  "chapterIds": [1234],
  "chapterNumbers": [179.0],
  "outputFormat": "cbz"       // cbz (default) | cbt | folder
}
```
Success → `200`:
```jsonc
{ "jobId": "…", "status": "queued" | "resolving", "message": null }
```
**Idempotent on `releaseHandle` (R5/DL-03):** re-submitting a handle for a live or
completed job returns the *existing* `jobId` (the `status` field is only populated when
it is a freshly-scheduled `queued`/`resolving`, else omitted).

**Rejection (expired/unknown handle) → `400` — but the body is a `SubmitResponse`, NOT
the error envelope:**
```jsonc
{ "jobId": null, "message": "release no longer resolvable" }
```
So: on a `400` from `POST /downloads`, parse it as a `SubmitResponse` with
`jobId:null`, not as `{error:{…}}`. (This is the one endpoint whose 400 shape differs.)

### `GET /downloads` → `{ "jobs": DownloadJob[] }`
The poll endpoint. Returns the **in-memory** queue/history projection — cheap under
frequent polling (designed for ~1/min, debounce 5 s). Does **not** re-scan disk per
poll. See §6 for `DownloadJob`.

### `GET /downloads/{jobId}` → `DownloadJob`
One job. `404` (error envelope, code `not_found`) for an unknown id. Use it to read the
final `outputPath` at import time.

### `DELETE /downloads/{jobId}?deleteData=false` → `204`
Removes the job from the queue. `deleteData=true` also unlinks **only** the job's own
gateway-computed output + staging temps (never a client-supplied path). `404` for an
unknown id. `deleteData` is parsed tolerantly (a bad value → `false`, never a 400).

### `GET /status` → `StatusResponse`
```jsonc
{
  "isLocalhost": true,
  "removesCompletedDownloads": false,   // gateway keeps output until you DELETE
  "outputRootFolders": ["/data/manga"], // gateway-determined; never client-supplied
  "version": "0.1.0",
  "capabilities": { "pause": false, "outputFormats": ["cbz","cbt","folder"], "maxConcurrentChapters": 3 }
}
```

---

## 6. The `Release` and `DownloadJob` objects

### `Release` (what search/recent return)
Required: `guid`, `title`, `sourceKey`, `downloadHandle`, `publishDate`.
```jsonc
{
  "guid": "…",                  // dedup key — de-dupe your release list by this
  "title": "…",                 // human title; ALSO parseable by Mangarr's MangaParser
  "sourceKey": "mangadex",
  "downloadHandle": "…",        // opaque; submit verbatim to POST /downloads. TTL >= 30 min.
  "publishDate": "2026-05-01T12:00:00Z",
  "infoUrl": null,
  // ── structured fields: first-class data, the intended PRIMARY path (parsing
  //    `title` becomes optional when these are populated). null only when truly unknown.
  "mangaTitle": "Chainsaw Man",
  "chapterNumber": 179.0,       // decimal; >=3 places preserved when known
  "volume": 11,
  "language": "en",             // BCP-47 → ReleaseInfo.TranslatedLanguage
  "scanlationGroup": "…",
  "pageCount": 18,
  "sizeBytes": 0,               // often 0/estimate pre-download — do not gate import on it
  "ids": { "mangadexChapterId": "…", "mangadexMangaId": "…" }  // cross-ref ids when known
}
```
**Mapping guidance:** prefer the structured fields (`mangaTitle`, `chapterNumber`,
`volume`, `language`, `scanlationGroup`) over re-parsing `title`. `title` is kept
parser-compatible as a fallback. De-dupe by `guid`. One `Release` == one grabbable
chapter upload (no pre-merge across uploads).

### `DownloadJob` (what the queue returns)
Required: `jobId`, `title`, `status`.
```jsonc
{
  "jobId": "…",
  "title": "…",
  "sourceKey": "mangadex",
  "status": "queued|resolving|downloading|archiving|completed|failed|warning|paused",
  "totalBytes": 0, "remainingBytes": 0,        // byte progress (may be estimated)
  "totalPages": 18, "downloadedPages": 7,      // page progress — the reliable signal
  "etaSeconds": 42,
  "outputPath": "/data/manga/…/Chapter 179.cbz",  // host-reachable when completed
  "message": null,
  "createdAt": "…", "updatedAt": "…", "completedAt": null
}
```

---

## 7. Sources currently registered (live `/caps` data)

Verified against the running registry (2026-06-02). **Use these exact `key` values**
in `SearchRequest.sources` and `SubmitRequest.sourceKey`:

| `key` | `name` | `antibot` | `idTypes` | `languages` | `rateLimitPerMinute` | search | recent |
|-------|--------|-----------|-----------|-------------|----------------------|--------|--------|
| `mangadex` | MangaDex | `none` | `["mangadexId"]` | en, es, es-la, fr, de, pt-br, ru, ja, ko, zh | 300 | ✓ | ✓ |
| `comix` | Comix | `cloudflare+encrypted` | `[]` (title-search only) | en | 10 | ✓ | ✓ |

The framework is built for 50+ sources; new ones appear in `/caps` automatically. Don't
hardcode this list — read `/caps` and respect each source's `enabled` flag and
`rateLimitPerMinute`.

---

## 8. Error model

Every endpoint **except** the `POST /downloads` 400 (see §5) returns errors as:
```jsonc
{ "error": { "code": "<enum>", "message": "<human text>" } }
```
`code` ∈ `auth | rate_limited | source_unavailable | bad_request | not_found | internal`.

| HTTP | code | When |
|------|------|------|
| 401 | `auth` | missing/invalid API key |
| 400 | `bad_request` | malformed body / failed validation / missing search input |
| 404 | `not_found` | unknown `jobId` on `GET`/`DELETE /downloads/{jobId}` |
| 429 | `rate_limited` | includes a `Retry-After: <seconds>` header — honor it |
| 500 | `internal` | generic; never leaks stack/detail |

---

## 9. Integration gotchas (read these before coding the consumer)

1. **CBZ holds page images only — no `ComicInfo.xml`.** The gateway packages the raw
   page images and stops. **Mangarr adds metadata** (writes `ComicInfo.xml` into the
   archive) *after* hand-off, because that needs Mangarr's metadata. Don't expect the
   gateway to embed series/chapter metadata in the file.
2. **Per-source failures live in `warnings[]`, not HTTP status.** A `200` from
   `/search` can still mean "one source is degraded." Surface/log `warnings[]`.
3. **`POST /downloads` 400 is a `SubmitResponse{jobId:null}`, not the error envelope.**
   Branch your parser on the endpoint.
4. **Idempotency is on `releaseHandle`.** Safe to re-grab; you'll get the same `jobId`.
5. **Poll `GET /downloads`, don't hammer single-job GETs.** The list is the cheap
   in-memory projection; ~1/min with a 5 s debounce is the intended cadence.
6. **`outputPath` is host-reachable when `completed`** (after any remote-path mapping).
   Read it from the job and import from there. The gateway keeps the output until you
   `DELETE` it (`removesCompletedDownloads:false`).
7. **`downloadUrl` in `SubmitRequest` is advisory and ignored** (SSRF guard). The
   gateway always resolves the manifest from the `releaseHandle`. Never rely on it.
8. **`pause` is not supported in v1** (`capabilities.pause:false`; the `paused` status
   value exists in the enum but is not produced). Don't build a pause feature against it.
9. **TTLs:** `downloadHandle` is valid **≥ 60 min** (Mangarr caches interactive-search
   releases 30 min — comfortably inside). `/caps` is cacheable 6–12 h. A handle used
   after expiry → the `jobId:null` 400 rejection (re-search to mint a fresh handle).
10. **Decimal chapter numbers survive losslessly** (e.g. `1.005`). Keep them as
    decimals end-to-end; don't round-trip through a float that drops precision.
11. **Single process, single host.** The gateway must run as one process (shared
    session/solver/queue) — do not point your client at a load-balanced multi-worker
    deployment; a handle minted by one worker is unknown to another.

---

## 10. Versioning note

- `info.version` in `manga-gateway.openapi.yaml` is the **contract document** version
  (`1.0.0`).
- The **running gateway** reports its own build version in `gatewayVersion` (`/caps`)
  and `version` (`/status`, `/version`) — currently **`0.1.0`**.

These are intentionally separate. Treat the runtime `version`/`gatewayVersion` fields as
the deployed-build identity; treat `info.version` as the spec revision.

---

## 11. Running the gateway (for local integration testing)

- Python 3.12 (floor 3.11), managed with `uv`. Single process — **never** run with
  `--workers N`.
- Install + browser engine (for the Cloudflare/Comix source):
  `uv sync` then `uv run patchright install chromium`.
- Run: `uv run python -m manga_gateway` (binds `127.0.0.1:9191` by default).
- On first start it generates `config.toml` with an `api_key` — read the key from
  there for your client config.
- Config knobs (env `GATEWAY_*` overrides TOML): `host`, `port`, `url_base`,
  `output_root`, `max_concurrent_chapters`, plus the `cloudflare_*` anti-bot knobs. See
  `src/manga_gateway/config.py` for the full list and defaults.
- A Docker image + GHCR workflow ship with the repo (Phase 6) if you prefer a container.

---

## 12. The binding artifact

**`manga-gateway.openapi.yaml`** (repo root) is the contract of record — OpenAPI 3.1,
verified faithful to the live FastAPI app on 2026-06-02. Generate your client models
from it. This document is the orientation; the YAML is the source of truth.
