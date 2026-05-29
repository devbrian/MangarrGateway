# GSD Bootstrap Prompt — Manga Gateway (greenfield)

> **How to use:** Create the new greenfield repo, copy this entire `handoff/` folder into its root,
> then paste the prompt below into Claude Code / GSD from that repo. (The prompt uses `./handoff/`
> relative paths, so the bundle must sit at the repo root.)

---

We're bootstrapping a brand-new, greenfield project: the **Manga Gateway**.
It's an external service that Mangarr (a manga library manager — a Sonarr fork) talks to over
HTTP/JSON. The complete, authoritative spec is in the hand-off bundle at ./handoff/. Before doing
anything else, read, in this order:
  1. ./handoff/README.md          (index + scope + the open decisions)
  2. ./handoff/MANIFEST.md        (the idea + locked requirements R1–R6)
  3. ./handoff/manga-gateway.openapi.yaml   (the CONTRACT OF RECORD — when prose and this disagree, this wins)
  4. ./handoff/002-universal-indexer/README.md   (search surface)
  5. ./handoff/001-external-download-client/README.md   (download surface)
(The C# blocks in 001/002 are CONTEXT ONLY — how our API gets called — NOT something we implement.
Spike 003, the Mangarr-side reintegration plan, is intentionally NOT in this bundle; it lives in
the Mangarr repo and we build none of it.)

What we're building, in one breath: a SINGLE service exposing TWO JSON-REST API surfaces that
share ONE authenticated secure-site session and ONE anti-bot/Cloudflare solver —
  • a SEARCH/indexer surface (Jackett/Torznab analog) that fans out to many manga aggregator sites
    (MangaDex, Comix, …) and returns normalized releases, and
  • a DOWNLOAD surface (SABnzbd/qBittorrent analog) that fetches a chapter's page images, packages
    them, and reports queue/progress/completion.
The gateway owns everything Mangarr must NOT: site sessions, Cloudflare/headless-browser challenge
solving, per-source rate limiting, release→manifest resolution, image fetching.

Hard constraints (from MANIFEST.md):
  • R1 — one combined process; the search session is reused by the download surface.
  • R2 — JSON over HTTP(S), API-key auth. Torznab is the conceptual model only, NOT the wire format.
  • R5 — the job id returned by POST /downloads is the stable join key (queue → history).
  • R6 — release → manifest → per-image fetch happens inside the gateway; the manifest token never
    round-trips through Mangarr.
  • The OpenAPI file is the contract Mangarr already expects — implement it faithfully.

Scope fence (do NOT cross): this repo is the GATEWAY ONLY. The bundle's specs also describe the
Mangarr-side consumer (a C# IDownloadClient/IIndexer and its monitoring loop) — that is context for
how our API gets called, NOT something we implement. Specifically: 001 §2–§3 and 002 §2 and all of
003 are Mangarr-side; we build 001 §4–§9 and 002 §3–§10. There is no Mangarr C# code in this
project. Source paths like `src/NzbDrone.Core/...` are provenance citations into the Mangarr repo
(not included), not files to open.

Before scaffolding, get my decision on the open questions the bundle flags — especially:
  1. Tech stack (the bundle is language-agnostic; the work is heavy web automation + headless-browser
     challenge solving + image fetching — recommend Python or Node, but ask me).
  2. Folder-vs-CBZ delivery (recommend gateway returns a folder; Mangarr archives).
  3. Whether MangaDex is in-scope as a no-anti-bot source for v1.
  4. Auth/bind model and the /caps + downloadHandle TTLs (downloadHandle must be ≥ 30 min).

Then bootstrap the project: run `/gsd-ingest-docs ./handoff` to seed .planning/ from these specs,
and propose a roadmap that delivers the search surface first (the download surface consumes the
downloadHandle that search returns). Ask me anything ambiguous rather than guessing.
