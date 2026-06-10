# Lab build: lean footprint, workspace auth, and the Control Tower contract

## Why this fork exists

This repo is a fork of [`databrickslabs/coding-agents-databricks-apps`](https://github.com/databrickslabs/coding-agents-databricks-apps).
Upstream CoDA is single-user; to run it across a large lab fleet, an
orchestrator previously had to **patch the app source per attendee** to inject
their identity into the authorization check. This fork removes that need: it
adds a deployment-time `CODA_AUTH_MODE=workspace` so any authenticated
workspace user is admitted, plus a lean `CODA_PROFILE=lab` footprint and an
idempotent SDK deploy path. [Control Tower](https://github.com/althrussell/databricks-labs-control-tower)
deploys this fork **unmodified** and sets two env vars — nothing else.

## Two footprints, one source tree

CoDA ships in two footprints from the **same source tree** — no per-lab fork,
no deploy-time source patching. A single deployment-time env var
(`CODA_PROFILE`) selects between them, and a second (`CODA_AUTH_MODE`) selects
who may use the app. This page explains what the **lab** profile drops, how
the **full** profile differs, and the contract Control Tower relies on.

## TL;DR

| Concern              | Full build (default)                       | Lab build                                  |
| -------------------- | ------------------------------------------ | ------------------------------------------ |
| `CODA_PROFILE`       | `full` (or unset)                          | `lab`                                      |
| Coding agents        | Claude, Codex, OpenCode, Gemini, Hermes    | **Claude only** (others off)               |
| App builder          | AppKit + Lakebase (default)                | AppKit + Lakebase (default)                |
| Content-filter proxy | on (OpenCode needs it)                     | off (no OpenCode)                          |
| `CODA_AUTH_MODE`     | `owner` (single user)                      | `workspace` (any authenticated user)       |
| Set by               | `make deploy`                              | `scripts/lab_deploy.py` / `make lab-deploy`|

## Why a lean profile

Large-scale labs spin up **one CoDA instance per attendee**. Every extra
coding-agent CLI installed at boot is wall-clock setup time and image weight
multiplied by the attendee count. The lab profile keeps the boot path to the
essentials an attendee actually needs in a guided lab — Claude Code plus the
AppKit + Lakebase app-build path — and skips the rest.

The full build stays the default for individual / dogfood use, where having
every agent available matters more than boot time.

## What the lab profile drops

`CODA_PROFILE=lab` flips the **default** for every *toggleable* agent to off:

- **Codex** (`ENABLE_CODEX`)
- **OpenCode** (`ENABLE_OPENCODE`) — and with it the content-filter **proxy**,
  which only OpenCode needs
- **Gemini** (`ENABLE_GEMINI`)
- **Hermes** (`ENABLE_HERMES`)

These are **always installed** regardless of profile (they are core):

- **Claude Code** (`setup_claude.py`)
- **AppKit** runtime + version pin + npm precache (`setup_appkit.py`,
  `install_node.sh`)
- **Databricks CLI** auth (`setup_databricks.py`)
- Base tooling: `git` config + workspace-sync hook, `micro`, `gh`, Databricks
  CLI upgrade, Node v22+.

In the setup UI, disabled agents are shown as **skipped** (not stuck pending),
so attendees see an honest, complete-looking boot.

### Per-agent overrides win over the profile

An explicit `ENABLE_<AGENT>` value always beats the profile default. So you can
run a lean lab that *also* keeps Gemini, for example:

```yaml
- name: CODA_PROFILE
  value: "lab"        # Codex / OpenCode / Hermes off
- name: ENABLE_GEMINI
  value: "true"       # ...but keep Gemini
```

Resolution order (see `_agent_enabled` in `app.py`):

1. Explicit `ENABLE_<AGENT>` (`true`/`false`) — always wins.
2. Otherwise: `CODA_PROFILE=lab` ⇒ off; anything else (incl. unset / `full`) ⇒ on.

## Authorization: workspace mode kills source-patching

The other half of the lab contract is **who** may use the app.
`CODA_AUTH_MODE` has three values (see `app.yaml.template`):

- **`owner`** (default) — single-user. Only the app owner plus any emails in
  `CONTROL_TOWER_AUTHORIZED_EMAILS` / `AUTHORIZED_EMAILS` /
  `DATABRICKS_APP_AUTHORIZED_EMAIL`.
- **`allowlist`** — only the explicit allowlist emails (owner resolution not
  required — good for SP-deployed apps with a known attendee set).
- **`workspace`** — **any authenticated workspace user.** Used for labs: each
  attendee gets their own isolated instance in their own workspace, so
  per-instance auth is simply "whoever is in this workspace."

Auth is enforced centrally by `_user_is_authorized` in `app.py`, across HTTP
requests, WebSocket connects, and the `configure-pat` gate (with a bootstrap
window before the owner is resolved). The mode is read from the deployment env
— **not** baked into source.

### The Control Tower contract

Before this change, Control Tower had to patch CoDA's `app.py` / `app.yaml` at
deploy time to inject each attendee's email into the authorization check. That
was brittle and version-coupled.

Now Control Tower just deploys CoDA unmodified and sets two env vars on the
deployment:

```
CODA_AUTH_MODE=workspace
CODA_PROFILE=lab
```

That's the whole contract. No source edits, no allowlist injection, no
per-attendee patching. `scripts/lab_deploy.py` (and `make lab-deploy`) set both
automatically and mirror Control Tower's exact SDK path
(`repos.create` → `apps.create_and_wait` → `apps.deploy_and_wait`), so a local
operator and Control Tower converge on the identical deployment shape.

First-boot `.git` reinit is safe under Control Tower's `repos.create` source:
the app container's `/app/python/source_code` is an ephemeral copy, so
reinitializing git there cannot touch the workspace Git folder, its repo link,
or workspace sync (the post-commit hook only syncs `~/projects/*`). The reinit
is also fully defensive — any failure is logged and non-fatal.

## Deploying

```bash
# Full build, single-user (owner) — your own workspace
make deploy PROFILE=<profile>

# Lab build, workspace-wide auth — mirrors the Control Tower SDK path
make lab-deploy PROFILE=<profile> APP_NAME=coda-lab
make lab-verify PROFILE=<profile> APP_NAME=coda-lab

# Or directly, with extra env overrides:
uv run python scripts/lab_deploy.py --profile <profile> --app-name coda-lab \
    --extra-env MLFLOW_TRACING_ENABLED=true
```

`scripts/lab_deploy.py` is idempotent: re-running against an existing repo /
app / deployment converges rather than erroring, so it is safe as a
retry-on-failure provisioning primitive.

## Verifying

- Unit tests for the toggle logic and lab deploy: `tests/test_lean_profile.py`,
  `tests/test_lab_deploy.py`.
- Workspace-mode admission + acceptance gate (real infra, opt-in):
  `tests/e2e/test_lab_workspace_auth.py` (see `tests/e2e/README.md`).
