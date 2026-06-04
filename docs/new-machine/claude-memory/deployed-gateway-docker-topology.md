---
name: deployed-gateway-docker-topology
description: "How/where the MangarrGateway docker instance is deployed — remote host, ports, mounts, redeploy command"
metadata: 
  node_type: memory
  type: project
  originSessionId: d7d50855-1e73-4053-8bd5-01a8d2bd7b5e
---

The deployed MangarrGateway runs on a **remote Linux host via docker context `local-remote` (`ssh://llm@192.168.0.246`)**, NOT the local Windows dev box. So `docker` CLI commands here target that remote daemon.

- Container: `mangarrgateway-gateway-1`, published on **`192.168.0.246:9191`** (compose project `mangarrgateway`). `localhost:9191` from the Windows machine is **refused** — hit the host IP.
- Deployed from the **base file only** (`docker-compose.yml`, NOT the dev `docker-compose.override.yml`), so application code is **baked into the image** — a code change requires a **rebuild**, not just a restart.
- Mounts (host binds, set via `.env`): `/opt/mangarrgateway` → `/state` (holds `config.toml` with the auto-generated `api_key`, `gateway.db`, `cf_clearance`), and `/mnt/mediatrial/mangarr_dl` → `/data/manga` (CBZ output).
- Redeploy (preserves state/API key — `.env` resolves the same binds): `docker compose -f docker-compose.yml up --build -d gateway`.
- The API key for hitting the deployed API lives at `/state/config.toml` inside the container (`docker exec … cat /state/config.toml`). Do not store its value here.

Related: nightly live-smoke is on a GitHub Actions **datacenter IP** (separate from this residential/remote deploy) — see [[run-live-tests-locally-before-push]].
