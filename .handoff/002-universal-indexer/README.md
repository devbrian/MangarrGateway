---
spike: 002
name: universal-indexer
type: spec
validates: "Given the Jackett/Torznab model, what JSON caps+search API must a single external manga-gateway expose so one connection fans out to all aggregator sites, returns normalized releases, and moves all anti-bot/Cloudflare out of Mangarr?"
verdict: DRAFTED
related: [001, 003]
tags: [indexer, search, api, gateway, antibot]
---

# Spike 002: Universal Indexer (Manga-Gateway Search Surface)

## What This Validates

> Given the Jackett/Torznab model, what JSON API must a single external "manga-gateway" expose so
> Mangarr connects to **one** indexer that fans out to **all** aggregator sites, returns normalized
> releases, and moves **all** secure-website communication (Cloudflare, Comix's encrypted-response
> headless-Chromium session) out of Mangarr?

Hand-off deliverables:

1. **The gateway search API** (caps handshake + search + recent feed) — what the external project builds.
2. **The Mangarr-side `GatewayIndexer`** (the `IIndexer` implementation that calls it).

This is the search half of the combined gateway (R1). The download half is
**[001 — External Download Client](../001-external-download-client/README.md)**; the search
result's release handle is the same token 001's `Download()` submits. The Mangarr-internal search
orchestration (`MangaReleaseSearchService`, `MangaRssSyncService`, decision engine) already exists
and is **kept** — the Mangarr-side changes are spec'd as spike 003, which is **Mangarr-side and
intentionally excluded from this gateway-only hand-off** (it lives in the Mangarr repo).

---

## 1. Background

### 1a. Sonarr/Jackett's universal-indexer model (the conceptual target) — verified on `v5-develop`

Jackett exposes one **Torznab** endpoint per tracker; Sonarr adds it as one indexer. The protocol
is two HTTP verbs:

- **`?t=caps`** — a capabilities handshake (cached 7 days) that declares categories, which search
  params the source supports (`q, season, ep, tvdbid, imdbid, …`), and page limits. Sonarr then
  **conditionally builds queries** based on caps (skips search modes the source doesn't advertise).
  - `src/NzbDrone.Core/Indexers/Newznab/NewznabCapabilitiesProvider.cs`, `NewznabCapabilities.cs`.
- **`?t=search`/`?t=tvsearch`** — the query (`q, cat, season, ep, tvdbid, imdbid, limit, offset, apikey`),
  returning **RSS `<item>`s** with a typed extension-attr bag (`<torznab:attr name=… value=…>`:
  `infohash, magneturl, seeders, peers, downloadvolumefactor, tag, language`).
  - `NewznabRequestGenerator.cs`, `RssParser.cs`, `NewznabRssParser.cs`, `Torznab/TorznabRssParser.cs`.

Sonarr stamps `IndexerId/Indexer/Protocol/Priority` locally in `IndexerBase.CleanupReleases`; only
`Guid, Title, Size, DownloadUrl, InfoUrl, CommentUrl, PublishDate, Languages, [ids], flags` come
from the wire.

**We adopt this *shape* (caps → gated search → normalized release list) but with a JSON wire format
and manga-native semantics (R2).** No XML/RSS.

### 1b. Mangarr's current in-process model (what we replace)

`IIndexer` ([src/NzbDrone.Core/Indexers/IIndexer.cs](../../../src/NzbDrone.Core/Indexers/IIndexer.cs))
is already manga-shaped:

```csharp
public interface IIndexer : IProvider
{
    bool SupportsRss { get; }
    bool SupportsSearch { get; }
    DownloadProtocol Protocol { get; }                                 // Http
    Task<IList<ReleaseInfo>> FetchRecent();                            // RSS sync
    Task<IList<ReleaseInfo>> Fetch(MangaSearchCriteria searchCriteria);    // whole-manga
    Task<IList<ReleaseInfo>> Fetch(ChapterSearchCriteria searchCriteria);  // single-chapter
    HttpRequest GetDownloadRequest(string link);
}
```

Concrete indexers (`MangaDexIndexer`, `ComixIndexer`) extend a manga-specific base chain:
`HttpAggregatorBase<TSettings>` → `HttpIndexerBase<TSettings>` → `IndexerBase<TSettings>`. The base
already gives us `FetchReleases(...)`, per-source rate-limiting (`SourceKey`), the exception ladder,
RSS watermarking, and `CleanupReleases` stamping. **Comix carries the in-process Cloudflare bypass**
(`ComixPlaywrightSigner` driving headless Chromium; encrypted-response decrypt; token rotation) plus
a generic `CloudflareClearanceService` (FlareSolverr/Byparr client). **All of that secure-site
machinery is what moves into the gateway.**

### 1c. The combined-gateway model (R1)

Mangarr registers **one** `GatewayIndexer` (one `IIndexer` definition) pointing at the gateway. The
gateway internally manages N sources (mangadex, comix, …), each with its own session/anti-bot. From
Mangarr's perspective there is a single indexer that returns releases tagged with their originating
`sourceKey`. The release's `DownloadUrl` is an **opaque gateway handle**, and grabbing it (001)
submits that handle back to the same process — so the authenticated session that found the release
is the same one that downloads it.

---

## 2. The Mangarr-side `GatewayIndexer`

New files under `src/NzbDrone.Core/Indexers/Gateway/`:

```
Gateway/
├── GatewayIndexer.cs            // : HttpIndexerBase<GatewaySettings>  (NOT HttpAggregatorBase — see note)
├── GatewaySettings.cs           // [FieldDefinition] host/port/apikey/useSsl/urlBase + source filter + validator
├── GatewayRequestGenerator.cs   // : IIndexerRequestGenerator  (builds JSON search/recent requests)
├── GatewayParser.cs             // : IParseIndexerResponse      (JSON release[] -> ReleaseInfo[])
├── GatewayCapabilities.cs       // cached caps document (categories, supported params, sources, limits)
├── GatewayCapabilitiesProvider.cs
└── Responses/                   // DTOs for caps + search JSON
```

> **Base-class choice.** `HttpAggregatorBase` exists to (a) bucket rate-limits per *source* and
> (b) implement `GetChapterPages()` for the in-process downloader. With the combined gateway, the
> gateway owns per-source rate-limiting and page-fetching, so `GatewayIndexer` should extend
> **`HttpIndexerBase<GatewaySettings>`** directly (one rate-limit bucket = the gateway host) and
> **not** implement `IHttpAggregator`/`GetChapterPages` (the download surface in 001 handles
> page-fetch). This is a clean simplification the combined-gateway decision unlocks.
>
> Trade-off: today `InProcessImageDownloadClient.Download()` casts the indexer to `IHttpAggregator`.
> Once the gateway download client (001) no longer needs the indexer for page-fetch, that coupling
> goes away (the `IIndexer` arg to `Download()` becomes vestigial). 003 tracks the removal.

`GatewayRequestGenerator` produces `IndexerRequest`s wrapping JSON GET/POST calls; `GatewayParser`
deserializes the JSON release array and maps each into `ReleaseInfo` (then `CleanupReleases` stamps
`IndexerId/Indexer/Protocol/Priority`). This reuses the entire `FetchReleases` engine unchanged.

### Settings

```csharp
public class GatewaySettings : IIndexerSettings   // BaseUrl + MultiLanguages + FailDownloads from the interface
{
    [FieldDefinition(0, Label="URL")] public string BaseUrl { get; set; }   // gateway host, e.g. http://localhost:9191
    [FieldDefinition(1, Label="ApiKey", Privacy=PrivacyLevel.ApiKey)] public string ApiKey { get; set; }
    [FieldDefinition(2, Label="Sources", Type=FieldType.Select, SelectOptionsProviderAction="gatewaySources")]
    public IEnumerable<string> EnabledSources { get; set; }   // optional allow-list; empty = all the gateway offers
    [FieldDefinition(3, Label="Languages", Type=FieldType.Select, SelectOptions=typeof(RealLanguageFieldConverter), Advanced=true)]
    public IEnumerable<int> MultiLanguages { get; set; }
    // FailDownloads carried from IIndexerSettings
}
```

The `Sources` dropdown is populated via `RequestAction("gatewaySources", …)` → `GET /caps`
(mirrors Sonarr's `newznabCategories` provider-action pattern). This is how one indexer connection
surfaces "which aggregator sites are available" in the Mangarr UI.

---

## 3. The capabilities handshake — `GET /caps`

Fetched once, **cached** (e.g. 6–24 h, keyed on settings; mirror Sonarr's 7-day caps cache). Drives
conditional query generation and the UI source list.

```json
{
  "gatewayVersion": "1.0.0",
  "sources": [
    {
      "key": "mangadex",
      "name": "MangaDex",
      "enabled": true,
      "supportsRecent": true,
      "supportsSearch": true,
      "idTypes": ["mangadexId"],
      "languages": ["en","es-la","pt-br","ja"],
      "rateLimitPerMinute": 30,
      "antibot": "none"
    },
    {
      "key": "comix.to",
      "name": "Comix",
      "enabled": true,
      "supportsRecent": true,
      "supportsSearch": true,
      "idTypes": [],
      "languages": ["en"],
      "rateLimitPerMinute": 10,
      "antibot": "cloudflare+encrypted"
    }
  ],
  "supportedSearchParams": ["q","mangadexId","anilistId","malId","chapter","volume","language","sourceKey"],
  "limits": { "defaultPageSize": 50, "maxPageSize": 100 },
  "downloadFormats": ["cbz","cbt","folder"]
}
```

- `sources[]` = the aggregators behind this one indexer (the "Jackett fans out to many trackers"
  analog). The UI lists these; `EnabledSources` filters them.
- `supportedSearchParams` = the manga query vocabulary the gateway honors. Mangarr's request
  generator only emits params in this list (Sonarr's conditional-query pattern). The manga peers of
  Torznab's `season/ep/tvdbid` are **`chapter` (decimal), `volume`, and external id types
  (`mangadexId/anilistId/malId`)**.
- `idTypes` per source = which external metadata ids that source can match on, so Mangarr can send
  `mangadexId` to MangaDex but fall back to title `q` for Comix.
- `limits` drive paging. `downloadFormats` informs 001's `OutputFormat` field.

`Test()` requires caps to be fetchable, auth-valid, and at least one source `enabled` with
`supportsSearch` or `supportsRecent`.

---

## 4. Search — `POST /search`

POST (not GET) so the manga criteria (title + ids + language list) ride in a JSON body cleanly.

Request (from a `MangaSearchCriteria` / `ChapterSearchCriteria`):
```json
{
  "type": "chapter",                 // "manga" (whole-title) | "chapter" (single)
  "query": "Solo Leveling",          // cleaned title; always sent as fallback
  "ids": { "mangadexId": "32d76d19-…", "anilistId": 105398, "malId": 121496 },
  "chapter": 179.0,                  // decimal; present for type=chapter
  "volume": null,
  "languages": ["en"],               // from PreferredLanguages / MultiLanguages
  "sources": ["mangadex","comix.to"],// from EnabledSources; omit = all
  "interactive": true,               // mirrors criteria.InteractiveSearch (relax filtering)
  "limit": 100,
  "offset": 0
}
```

Response `200`:
```json
{
  "releases": [
    {
      "guid": "comix:mr3m0:ch-179:en:lumikha",
      "title": "Solo Leveling - Chapter 179 [Team Lumikha]",
      "sourceKey": "comix.to",
      "downloadHandle": "h_01HF…",        // opaque; becomes ReleaseInfo.DownloadUrl -> 001 submit
      "infoUrl": "https://comix.to/manga/mr3m0/179",
      "publishDate": "2026-05-20T00:00:00Z",
      "mangaTitle": "Solo Leveling",
      "chapterNumber": 179.0,
      "volume": 14,
      "language": "en",
      "scanlationGroup": "Team Lumikha",
      "pageCount": 52,
      "sizeBytes": 0,
      "ids": { "mangadexId": null }
    }
  ],
  "warnings": [
    { "sourceKey": "comix.to", "code": "source_degraded", "message": "Cloudflare challenge in progress; partial results" }
  ]
}
```

- **Per-source isolation:** one failing source MUST NOT fail the whole response. Return the sources
  that worked + a `warnings[]` entry for the ones that didn't. (Mangarr's `MangaReleaseSearchService`
  already isolates per-indexer; here the gateway isolates per-source and reports it so Mangarr can
  drive per-source auto-disable — §7.)
- `downloadHandle` is the join to 001. Mangarr stores it as `ReleaseInfo.DownloadUrl`. It must remain
  valid long enough to grab (e.g. ≥ 30 min — Mangarr caches interactive-search `RemoteChapter`s for
  30 min today).

---

## 5. Recent feed — `GET /recent` (the RSS-sync peer)

Maps to `IIndexer.FetchRecent()`, driven by `MangaRssSyncService`.

```
GET /recent?sources=mangadex,comix.to&languages=en&limit=100&since=2026-05-27T17:00:00Z
```
Same `releases[]` response shape as `/search`. `since` lets the gateway return only items newer than
Mangarr's last RSS watermark (Mangarr also de-dupes via `GetLastRssSyncReleaseInfo`, so `since` is an
optimization, not a correctness requirement). Sort newest-first.

---

## 6. Release object → `ReleaseInfo` mapping

`GatewayParser.ParseResponse` maps each JSON release into
[`ReleaseInfo`](../../../src/NzbDrone.Core/Parser/Model/ReleaseInfo.cs). What comes from the wire vs.
what Mangarr stamps:

| Gateway JSON field | `ReleaseInfo` field | Notes |
|---|---|---|
| `guid` | `Guid` | dedup key (`CleanupReleases` de-dupes by Guid) |
| `title` | `Title` | parsed downstream by `MangaParser.ParseChapterTitle` |
| `downloadHandle` | `DownloadUrl` | opaque; 001 submits it back. **Not a URL Mangarr fetches.** |
| `infoUrl` | `InfoUrl` | UI "open source page" link |
| `publishDate` | `PublishDate` | drives `Age`/RSS watermark; **required** |
| `sizeBytes` | `Size` | 0 acceptable |
| `scanlationGroup` | `ScanlationGroup` | manga-specific (Phase 3); → ComicInfo.xml |
| `language` | `TranslatedLanguage` | BCP-47; manga-specific |
| `language` | `Languages` | mapped to `List<Language>` (multi-language fallback in `CleanupReleases`) |
| `sourceKey` | *(carried in Title/Indexer context)* | also used for per-source status (§7) |
| — | `IndexerId`, `Indexer` | stamped by `CleanupReleases` (the one gateway indexer's id/name) |
| — | `DownloadProtocol` | stamped `Http` |
| — | `IndexerPriority` | stamped from the indexer definition |
| `chapterNumber`, `volume`, `pageCount`, `mangaTitle` | *(advisory)* | Not first-class `ReleaseInfo` fields today; either (a) encode into `Title` so the existing parser recovers them, or (b) add optional fields to `ReleaseInfo`. **Recommendation (a):** keep `Title` authoritative so Mangarr's parser/decision-engine path is unchanged; treat structured fields as hints. `ChapterIds` is resolved later by Mangarr's parsing service, not sent by the gateway. |
| `ids.*` | *(unused on ReleaseInfo)* | manga external ids are matched via `MangaSearchCriteria.Manga`, not carried on the release |

**Design rule:** the gateway should produce a `title` that Mangarr's `MangaParser.ParseChapterTitle`
can parse (manga name + chapter number + optional group/language), because the decision engine parses
`Title`, not the structured hints. The structured fields are belt-and-suspenders / future-proofing.

---

## 7. Per-source status & auto-disable (the anti-bot payoff)

Mangarr already has **two** status axes: per-indexer (`IndexerStatusService`) and **per-source**
(`IIndexerSourceStatusService` / `IndexerSourceStatus` — a manga-specific addition). With the
combined gateway:

- **Per-indexer status** = is the gateway reachable at all (transport/auth). Drives the standard
  backoff ladder (`0,60,300,900,1800,3600,…` seconds, capped 24 h).
- **Per-source status** = is *this aggregator* healthy. The gateway reports per-source degradation in
  `/search`/`/recent` `warnings[]` and in `/caps` (`enabled:false`). Mangarr's `GatewayParser` reads
  the warnings and calls `IIndexerSourceStatusService.RecordFailure(sourceKey)` for the 4-step
  auto-disable escalation (the same mechanism Comix uses today for decrypt failures).

This means a single Cloudflare-blocked source doesn't disable the whole gateway indexer — exactly the
behavior the in-process `ComixIndexer` has now, preserved across the externalization.

**Anti-bot ownership (the whole point of the spike):** `ComixPlaywrightSigner` (embedded Chromium,
token capture, response decrypt) and `CloudflareClearanceService` (FlareSolverr/Byparr) **move into
the gateway**. Mangarr stops shipping/operating headless Chromium. The gateway is the single place
that solves challenges, holds cookies/UA, and decrypts responses — and because of R1, that same
session serves both search and download. See 003 §"What to remove".

---

## 8. The gateway search API surface (summary)

Base path `{/UrlBase}/api/v1`. Auth `X-Api-Key`. JSON.

| Method | Path | Maps to | Purpose |
|---|---|---|---|
| `GET` | `/caps` | caps provider + `gatewaySources` action | sources, supported params, categories/langs, limits |
| `POST` | `/search` | `Fetch(MangaSearchCriteria)` / `Fetch(ChapterSearchCriteria)` | manga/chapter search across sources |
| `GET` | `/recent` | `FetchRecent()` | RSS-sync feed |
| `GET` | `/version` (shared with 001) | `Test()` | connectivity/version |

(The download endpoints `POST/GET/DELETE /downloads`, `GET /status` live in
**[001 §6](../001-external-download-client/README.md)** — same process, same auth, same base path.)

Full machine-readable contract: **[`../manga-gateway.openapi.yaml`](../manga-gateway.openapi.yaml)**.

---

## 9. Error model (mirror Newznab's, JSON-ified)

Per-source soft failures → `warnings[]` (don't fail the call). Hard failures → HTTP status + body:

```json
{ "error": { "code": "auth", "message": "Invalid API key" } }
```
| `code` | HTTP | Mangarr mapping |
|---|---|---|
| `auth` | 401/403 | `ApiKeyException` → `Test()` auth failure |
| `rate_limited` | 429 | `TooManyRequestsException` (+ `Retry-After`) → backoff |
| `source_unavailable` | 200 + warning | per-source `RecordFailure(sourceKey)` |
| `bad_request` | 400 | `IndexerException` |
| `internal` | 5xx | `IndexerException` → `RecordConnectionFailure` |

These map onto the exception ladder `HttpIndexerBase.FetchReleases` already handles
(`TooManyRequestsException`, `ApiKeyException`, `RequestLimitReachedException`,
`CloudFlareCaptchaException`, `HttpException`, `IndexerException`), so no new handling is needed
Mangarr-side beyond translating JSON `error.code` → the right exception in `GatewayParser`/proxy.

---

## 10. Edge cases

| Case | Required behavior |
|---|---|
| Source returns 0 results vs. source errored | Distinguish: empty `releases[]` (fine) vs. `warnings[]` entry (degraded). |
| Title-only source (Comix, no external ids) | Gateway matches on `query` + `ids` it understands; ignores ids it can't use. |
| Same chapter from multiple sources | Each yields a distinct `guid`; Mangarr de-dupes by Guid and ranks via decision engine + custom formats. Gateway should NOT pre-merge. |
| Decimal/fractional chapters (12.5, 1.123) | `chapter` is a JSON number; preserve ≥3 decimal places (Mangarr stores DECIMAL(10,3)). |
| Language variants (`es` vs `es-la`) | Gateway returns BCP-47; Mangarr's `MultiLanguages` fallback handles mapping. |
| Caps changes (source added/removed) | Caps cache TTL bounds staleness; `gatewaySources` UI action refetches. |
| `downloadHandle` expiry before grab | Handle TTL ≥ Mangarr's 30-min interactive cache; on expired-handle submit, 001's `POST /downloads` returns a rejection → Mangarr re-searches. |

---

## 11. Open questions

1. **Caps cache TTL** — Sonarr uses 7 days for Torznab. Manga sources change behavior more often
   (anti-bot). Recommend 6–12 h, with `gatewaySources` UI action forcing a refetch.
2. **One gateway indexer vs. one-per-source in the Mangarr UI.** R1 says one indexer connection.
   But users may want per-source enable/priority. Resolved via the `EnabledSources` multiselect +
   per-source status, keeping a single `IndexerDefinition`. Confirm this is enough, or whether the
   gateway should also let Mangarr set per-source priority (could ride in the search request).
3. **Search vs. browse semantics** — does `/search` with no `query` and no `ids` mean "everything"
   (browse) or is it rejected? Recommend: require at least `query` or one `id` for `/search`;
   "everything-recent" is what `/recent` is for.
4. **Should `chapterNumber/volume/scanlationGroup` become first-class `ReleaseInfo` fields?**
   (Recommendation in §6: no — keep `Title` authoritative; add only if the parser proves lossy.)

---

## Investigation Trail

- Confirmed Mangarr's `IIndexer` is already manga-shaped (only `MangaSearchCriteria`/`ChapterSearchCriteria` overloads; TV criteria stripped) — the `GatewayIndexer` is a provider addition, no contract change.
- Confirmed the manga base chain (`HttpAggregatorBase`→`HttpIndexerBase`→`IndexerBase`) + `FetchReleases` + per-source `IndexerSourceStatusService` are kept; the gateway indexer reuses all of it.
- Key simplification from R1: `GatewayIndexer` can drop `IHttpAggregator`/`GetChapterPages` (page-fetch is the download surface's job in the same process), extending `HttpIndexerBase` directly. This removes the `Download()`→`IHttpAggregator` cast coupling (tracked in 003).
- Confirmed via `v5-develop` that the Torznab model = caps-handshake + gated-search + extension-attr release bag; we keep the *shape* and swap XML→JSON, TV-params→manga-params (R2).
- The anti-bot relocation (Comix Playwright signer + Cloudflare clearance) is the central win: one external session for both search and download, no headless Chromium in Mangarr.

## Results

**Verdict: DRAFTED.** A single gateway indexer with a JSON caps→search→recent contract maps cleanly
onto Mangarr's kept `IIndexer`/`HttpIndexerBase`/`CleanupReleases`/per-source-status machinery. The
external project builds ~3 endpoints (`/caps`, `/search`, `/recent`) plus the shared `/version`. The
manga query vocabulary (`q + external ids + chapter(decimal) + volume + language + source filter`)
replaces Torznab's TV params. The decisive benefit — moving all Cloudflare/Comix-decrypt/headless-
Chromium out of Mangarr into one shared gateway session — is fully supported by the existing
per-source status design. No blocker found.
