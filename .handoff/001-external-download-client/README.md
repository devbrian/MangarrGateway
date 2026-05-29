---
spike: 001
name: external-download-client
type: spec
validates: "Given Mangarr's existing IDownloadClient contract, what HTTP/JSON API must an out-of-process manga downloader (the gateway download surface) expose — and what settings/proxy/monitoring code must Mangarr add — so Mangarr drives it exactly like SABnzbd/qBittorrent?"
verdict: DRAFTED
related: [002, 003]
tags: [download, api, gateway, sonarr-parity]
---

# Spike 001: External Download Client (Manga-Gateway Download Surface)

## What This Validates

> Given Mangarr's existing `IDownloadClient` contract, what HTTP/JSON API must an out-of-process
> manga downloader expose — and what Mangarr-side code must be added — so Mangarr drives it
> exactly the way Sonarr drives SABnzbd/qBittorrent?

This document is the hand-off spec for two deliverables:

1. **The gateway download API** (what the external project must build).
2. **The Mangarr-side `GatewayDownloadClient`** (the `IDownloadClient` implementation that calls it).

The orchestration Mangarr must *restore* to actually drive this client (poll loop, tracked
downloads, completed/failed handling, queue projection) is large enough to be its own document —
it is spec'd as **spike 003 — Mangarr Re-Integration**, which is **Mangarr-side and intentionally
excluded from this gateway-only hand-off** (it lives in the Mangarr repo). This spec covers the
client + wire contract; 003 covers the Mangarr-internal plumbing that calls it.

---

## 1. Background: three models side by side

### 1a. Sonarr's external model (the target shape) — verified on `v5-develop`

Sonarr never downloads anything itself. `IDownloadClient.Download()` hands a `.nzb`/`.torrent`
to an external process (SABnzbd/qBittorrent) and returns that process's **own job id**. A timer
(`DownloadMonitoringService`) then polls `GetItems()` on every client, projects each item into a
`TrackedDownload`, and `CompletedDownloadService`/`FailedDownloadService` drive import or blocklist.
The queue UI is a pure projection of those tracked downloads.

- Contract: `src/NzbDrone.Core/Download/IDownloadClient.cs` (on `v5-develop`).
- Canonical Usenet client: `src/NzbDrone.Core/Download/Clients/Sabnzbd/Sabnzbd.cs` (+ `SabnzbdProxy.cs`, `SabnzbdSettings.cs`).
- Canonical Torrent client: `src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrent.cs`.
- Three-file decomposition every client uses: **`XxxSettings`** (`[FieldDefinition]` host/port/apikey/category + validator), **`IXxxProxy`/`XxxProxy`** (all HTTP I/O), **`Xxx`** (state mapping + `GetStatus()` + `Test()`).

### 1b. Mangarr's current in-process model (what we replace)

`InProcessImageDownloadClient : DownloadClientBase<InProcessImageDownloadClientSettings>`
([src/NzbDrone.Core/Download/Clients/InProcess/InProcessImageDownloadClient.cs](../../../src/NzbDrone.Core/Download/Clients/InProcess/InProcessImageDownloadClient.cs)):

- `Download(RemoteChapter, IIndexer)` requires the indexer to be an `IHttpAggregator`, calls
  `aggregator.GetChapterPages(release)` → `ChapterManifest`, then `ChapterDownloadService.EnqueueAsync(...)`
  and returns the new DB row id as the `DownloadId`. **Returns immediately**; fetching is fire-and-forget.
- The real work lives in `ChapterDownloadService` (bounded `Channel<ChapterDownloadJob>` per source →
  per-page `Channel<ChapterPage>` → `ChapterPageFetcher` → write `NNNN.ext` → archive to CBZ →
  `ChapterArchivedEvent`). State persists in a `ChapterDownloadState` DB row.
- `GetItems()` projects `ChapterDownloadState` rows — **but nothing polls it for the queue**;
  completion is driven by `ChapterArchivedEvent` + a 1-min `ProcessMangaCompletedCommand`, not by a
  `GetItems()` monitor. See 003 for why the live queue is structurally empty today.

### 1c. The combined-gateway model (R1 — the decision)

The external process is **one service** exposing a search surface (002) and this download surface.
Because both share one authenticated session, the manga divergence from Sonarr is:

| Sonarr | Manga gateway |
|--------|---------------|
| Indexer returns a `.torrent`/`.nzb` URL | Search returns an opaque **release handle** (002 §release object) |
| Download client fetches that file from a **different** system (Usenet/BitTorrent) | Download surface resolves the **same** release handle into a page manifest **inside the gateway** and fetches images through the **same** session |
| `Download()` ships bytes out | `Download()` ships a release handle; the gateway already knows how to fetch it |

**Consequence:** Mangarr's `Download()` no longer needs to pass page URLs or a manifest. It passes
the release handle (`release.Guid` / `release.DownloadUrl`) plus the destination intent, and the
gateway does manifest-resolution + image-fetch + archive internally. The `IIndexer` argument to
`Download()` becomes vestigial for this client (kept for contract compatibility).

---

## 2. The contract Mangarr already has

Mangarr **kept** the manga-shaped `IDownloadClient` and its base
([src/NzbDrone.Core/Download/IDownloadClient.cs](../../../src/NzbDrone.Core/Download/IDownloadClient.cs)):

```csharp
public interface IDownloadClient : IProvider
{
    DownloadProtocol Protocol { get; }                       // gateway => DownloadProtocol.Http (=3)
    Task<string> Download(RemoteChapter remoteChapter, IIndexer indexer);   // returns the gateway job id
    IEnumerable<DownloadClientItem> GetItems();              // poll target — maps gateway jobs
    DownloadClientItem GetImportItem(DownloadClientItem item, DownloadClientItem previousImportAttempt);
    void RemoveItem(DownloadClientItem item, bool deleteData);
    DownloadClientInfo GetStatus();                          // IsLocalhost + OutputRootFolders
    void MarkItemAsImported(DownloadClientItem downloadClientItem);
}
```

`DownloadClientBase<TSettings>` already supplies `ConfigContract`, `Definition`/`Settings`,
`RequestAction`, `Test()` wrapper, `TestFolder`, `DeleteItemData`, a Polly `RetryStrategy`, and
injects `IConfigService, IDiskProvider, IRemotePathMappingService, Logger, ILocalizationService`.

**The new client subclasses this base unchanged.** No contract change is required — only a new
provider implementation. This is the cheap part; the expensive part is 003.

### Status & item types Mangarr already has (the mapping targets)

`DownloadItemStatus` ([…/Download/DownloadItemStatus.cs](../../../src/NzbDrone.Core/Download/DownloadItemStatus.cs)):
`Queued=0, Paused=1, Downloading=2, Completed=3, Failed=4, Warning=5`.

`DownloadClientItem` ([…/Download/DownloadClientItem.cs](../../../src/NzbDrone.Core/Download/DownloadClientItem.cs)):
`DownloadClientInfo, DownloadId, Category, Title, TotalSize, RemainingSize, RemainingTime,
SeedRatio, OutputPath (OsPath), Message, Status, IsEncrypted, CanMoveFiles, CanBeRemoved`.
(`SeedRatio`/`IsEncrypted`/`Category` are torrent/usenet leftovers — unused by manga; leave default.)

`DownloadClientInfo` ([…/Download/DownloadClientInfo.cs](../../../src/NzbDrone.Core/Download/DownloadClientInfo.cs)):
`IsLocalhost, SortingMode, RemovesCompletedDownloads, OutputRootFolders (List<OsPath>)`.

---

## 3. The Mangarr-side `GatewayDownloadClient` (three-file decomposition)

Mirror SABnzbd exactly (proxy/settings/client split). New files under
`src/NzbDrone.Core/Download/Clients/Gateway/`:

```
Gateway/
├── GatewayDownloadClient.cs          // : DownloadClientBase<GatewayDownloadClientSettings>
├── GatewayDownloadClientSettings.cs  // [FieldDefinition] host/port/apikey/useSsl/urlBase + validator
├── IGatewayDownloadProxy.cs          // the HTTP surface (one method per gateway endpoint)
├── GatewayDownloadProxy.cs           // all HTTP I/O; errors -> DownloadClient*Exception
└── Responses/                        // DTOs deserialized from gateway JSON
    ├── GatewaySubmitResponse.cs
    ├── GatewayJob.cs
    ├── GatewayJobList.cs
    └── GatewayStatusResponse.cs
```

> **Note on R1:** the gateway's *search* surface (002) is registered separately as an
> `IIndexer`. Settings overlap (same host/port/apikey). Consider a shared
> `GatewaySettingsBase` or simply duplicate the four connectivity fields in both settings
> classes (Sonarr duplicates them across every client/indexer — duplication is idiomatic here).

### 3a. `GatewayDownloadClientSettings`

```csharp
public class GatewayDownloadClientSettings : DownloadClientSettingsBase<GatewayDownloadClientSettings>
{
    [FieldDefinition(0, Label="Host",    Type=FieldType.Textbox)] public string Host { get; set; } = "localhost";
    [FieldDefinition(1, Label="Port",    Type=FieldType.Textbox)] public int Port { get; set; } = 9191;
    [FieldDefinition(2, Label="UseSsl",  Type=FieldType.Checkbox)] public bool UseSsl { get; set; }
    [FieldDefinition(3, Label="UrlBase", Type=FieldType.Textbox, Advanced=true)] public string UrlBase { get; set; }
    [FieldDefinition(4, Label="ApiKey",  Type=FieldType.Textbox, Privacy=PrivacyLevel.ApiKey)] public string ApiKey { get; set; }
    // Manga divergence from SAB: no "category". Optional advanced fields:
    [FieldDefinition(5, Label="OutputFormat", Type=FieldType.Select, SelectOptions=typeof(ArchiveFormat), Advanced=true)] public int OutputFormat { get; set; } // CBZ default
    [FieldDefinition(6, Label="ConcurrentChapters", Type=FieldType.Number, Advanced=true)] public int ConcurrentChapters { get; set; } = 2;

    private static readonly GatewayDownloadClientSettingsValidator Validator = new();
    public override NzbDroneValidationResult Validate() => new(Validator.Validate(this));
}
```

Validator: `Host` valid host; `Port` 1–65535; `ApiKey` `NotEmpty`. (No category required —
this is the chief settings divergence from `SabnzbdSettings`, which warns on empty `TvCategory`.)

### 3b. `IGatewayDownloadProxy` (the external surface, one method per endpoint)

```csharp
public interface IGatewayDownloadProxy
{
    // submit a grab; returns the gateway job id (the DownloadId)
    GatewaySubmitResponse Submit(GatewaySubmitRequest request, GatewayDownloadClientSettings settings);

    // poll: live jobs + recently-finished history in one call
    GatewayJobList GetJobs(GatewayDownloadClientSettings settings);
    GatewayJob GetJob(string jobId, GatewayDownloadClientSettings settings);

    // remove from gateway; deleteData also removes downloaded/staged files
    void RemoveJob(string jobId, bool deleteData, GatewayDownloadClientSettings settings);

    // status: output root folders + capabilities + whether gateway auto-removes finished jobs
    GatewayStatusResponse GetStatus(GatewayDownloadClientSettings settings);

    // connectivity / version (Test())
    GatewayVersion GetVersion(GatewayDownloadClientSettings settings);
}
```

`GatewayDownloadProxy` builds `http(s)://{Host}:{Port}/{UrlBase}/api/v1/...` requests via
`IHttpClient`, adds the `X-Api-Key` header, and maps failures: connect/DNS →
`DownloadClientUnavailableException`; 401/403 → `DownloadClientAuthenticationException`;
4xx job-rejection → `DownloadClientRejectedReleaseException`; other non-2xx →
`DownloadClientException`. (All three exception types already exist under
`src/NzbDrone.Core/Download/Clients/`.)

### 3c. `GatewayDownloadClient`

```csharp
public class GatewayDownloadClient : DownloadClientBase<GatewayDownloadClientSettings>
{
    public override string Name => "Manga Gateway";
    public override DownloadProtocol Protocol => DownloadProtocol.Http;

    public override async Task<string> Download(RemoteChapter remoteChapter, IIndexer indexer)
    {
        var req = new GatewaySubmitRequest
        {
            ReleaseHandle  = remoteChapter.Release.Guid,        // opaque handle from search (002)
            DownloadUrl    = remoteChapter.Release.DownloadUrl, // gateway-internal manifest URL/handle
            SourceKey      = /* release.Indexer or a source tag */,
            MangaId        = remoteChapter.Manga.Id,
            ChapterIds     = remoteChapter.Release.ChapterIds,
            Title          = remoteChapter.Release.Title,
            OutputFormat   = Settings.OutputFormat,
            // identity so the gateway can name the output folder predictably:
            MangaTitle     = remoteChapter.Manga.Title,
            ChapterNumbers = remoteChapter.Chapters.Select(c => c.ChapterNumber).ToList(),
        };
        var resp = _proxy.Submit(req, Settings);
        if (resp?.JobId is null) throw new DownloadClientRejectedReleaseException(remoteChapter.Release, resp?.Message ?? "Gateway rejected the grab");
        return resp.JobId;                                      // becomes DownloadClientItem.DownloadId
    }

    public override IEnumerable<DownloadClientItem> GetItems()
    {
        var list = _proxy.GetJobs(Settings);
        foreach (var j in list.Jobs)
            yield return MapJob(j);                             // see §5 mapping table
    }

    public override void RemoveItem(DownloadClientItem item, bool deleteData)
        => _proxy.RemoveJob(item.DownloadId, deleteData, Settings);

    public override DownloadClientInfo GetStatus()
    {
        var s = _proxy.GetStatus(Settings);
        return new DownloadClientInfo
        {
            IsLocalhost = s.IsLocalhost,
            RemovesCompletedDownloads = s.RemovesCompletedDownloads,
            OutputRootFolders = s.OutputRootFolders
                .Select(p => _remotePathMappingService.RemapRemoteToLocal(Settings.Host, new OsPath(p)))
                .ToList(),
        };
    }

    protected override void Test(List<ValidationFailure> failures)
    {
        try { var v = _proxy.GetVersion(Settings); /* min-version gate */ }
        catch (DownloadClientAuthenticationException) { failures.Add(new("ApiKey", _localizationService.GetLocalizedString("...InvalidApiKey"))); }
        catch (Exception ex) { failures.Add(new("Host", ex.Message)); }
        // optional: assert OutputRootFolders are reachable/remappable
    }
}
```

`GetImportItem` default (return item) is fine if `GetJob`/`GetJobs` already returns a usable
`OutputPath` for completed jobs (recommended — see §4). `MarkItemAsImported` can stay the base
no-op (manga has no post-import label concept). Override it only if you want the gateway to GC a
completed job after Mangarr confirms import.

---

## 4. Download lifecycle (end to end)

```
Mangarr                                  Gateway (combined process)
───────                                  ──────────────────────────
DownloadReport(remoteChapter)
  └─ GatewayDownloadClient.Download() ── POST /api/v1/downloads ──▶ create job, return {jobId}
       returns jobId (DownloadId)                                   (search session already authed)
  └─ publishes ChapterGrabbedEvent                                  resolve release → page manifest
       (003: writes "grabbed" download history)                    fetch each image via shared session
                                                                    archive → CBZ in staging/output dir
DownloadMonitoringService (timer, 003)
  └─ GetItems() ──────────────────────── GET /api/v1/downloads ──▶ [ {jobId, status, progress, outputPath?}, ... ]
       builds/updates TrackedDownload
  └─ CompletedDownloadService.Check()
       sees Status=Completed + OutputPath
       └─ import via IImportApprovedChapters (scans OutputPath)
  └─ DownloadProcessingService
       └─ RemoveItem(deleteData) ─────── DELETE /api/v1/downloads/{jobId}?deleteData=true ──▶ remove job + staged files
Queue UI = projection of TrackedDownloads (live progress)
```

**Who archives?** The gateway. It owns the session and the bytes, so it does fetch + CBZ
packaging and exposes a finished folder/file at `OutputPath`. Mangarr's import pipeline
(`IMakeMangaImportDecision` → `IImportApprovedChapters`) then scans that path exactly as it does
for the in-process client today. This keeps Mangarr's `MediaFiles/MangaImport/` pipeline unchanged.

**OutputPath contract:** for a `Completed` job, `GetJob`/`GetJobs` MUST return an `outputPath`
that is a host-reachable path *after* `IRemotePathMappingService.RemapRemoteToLocal`. If the
gateway runs in a container/another host, the user configures a remote-path mapping (the
mechanism already exists and is injected into `DownloadClientBase`). This mirrors SABnzbd's
`history.Storage` → `RemapRemoteToLocal` → walk-to-job-folder behavior.

---

## 5. Status mapping: gateway job → `DownloadClientItem`

Define a gateway status vocabulary and map it to Mangarr's six-value `DownloadItemStatus`.

| Gateway `status` | `DownloadItemStatus` | `DownloadClientItem` fields |
|---|---|---|
| `queued` | `Queued` | `RemainingSize=TotalSize`, `CanBeRemoved=true`, `CanMoveFiles=true` |
| `resolving` (fetching manifest) | `Queued` | progress unknown |
| `downloading` | `Downloading` | `RemainingSize = TotalSize − downloaded`, `RemainingTime` if estimable |
| `archiving` | `Downloading` | (Mangarr has no "post-processing" status; fold into Downloading, as SAB does for verifying/moving) |
| `completed` | `Completed` | `OutputPath = <finished CBZ folder>`, `CanBeRemoved=true` |
| `failed` | `Failed` | `Message = failureReason` |
| `warning` (partial / pages missing / retryable) | `Warning` | `Message = detail` |
| `paused` | `Paused` | (optional; only if gateway supports pause) |

Field mapping for every item:

```csharp
DownloadClientItem MapJob(GatewayJob j) => new()
{
    DownloadClientInfo = DownloadClientItemClientInfo.FromDownloadClient(this, hasPostImportCategory: false),
    DownloadId   = j.JobId,
    Title        = j.Title,
    TotalSize    = j.TotalBytes,                 // 0 if unknown (size is advisory for manga)
    RemainingSize= j.RemainingBytes,
    RemainingTime= j.EtaSeconds is { } s ? TimeSpan.FromSeconds(s) : null,
    OutputPath   = j.OutputPath is null ? default : new OsPath(j.OutputPath),
    Message      = j.Message,
    Status       = MapStatus(j.Status),
    CanBeRemoved = j.Status is "completed" or "failed" or "warning",
    CanMoveFiles = j.Status is "completed",
    // Category/SeedRatio/IsEncrypted: leave default (torrent/usenet leftovers)
};
```

> **Size is advisory.** Manga has no reliable byte-size before fetch. `TotalSize=0` is fine;
> Mangarr's specs don't gate on size for the manga path. (The in-process client already estimates.)

---

## 6. The gateway download API (what the external project builds)

Base path `{/UrlBase}/api/v1`. Auth: `X-Api-Key: <key>` header (also accept `?apikey=` for parity
with the *arr ecosystem). All bodies JSON; `Content-Type: application/json`.

### `POST /downloads` — submit a grab → returns job id

Request:
```json
{
  "releaseHandle": "comix:mr3m0:ch-1023:en",
  "downloadUrl": "https://gateway-internal/resolve/…",
  "sourceKey": "comix.to",
  "title": "Solo Leveling - Chapter 179 [Team Lumikha]",
  "mangaId": 42,
  "mangaTitle": "Solo Leveling",
  "chapterIds": [9001],
  "chapterNumbers": [179],
  "outputFormat": "cbz"
}
```
Response `200`:
```json
{ "jobId": "j_01HF…", "status": "queued", "message": null }
```
Rejection `4xx` (e.g. release no longer resolvable): `{ "jobId": null, "message": "…" }` →
Mangarr throws `DownloadClientRejectedReleaseException`.

`jobId` is the gateway's own id and becomes the Mangarr `DownloadId` (R5). It MUST be stable for
the life of the job (queue → history). Idempotency: re-submitting the same `releaseHandle` SHOULD
return the existing `jobId` rather than duplicating (prevents double-grab on retry).

### `GET /downloads` — poll live + recently-finished (maps to `GetItems()`)

Response `200`:
```json
{
  "jobs": [
    { "jobId":"j_01HF…","title":"…","status":"downloading","totalBytes":0,"remainingBytes":0,
      "downloadedPages":12,"totalPages":34,"etaSeconds":40,"outputPath":null,"message":null,
      "sourceKey":"comix.to","createdAt":"2026-05-27T18:00:00Z","updatedAt":"2026-05-27T18:01:00Z" },
    { "jobId":"j_01HE…","title":"…","status":"completed","totalPages":50,"downloadedPages":50,
      "outputPath":"/downloads/manga-42/Solo Leveling - Chapter 178.cbz",
      "createdAt":"…","updatedAt":"…","completedAt":"…" }
  ]
}
```
The gateway MUST keep finished jobs in this list long enough for Mangarr to import them
(retention window; default ≥ a few hours, or until Mangarr calls `DELETE`). This mirrors SAB
returning both queue and history.

### `GET /downloads/{jobId}` — single job (maps to `GetImportItem` refinement)

Same `job` shape. Used when a bulk poll item lacks an `outputPath` and Mangarr needs to resolve it.

### `DELETE /downloads/{jobId}?deleteData={bool}` — remove (maps to `RemoveItem`)

`deleteData=true` also deletes the staged/output files. Returns `204`.

### `GET /status` — output folders + capabilities (maps to `GetStatus()`)

```json
{
  "isLocalhost": true,
  "removesCompletedDownloads": false,
  "outputRootFolders": ["/downloads"],
  "capabilities": { "pause": false, "outputFormats": ["cbz","cbt","folder"], "maxConcurrentChapters": 4 },
  "version": "1.0.0"
}
```
`outputRootFolders` drives Mangarr's disk-space health check and import scanning. If the gateway
auto-removes finished jobs, set `removesCompletedDownloads=true` so Mangarr's
`DownloadClientCheck`/processing logic adapts (Sonarr keys off this flag).

### `GET /version` (or `/health`) — `Test()` probe

```json
{ "version": "1.0.0", "status": "ok" }
```

> **Security note (defensive):** the gateway exposes outbound web-scraping + writes files to a
> shared download dir. Bind to localhost by default; require the API key on every endpoint; never
> reflect arbitrary `outputPath` writes from the client. Treat `releaseHandle`/`downloadUrl` as
> gateway-issued tokens, not Mangarr-supplied URLs to fetch blindly (SSRF avoidance).

---

## 7. `Test()` shape (mirror SABnzbd's probe trio, manga-trimmed)

1. **Connection + version** — `GET /version`; gate on a minimum gateway version.
2. **Auth** — a 401/403 from `/status` → "API Key incorrect" failure.
3. **Output folder** — optionally assert each `outputRootFolders` entry remaps to a
   Mangarr-reachable, writable path (`TestFolder` from the base class). Warn (don't fail) if the
   gateway is on another host with no remote-path mapping configured.

(There is no "category exists / job folders enabled / sorting disabled" probe — those are
SAB-specific.)

---

## 8. Error handling, retries, rate limiting

- **Transport errors** → `DownloadClientUnavailableException` (Mangarr's
  `DownloadClientStatusService` then applies the standard backoff/auto-disable ladder — already
  present in the kept code).
- **Auth** → `DownloadClientAuthenticationException`.
- **Job rejection on submit** → `DownloadClientRejectedReleaseException` (carries the release so
  Mangarr can blocklist/redownload via 003's `FailedDownloadService`).
- **Retries:** `DownloadClientBase.RetryStrategy` (Polly, 2 attempts, 5xx/408) wraps proxy calls.
- **Poll cadence:** Mangarr's restored `DownloadMonitoringService` runs on a timer (Sonarr default
  ~1 min, debounced 5s on grab/import events). The gateway should tolerate frequent `GET /downloads`
  polling cheaply (return cached state; don't re-scan disk per poll).

---

## 9. Edge cases the spec must pin down

| Case | Required gateway behavior |
|---|---|
| Manifest token expired mid-fetch | Gateway re-resolves internally (it owns the search session); only surfaces `failed` after its own retry budget. Mangarr never sees the token. |
| Partial chapter (some pages 404) | `warning` status + `message`; gateway decides retry vs. give-up. Mangarr's `FailedDownloadService` (003) can blocklist+redownload on `failed`. |
| Source temporarily blocked (Cloudflare) | Gateway retries/solves internally; if unrecoverable, `failed` with a typed reason so Mangarr can auto-disable that *source* (see 002 per-source status). |
| Duplicate grab (retry/race) | Idempotent submit (same `releaseHandle` → same `jobId`). |
| Gateway restart mid-download | Jobs SHOULD survive restart (persist job state) OR return `failed` so Mangarr redownloads. Document which. |
| Output collision (chapter already on disk) | Mangarr's import decision handles dedupe (`ChapterFile` exists check already in `ProcessMangaCompletedDownloads`). Gateway just produces the file. |
| Very large chapter / slow source | `etaSeconds` optional; Mangarr tolerates unknown ETA. |

---

## 10. What stays in Mangarr vs. moves to the gateway

| Concern | Today (in-process) | After (gateway) |
|---|---|---|
| Page fetch (`ChapterPageFetcher`) | Mangarr | **Gateway** |
| Bounded-channel orchestration (`ChapterDownloadService`) | Mangarr | **Gateway** |
| CBZ archiving (`IChapterArchiverFactory`) | Mangarr | **Gateway** (or keep in Mangarr if the gateway returns a folder — see open question) |
| Anti-bot/Cloudflare (`ComixPlaywrightSigner`, `CloudflareClearanceService`) | Mangarr | **Gateway** (002) |
| `DownloadClientState` DB rows + housekeeper | Mangarr | **Gateway** (its own persistence) |
| Import decision + move (`MediaFiles/MangaImport`) | Mangarr | **Mangarr** (unchanged) |
| Queue / history / monitoring | Missing today | **Mangarr** (restore — 003) |

---

## 11. Open questions for the user / external team

1. **Archiving location.** Gateway archives to CBZ (clean — Mangarr just imports a file), OR
   gateway delivers a *folder of images* and Mangarr keeps `IChapterArchiverFactory`? Folder
   delivery keeps Mangarr in control of CBZ/ComicInfo.xml metadata (scanlation group, language)
   but couples the two more. **Recommendation: gateway returns a folder; Mangarr archives** — so
   ComicInfo.xml generation (which needs Mangarr's metadata) stays in Mangarr. Revisit in 003.
2. **Does the gateway need the destination root path,** or does Mangarr always move from the
   gateway's `outputPath` into the library? (Sonarr leaves files in the client's complete-dir and
   *imports/moves* them — recommend the same: gateway writes to its own dir, Mangarr imports out.)
3. **Pause/resume** — needed for v1, or defer? (Sonarr supports it; manga rarely needs it.)
4. **Auth model** — static API key (recommended, matches *arr) vs. per-request HMAC of the
   release handle (defends a localhost-exposed gateway harder). Default: API key, localhost bind.

---

## Investigation Trail

- Confirmed Mangarr kept `IDownloadClient`/`DownloadClientBase`/`DownloadClientItem`/`DownloadClientInfo`/`DownloadItemStatus` (modified, not deleted) — the new client is a pure provider addition, no contract change.
- Confirmed via `v5-develop` diff that SABnzbd is the closest template (returns a job id from the add-response; `GetItems()` = queue+history concat; `GetStatus()` resolves output folders; `Test()` = version+auth+category probes). qBittorrent is the fallback template *only if* the gateway can't return an id synchronously — but the gateway can (it's our process), so **SABnzbd model wins**.
- Key realization (R6/R1): the `IIndexer` arg to `Download()` and the page-manifest plumbing become gateway-internal; Mangarr's `Download()` shrinks to "submit release handle → get job id."
- Largest downstream dependency: none of this is *driven* without restoring the monitoring loop → 003.

## Results

**Verdict: DRAFTED.** The download-client wire contract is small and clean because Mangarr already
owns the `IDownloadClient` surface and SABnzbd provides a proven template. The external project
needs ~6 endpoints (`POST/GET/GET{id}/DELETE /downloads`, `GET /status`, `GET /version`) and a
stable job-id + `outputPath`-on-complete contract. The real cost is **Mangarr-side** (003), not the
client. No blocker found; the design is consistent with the kept contract surface and the
combined-gateway decision (R1).
