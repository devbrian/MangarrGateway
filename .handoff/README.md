# Manga Gateway — Hand-off Bundle

This folder is the complete, authoritative spec for a **new, greenfield project: the Manga
Gateway**. It is everything an external team needs to build the service without access to the
Mangarr codebase.

> **One breath:** Build a SINGLE service exposing TWO JSON-REST API surfaces that share ONE
> authenticated secure-site session and ONE anti-bot/Cloudflare solver —
> a **search/indexer surface** (Jackett/Torznab analog) that fans out to many manga aggregator
> sites and returns normalized releases, and a **download surface** (SABnzbd/qBittorrent analog)
> that fetches a chapter's page images, packages them, and reports queue/progress/completion.

The gateway owns everything Mangarr (a manga library manager — a Sonarr fork) must NOT: site
sessions, Cloudflare/headless-browser challenge solving, per-source rate limiting,
release→manifest resolution, and image fetching.

---

## Read these, in this order

1. **`README.md`** — this file (index + scope + the open decisions).
2. **`MANIFEST.md`** — the idea + the locked, non-negotiable requirements **R1–R6**.
3. **`manga-gateway.openapi.yaml`** — the **CONTRACT OF RECORD**. When prose and this file
   disagree, **this file wins**. Implement it faithfully — it is the exact API Mangarr already
   expects to call.
4. **`002-universal-indexer/README.md`** — the **search** surface (`/caps`, `/search`, `/recent`).
5. **`001-external-download-client/README.md`** — the **download** surface
   (`POST/GET/GET{id}/DELETE /downloads`, `/status`, `/version`).

> **Not in this bundle:** spike 003 (Mangarr-Reintegration) — the Mangarr-side restore of the
> poll→track→complete/fail→import→queue loop that *drives* this gateway. It is entirely Mangarr-side
> (C# in the Mangarr repo) and **intentionally excluded** from this gateway-only hand-off. You build
> none of it; 001/002 reference it only to explain how your API gets called.

---

## Scope fence (do NOT cross)

This repo is the **GATEWAY ONLY**. The bundle's specs also describe the *Mangarr-side consumer*
(a C# `IDownloadClient` / `IIndexer` and its monitoring loop). That is **context for how our API
gets called, NOT something we implement**. There is no Mangarr C# code in this project.

| Document | What WE build (gateway) | What is Mangarr-side (context only — do NOT build) |
|----------|--------------------------|-----------------------------------------------------|
| **001 — download** | §4–§9 (the download API: endpoints, lifecycle, status vocabulary, edge cases) | §2–§3 (Mangarr's `GatewayDownloadClient`, settings, proxy) |
| **002 — search** | §3–§10 (the caps/search/recent API, release object, per-source status, error model) | §2 (Mangarr's `GatewayIndexer`, settings, parser) |
| **003 — reintegration** | *(nothing — excluded from this bundle)* | the entire document (Mangarr's poll→track→complete/fail→import→queue loop), in the Mangarr repo |

A practical tell: anything written in **C#** in these specs is the Mangarr side and is provenance,
not your deliverable. Your deliverables are the **JSON endpoints** and the **behavior** behind them.

### Note on source-path citations

The spike documents cite `src/NzbDrone.Core/...` paths as **evidence/provenance** from the
Mangarr repository (a Sonarr fork). Those files are **not included** in this bundle and are not
yours to open — they exist to justify why the contract is shaped the way it is. The binding
artifact for you is `manga-gateway.openapi.yaml` plus the behavioral prose in 001/002.

---

## The locked requirements (full text in `MANIFEST.md`)

- **R1 — One combined gateway process.** Search + download share one authenticated secure-site
  session and one anti-bot solver. The session that *finds* a release is the session that
  *downloads* it.
- **R2 — JSON REST over HTTP(S), API-key auth.** Torznab/Newznab is the **conceptual model only**
  (caps → gated search → normalized release list), NOT the wire format. No XML/RSS.
- **R3** — Mangarr registers the gateway as one indexer + one download client (both
  `DownloadProtocol.Http`, same host). *(Mangarr-side; context.)*
- **R4** — Mangarr restores its monitoring/import loop. *(Mangarr-side; spec'd in 003; context.)*
- **R5 — `DownloadId`/job id is the join key.** The id your `POST /downloads` returns must be
  stable for the life of the job (queue → history) and is what Mangarr uses to track it.
- **R6 — release → manifest → per-image fetch.** A "download" is a page manifest the gateway
  resolves and fetches; because of R1 the manifest token never round-trips through Mangarr.

---

## Open decisions — get the user's call BEFORE scaffolding

The specs flag these. Resolve them with the user up front (the bootstrap prompt will ask):

1. **Tech stack.** The bundle is language-agnostic. The work is heavy web automation +
   headless-browser challenge solving + image fetching — **recommend Python or Node**, but ask.
2. **Folder-vs-CBZ delivery.** Does the gateway archive to CBZ, or deliver a *folder of images*
   and let Mangarr archive? **Recommendation: gateway returns a folder; Mangarr archives** (keeps
   ComicInfo.xml generation, which needs Mangarr's metadata, on the Mangarr side). See 001 §11.1.
3. **MangaDex in scope** as a no-anti-bot source for v1? (Comix is the Cloudflare/encrypted case.)
4. **Auth/bind model + TTLs.** Static API key + localhost bind (recommended, matches the *arr
   ecosystem) vs. per-request HMAC. Pin the `/caps` cache TTL (rec. 6–12 h) and the
   `downloadHandle` TTL (must be **≥ 30 min** — Mangarr caches interactive-search releases 30 min;
   001 §9 / 002 §10).

---

## Suggested build order

Deliver the **search surface first** — the download surface consumes the `downloadHandle` that
search returns (002 §4 → 001 `POST /downloads`). A natural slicing:

1. Service skeleton + auth + `GET /version` + `GET /caps` (one source, e.g. MangaDex, `antibot: none`).
2. `POST /search` + `GET /recent` for that one source → normalized release list.
3. Add the anti-bot source (Comix: Cloudflare + encrypted-response headless session).
4. Download surface: `POST /downloads` (resolve handle → manifest → fetch images → package) +
   `GET /downloads` polling + `GET /status` + `DELETE /downloads/{id}`.
5. Per-source status/degradation reporting (`warnings[]`, `enabled:false`), retention windows, TTLs.

---

## Bootstrap

See **`BOOTSTRAP-PROMPT.md`** in this folder — paste it to GSD in the new greenfield repo to seed
`.planning/` from these specs (`/gsd-ingest-docs ./handoff`) and propose a roadmap.
