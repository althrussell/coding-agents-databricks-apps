# Deploy to Databricks Apps

> **This is the lab fork** of [`databrickslabs/coding-agents-databricks-apps`](https://github.com/databrickslabs/coding-agents-databricks-apps). It deploys exactly like upstream for single-user use, and additionally supports **workspace-wide auth** + a **lean lab profile** for large-scale, auto-deployed labs. For the lab path, jump to [Large-scale labs (auto-deploy)](#large-scale-labs-auto-deploy). For the rationale, see [About this fork](../README.md#about-this-fork).

## Prerequisites

- A Databricks workspace with Model Serving endpoints enabled

## Easy Start (Git Repo)

The simplest way — no CLI, no cloning, everything stays in the Databricks UI.

1. Go to **Databricks → Apps → Create App**
2. Choose **Custom App** and connect this Git repo:
   ```
   https://github.com/althrussell/coding-agents-databricks-apps.git
   ```
3. Click **Deploy**
4. Open the app — on first terminal session, paste a short-lived PAT when prompted

The app pulls the code directly from Git. To update later, just re-deploy — it picks up the latest from the repo.

> **Note:** On first startup, the app automatically removes the template's `.git` history and reinitializes a clean, remote-free git repo. This prevents accidental pushes back to the template repo from the in-browser terminal.

> **Optional (Highly Recommended):** If you use [Databricks AI Gateway](https://docs.databricks.com/aws/en/ai-gateway/), also add `DATABRICKS_GATEWAY_HOST` as a secret or environment variable. Otherwise the app falls back to direct model serving endpoints.

## Alternative: Deploy with CLI

If you prefer working from the terminal or need more control:

### 1. Clone the repo into your workspace

```bash
databricks repos create \
  --url https://github.com/althrussell/coding-agents-databricks-apps.git \
  --path /Workspace/Users/<your-email>/apps/coding-agents-databricks-apps
```

### 2. Configure `app.yaml`

In the cloned workspace folder, copy the template and edit it:

```bash
cp app.yaml.template app.yaml
```

Set your `DATABRICKS_GATEWAY_HOST`, or remove the gateway lines to fall back to direct model serving endpoints.

### 3. Create the app and deploy

```bash
databricks apps create <your-app-name>
```

No secrets or resources to configure. On first terminal session, paste a short-lived PAT when prompted — all CLIs are configured automatically.

### 4. Deploy

```bash
databricks apps deploy <your-app-name> \
  --source-code-path /Workspace/Users/<your-email>/apps/coding-agents-databricks-apps
```

> **Tip:** To update later, just `git pull` in the workspace repo and re-deploy.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABRICKS_TOKEN` | No | Optional. If not set, the app prompts for a token on first session. Auto-rotated every 10 minutes |
| `HOME` | Yes | Set to `/app/python/source_code` in app.yaml |
| `ANTHROPIC_MODEL` | No | Claude model name (default: `databricks-claude-opus-4-7`) |
| `CODEX_MODEL` | No | Codex model name (default: `databricks-gpt-5-5`) |
| `GEMINI_MODEL` | No | Gemini model name (default: `databricks-gemini-2-5-pro`) |
| `HERMES_MODEL` | No | Hermes model name (default: `databricks-claude-opus-4-7`) |
| `DATABRICKS_GATEWAY_HOST` | No | AI Gateway URL override. Auto-discovered from `DATABRICKS_WORKSPACE_ID` if unset. Falls back to direct model serving if neither is available |
| `CODA_AUTH_MODE` | No | Who may use the app: `owner` (default, single user), `allowlist` (explicit emails), or `workspace` (any authenticated workspace user — for labs). See [Authorization modes](#authorization-modes) |
| `CODA_PROFILE` | No | Agent footprint preset: **`lab` (default — lean: Claude + AppKit + Databricks core only, guided coach on)** or `full` (all agents). Unset resolves to `lab`. Set `full` explicitly for the power-user build. See [docs/lab-build.md](lab-build.md) |
| `ENABLE_CODEX` / `ENABLE_OPENCODE` / `ENABLE_GEMINI` / `ENABLE_HERMES` | No | Per-agent install toggles. An explicit value always overrides `CODA_PROFILE`. Unset ⇒ governed by profile (off in `lab`, on in `full`) |
| `LAKEBASE_INSTANCE_NAME` | No | Name for the on-demand Lakebase instance (default `coda-lab`). **No DB is provisioned at boot** — one is created/reused only when a built app needs persistence (`scripts/lakebase_ensure.py`) |
| `APPKIT_VERSION` | No | Pin a fleet-wide AppKit version so every attendee scaffolds an identical app (Control Tower injects this for consistency). Unset ⇒ cooldown-respected latest stable |
| `LAB_RESOURCE_TAGS` | No | `k=v,k2=v2` tags applied as `custom_tags` to on-demand Lakebase instances for cost attribution / teardown (Control Tower injects this) |
| `MLFLOW_TRACING_ENABLED` | No | Set to `"true"` to enable MLflow tracing for Claude, Codex, and Gemini (default `"false"`) |

## Authorization modes

`CODA_AUTH_MODE` controls who may use a deployment (enforced centrally across
HTTP, WebSocket, and the PAT-setup gate):

| Mode | Who's allowed | Use for |
|------|---------------|---------|
| `owner` (default) | The app owner (`app.creator`) plus any emails in `CONTROL_TOWER_AUTHORIZED_EMAILS` / `AUTHORIZED_EMAILS` / `DATABRICKS_APP_AUTHORIZED_EMAIL` | Individual / dogfood use |
| `allowlist` | Only the explicit allowlist emails (owner resolution not required) | SP-deployed apps with a known attendee set |
| `workspace` | **Any** authenticated workspace user | Large-scale labs: one isolated instance per attendee in their own workspace |

In `workspace` mode the app no longer needs per-attendee source patching — the
deployer just sets the env var. See [docs/lab-build.md](lab-build.md) for the
full lab contract.

## Security Model

The default is a **single-user, zero-config auth** app. No secrets or tokens are required at deploy time.

1. **Owner resolution**: The app owner is determined from `app.creator` via the service principal + Apps API — no PAT needed
2. **Authorization**: Resolved by `CODA_AUTH_MODE` (default `owner`) — each request's `X-Forwarded-Email` header is checked against the allowed set. Non-matching users see 403 (except in `workspace` mode, which admits any authenticated workspace user)
3. **Interactive PAT setup**: On first terminal session, the user pastes a short-lived PAT interactively. All enabled CLIs (Claude, Codex, OpenCode, Gemini, Hermes, Databricks) are configured automatically
4. **Auto-rotation**: PAT rotates every 10 minutes with a 15-minute lifetime. Old tokens are proactively revoked. Maximum leaked-token exposure: 15 minutes
5. **Session-aware**: Rotation is skipped when no active terminal sessions exist
6. **On restart**: The user re-pastes a token (no persistence by design)

## Large-scale labs (auto-deploy)

For lab fleets, deploy with workspace-wide auth and the lean profile via the
idempotent SDK path (the same one Control Tower uses):

```bash
make lab-deploy PROFILE=<profile> APP_NAME=coda-lab
make lab-verify PROFILE=<profile> APP_NAME=coda-lab
```

This sets `CODA_AUTH_MODE=workspace` + `CODA_PROFILE=lab` on the deployment —
no source edits, no per-attendee patching. `scripts/lab_deploy.py` is
idempotent and safe to retry. Full details: [docs/lab-build.md](lab-build.md).

### App persistence is on-demand (no clicks)

CoDA never provisions a database at boot. When the agent builds an app that
needs persistence, it runs `scripts/lakebase_ensure.py` to create (or reuse) a
single Lakebase instance per lab and binds it non-interactively — so attendees
never hit the "missing required resource Postgres" prompt or click resources in
the UI. Read-only apps skip Lakebase and incur no DB cost. The deploying
identity needs the database-create entitlement; see [docs/lab-build.md](lab-build.md#app-persistence-on-demand-lakebase-no-ui-clicks).

### Control Tower owns the fleet; CoDA owns the instance

The boundary is deliberate: **Control Tower** provisions attendee workspaces,
deploys CoDA, and tears the fleet down. **CoDA** does not ship fleet-management
scripts. Instead it exposes hooks Control Tower drives via env:

- `APPKIT_VERSION` — pin one AppKit version across the whole fleet so every
  attendee scaffolds an identical app.
- `LAB_RESOURCE_TAGS` — tags stamped onto any on-demand Lakebase instance, plus
  the deterministic `LAKEBASE_INSTANCE_NAME`, so Control Tower can attribute
  cost and tear instances down by tag/name with zero CoDA-side bookkeeping.

## Gunicorn Configuration

Production uses Gunicorn (`gunicorn.conf.py`) with:
- `workers=1` — PTY file descriptors and in-memory session state can't survive forking
- `threads=8` — Handles concurrent polling from the terminal client
- `worker_class=gthread` — Single process + thread pool
- `post_worker_init` hook calls `initialize_app()` to start setup

## Workspace Sync

Git commits automatically sync projects to Databricks Workspace:

```
/Workspace/Users/{email}/projects/{project-name}/
```

The post-commit hook uses `nohup ... & disown` to ensure the sync process survives across all coding agents, since some agents kill the entire process group when a shell command finishes.
