# New-Machine Setup — MangarrGateway

Replicate the full dev environment **and the Claude Code project memory** on a new
machine (these notes assume the new machine is a Mac).

Two categories of state matter:

| Category | Where it lives | Travels with `git clone`? |
|----------|----------------|---------------------------|
| Repo code + planning | the repo: `CLAUDE.md`, `.planning/`, `.claude/skills/`, `src/`, `tests/`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `docker-compose.yml` | ✅ yes |
| **Claude project memory** | **`~/.claude/projects/<project-key>/memory/`** (OUTSIDE the repo) | ❌ no — bundled here in [`claude-memory/`](./claude-memory/) |
| GSD runtime + skills | `~/.claude/get-shit-done/` + `~/.claude/skills/gsd-*` (global) | ❌ no — reinstall via `npx` |
| Secrets | `config.toml` (API key), `.env` | ❌ no — gitignored; recreate |

> The repo mount on the new laptop (`/Users/brian/Remote/Desktop/MangarrGateway/MangarrGateway`)
> points at the **old** machine's repo dir. Use it to copy the bundled
> `docs/new-machine/claude-memory/` files across — the live memory dir
> (`~/.claude/projects/...`) is NOT under that mount, which is exactly why a
> snapshot is bundled into the repo.

---

## 0. Prerequisites

Install on the new machine:

```bash
# uv (Python toolchain + venv + lockfile) — pin/verify 0.11.x
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node.js 20+ (only needed to run the GSD installer via npx) — e.g. via Homebrew
brew install node            # node --version  -> v20+ (v24 used on the source machine)

# Git, and Docker Desktop (only if you'll build/deploy images from this machine)
brew install git
brew install --cask docker
```

The repo pins **Python 3.12** (`requires-python = ">=3.12"`, `.python-version` = 3.12);
`uv` installs and manages it for you in step 2 — you do NOT need a system Python 3.12.

---

## 1. Get the repo

Clone fresh to a real local path (don't develop on the network mount):

```bash
git clone https://github.com/devbrian/MangarrGateway.git ~/Desktop/MangarrGateway
cd ~/Desktop/MangarrGateway
```

This already includes `CLAUDE.md`, the entire `.planning/` history, the 3 project
skills under `.claude/skills/`, and `docs/new-machine/` (this file + the memory bundle).

---

## 2. Python environment

```bash
uv python install 3.12      # uv-managed 3.12 (matches CI + the Docker image)
uv sync --dev               # creates .venv, installs all deps from uv.lock
uv run patchright install chromium   # the Cloudflare solver browser (Patchright/Chromium)
# Camoufox is the opt-in fallback only — `uv run camoufox fetch` (~200 MB) if ever needed.
```

Verify the toolchain is wired:

```bash
uv run nox -s gate          # ruff + ruff format --check + mypy + pytest (whole repo)
```

> Heads-up: the gate is **slow** — ~16 min locally, ~75–82 min on CI. That's normal,
> not stuck (see the `ci-gate-normal-duration-75min` memory). A run that dies in
> <2 min is a collection crash, not slowness.

---

## 3. Install GSD (Get-Shit-Done) globally

GSD's runtime + the `gsd-*` slash-command skills live in `~/.claude/`, not the repo.
Reinstall them from the repo root:

```bash
cd ~/Desktop/MangarrGateway
npx -y @opengsd/gsd-core@latest --claude --local
```

This restores `~/.claude/get-shit-done/` (the `gsd-tools.cjs` runtime the workflows
call) and the `~/.claude/skills/gsd-*` skills (`/gsd-execute-phase`, `/gsd-plan-phase`,
`/gsd-progress`, etc.). Confirm with `/gsd-progress` inside Claude Code, or:

```bash
ls ~/.claude/get-shit-done/bin/gsd-tools.cjs && ls ~/.claude/skills | grep -c '^gsd-'
```

---

## 4. Seed the Claude project memory  ← the part that doesn't come with `git clone`

The accumulated memory (workflow rules, project facts) lives at
`~/.claude/projects/<project-key>/memory/`. The `<project-key>` is the repo's
**absolute path** with `/`, `\`, `:`, `.` replaced by `-`. Examples:

- old machine: `C:\Users\jones\Desktop\Mangarr\MangarrGateway` → `C--Users-jones-Desktop-Mangarr-MangarrGateway`
- new machine: `/Users/brian/Desktop/MangarrGateway` → `-Users-brian-Desktop-MangarrGateway`

**Easiest, key-guess-free method:**

1. Open Claude Code once in the cloned repo (`cd ~/Desktop/MangarrGateway && claude`),
   say anything, exit. This auto-creates `~/.claude/projects/<project-key>/`.
2. Copy the bundled memory snapshot into that project's `memory/`:

```bash
# find the auto-created project dir (newest match for this repo)
PROJ=$(ls -dt ~/.claude/projects/*MangarrGateway* | head -1)
mkdir -p "$PROJ/memory"
cp ~/Desktop/MangarrGateway/docs/new-machine/claude-memory/*.md "$PROJ/memory/"
ls "$PROJ/memory"      # MEMORY.md + 10 memory files
```

> If you only have the network mount (not a fresh clone yet), copy from the mount:
> `cp /Users/brian/Remote/Desktop/MangarrGateway/MangarrGateway/docs/new-machine/claude-memory/*.md "$PROJ/memory/"`

`MEMORY.md` is the index loaded each session; the other 10 files are the individual
memories it points to. The bundled set (snapshot at last sync):

- `branch-and-pr-never-commit-to-main` — every change reaches `main` via a reviewed PR
- `prefer-merge-commits-no-squash` — merge PRs with `--merge`, never `--squash`
- `pr-comment-reply-and-resolve-individually` — reply to each review comment individually + resolve
- `ci-gate-run-full-nox-before-push` — run `uv run nox -s gate` (whole repo) before pushing
- `ci-gate-normal-duration-75min` — CI gate ~75–82 min is normal, not stuck
- `run-live-tests-locally-before-push` — changes to sources/antibot/solver_lifecycle/fanout/warm need a local `-m live` run
- `live-conftest-solver-kwargs-mirror-drift` — keep `tests/live/conftest.py` solver kwargs in sync with `app.py`
- `cloudflare-engine-default-patchright` — Patchright/Chromium is the default CF engine; camoufox is the opt-in fallback
- `deployed-gateway-docker-topology` — runs on the REMOTE host `192.168.0.246:9191` (docker context), code baked in the image
- `github-issues-single-source-of-truth` — track deferred items as GitHub issues

> Keeping it in sync later: memory is a living store. To re-snapshot from the source
> machine, re-copy its `~/.claude/projects/C--Users-jones-Desktop-Mangarr-MangarrGateway/memory/`
> into `docs/new-machine/claude-memory/` and re-run this step.

---

## 5. Secrets & local config (not in git)

- **`config.toml`** (gitignored) holds the gateway `api_key`. On a fresh run the app
  auto-generates and persists one; for the **deployed** gateway it lives at
  `/state/config.toml` on the remote host. Only needed if you run the gateway locally
  or call its authenticated endpoints — copy the value from the old machine if you want
  the same key.
- **`.env`** (gitignored) — start from the committed `.env.example`.

---

## 6. (Optional) Remote deploy from the new machine

The gateway runs on a **remote** Docker host (not localhost). To build/deploy from the
new machine, recreate the docker context and run the same compose command:

```bash
docker context create local-remote --docker "host=ssh://llm@192.168.0.246"
docker context use local-remote
# (ensure SSH key auth to llm@192.168.0.246 works first)

cd ~/Desktop/MangarrGateway
docker compose -f docker-compose.yml up --build -d gateway   # builds on the remote, recreates the container
```

Verify: `curl -o /dev/null -w '%{http_code}\n' http://192.168.0.246:9191/admin/metrics/v1/summary`
→ `401` (route exists, auth-gated). Reaches `404` only if old code is still running.

---

## 7. (Optional) Global Claude config

A `~/.claude/settings.json` exists on the source machine (global permissions/hooks/env —
**not** project-specific). It affects every project, so copy it across only if you want
the same global Claude Code behavior; otherwise skip. MCP servers (playwright, context7,
firecrawl, exa) are likewise global Claude config, not part of this repo.

---

## Quick checklist

- [ ] uv + node + (docker) installed
- [ ] `git clone` → `uv python install 3.12` → `uv sync --dev` → `uv run patchright install chromium`
- [ ] `uv run nox -s gate` is green
- [ ] `npx -y @opengsd/gsd-core@latest --claude --local` (GSD restored; `/gsd-progress` works)
- [ ] memory copied into `~/.claude/projects/<key>/memory/` (11 files incl. `MEMORY.md`)
- [ ] secrets handled (`config.toml` / `.env`) — only if running the gateway locally
- [ ] (optional) `local-remote` docker context recreated for deploys
