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

| Concern              | Lab build (DEFAULT)                        | Full build (opt-in)                        |
| -------------------- | ------------------------------------------ | ------------------------------------------ |
| `CODA_PROFILE`       | `lab` (or unset)                           | `full` (must set explicitly)               |
| Coding agents        | **Claude only** (others off)               | Claude, Codex, OpenCode, Gemini, Hermes    |
| Guided lab coach     | on (clarify → recommend → confirm)         | always-on contract; coach block lab-only   |
| Claude auto mode     | `bypassPermissions` (zero prompts)         | safe `default` (prompts)                    |
| App builder          | AppKit (+ Lakebase on demand)              | AppKit (+ Lakebase on demand)              |
| Content-filter proxy | off (no OpenCode)                          | on (OpenCode needs it)                     |
| `CODA_AUTH_MODE`     | `workspace` (any authenticated user)       | `owner` (single user)                      |
| Agent auth           | **OBO** (agents act as the attendee; no PAT prompt) | PAT (user pastes a token)         |
| `CODA_OBO_ENABLED`   | `true` (on by default; lab-only)           | ignored (always PAT)                       |
| Set by               | `scripts/lab_deploy.py` / `make lab-deploy`| `make deploy` w/ `CODA_PROFILE=full` in app.yaml |

## Why a lean profile

Large-scale labs spin up **one CoDA instance per attendee**. Every extra
coding-agent CLI installed at boot is wall-clock setup time and image weight
multiplied by the attendee count. The lab profile keeps the boot path to the
essentials an attendee actually needs in a guided lab — Claude Code plus the
AppKit + Lakebase app-build path — and skips the rest.

**Lab is the default** (an unset `CODA_PROFILE` resolves to `lab`) because this
fork is lab-first. The full build — every agent available — is opt-in for
individual / dogfood use: set `CODA_PROFILE=full` explicitly.

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
2. Otherwise the effective profile decides: `lab` (the default, incl. unset) ⇒
   off; `full` ⇒ on.

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

## Agent authentication: OBO by default (attendee owns their work)

`CODA_AUTH_MODE` decides who may open the app. **Agent auth** is a separate
axis: once an attendee is in, *as whom* do the coding agents (Claude, the
Databricks CLI, etc.) authenticate when they call Databricks APIs and deploy
things? Two modes, gated by `CODA_OBO_ENABLED`:

- **OBO (on-behalf-of-user) — the lab default.** The agents authenticate as the
  **attendee** using the forwarded user token (`x-forwarded-access-token`) that
  the Databricks Apps proxy injects on every authenticated request. There is **no
  PAT prompt**, and every resource the agent creates (notebooks, jobs, apps,
  catalogs) is **owned by the attendee** — exactly what a lab wants for
  attribution and cleanup.
- **PAT — the fallback.** The attendee pastes a Personal Access Token, which
  `PATRotator` keeps fresh. Used in the full profile, or in lab when
  `CODA_OBO_ENABLED=false`. Better for **unattended / long-running** work (see
  the caveat below).

### The gate

`CODA_OBO_ENABLED` is **on by default and hard-gated to lab mode**:

- In lab (the default, incl. unset `CODA_PROFILE`) → OBO is on. Set
  `CODA_OBO_ENABLED=false` to fall back to PAT.
- In `CODA_PROFILE=full` → the gate is **ignored entirely**; agent auth is always
  PAT. OBO never engages outside lab.

Resolution lives in `_obo_enabled` / `_agent_auth_mode` in `app.py`.

### Runtime model (and the one caveat)

The forwarded user token is **short-lived (~60 min)** and the app gets **no
refresh token** — it can only ever be refreshed by a *new inbound request*
carrying a fresh token. CoDA handles this in two ways:

1. **Capture on every request.** `_capture_obo` reads the header on each
   authenticated HTTP request and on the WebSocket handshake, dedupes it, and
   pumps it into all agent CLI configs + `~/.databrickscfg` + `DATABRICKS_TOKEN`
   (the same pipeline `PATRotator` uses). The first capture also triggers setup —
   so in OBO mode the app boots with **no PAT prompt** and starts configuring as
   soon as the attendee's first request lands.
2. **Browser keepalive.** `static/index.html` pings `/api/obo-refresh` every
   ~20 min (well under the ~60-min TTL). As long as the attendee's tab is open,
   the token stays fresh and long agent runs keep working.

> **Caveat (R2):** if the attendee **closes the tab** during a long *unattended*
> run, no fresh token arrives and the current one will lapse (~60 min). For
> attended lab/workshop sessions this is fine — keep the tab open. For unattended
> or very long-running work, prefer the PAT fallback (`CODA_OBO_ENABLED=false`),
> which refreshes server-side without a browser.

### Provisioning (headless, by `scripts/lab_deploy.py`)

OBO needs two pieces of workspace/app config, both provisioned headlessly so
Control Tower (and `make lab-deploy`) need no manual UI steps:

1. **Workspace scope allowlist** — `patch_public_workspace_setting` sets
   `allowed_apps_user_api_scopes = ["all-apis"]` so apps in the workspace may
   request the user token with full API breadth (`ensure_obo_scopes`).
2. **Per-app scopes** — the app is created with
   `user_api_scopes=["all-apis"]` (`ensure_app` / `enable_obo_and_create_app`).

The workspace patch needs **workspace-admin** auth (Control Tower has it). A
non-admin local `lab_deploy.py` run logs a warning and continues — the deploy
still succeeds; OBO can be enabled separately, or attendees use the PAT prompt.
Updated Control Tower contract (per attendee):

```
1. patch_public_workspace_setting(allowed_apps_user_api_scopes=["all-apis"])
2. apps.create(App(name, user_api_scopes=["all-apis"], ...))
3. apps.deploy + apps.update_permissions  (unchanged)
4. env: CODA_AUTH_MODE=workspace, CODA_PROFILE=lab   (OBO on by default;
        pass CODA_OBO_ENABLED=false to opt out)
```

### Residual preview-gate validation (one-time per account)

> **TODO / validate once:** Confirm on ONE real attendee workspace whether
> `patch_public_workspace_setting(allowed_apps_user_api_scopes=["all-apis"])` is
> **sufficient on its own**, or whether the **account-level "On-Behalf-Of User
> Authorization" Previews toggle** must also be enabled first. If the account
> toggle is required, it is a **one-time account action** (account previews apply
> to all workspaces) — record it here as a fleet-setup prerequisite, not a
> per-attendee step. _Status: not yet validated on a live account._

## Attendee experience (lab profile)

The lab profile is tuned so a first-time, possibly non-technical attendee can
succeed with almost no instructions:

- **The agent speaks first.** On the first terminal session, CoDA auto-launches
  Claude with a seeded opening, so the attendee is greeted and guided without
  needing to know to type `claude`. Disable with `CODA_LAB_AUTOLAUNCH=false`. A
  toolbar **"Start building"** button is the manual fallback (shown only in lab
  mode).
- **Persona check, once.** The coach asks whether the attendee is *technical* or
  *business* and adapts its language (outcomes vs. components). The answer is
  persisted to `~/.coda/persona`, so it is never asked again across sessions.
- **Auto mode on (zero approval prompts).** The lab profile sets Claude's
  `permissions.defaultMode` to `bypassPermissions`, so the build runs end to end
  without the attendee approving each edit and command. This is safe here
  because each attendee's CoDA app is an isolated, per-workspace container
  scoped to their own identity (agents can only touch what that identity
  allows). Override with `CODA_AUTO_MODE` (`false` to restore prompts, or an
  explicit mode like `acceptEdits` / `auto`). The full build stays at Claude's
  safe `default`.
- **Guided, not rushed.** The coach clarifies what to build, leads with a
  recommendation, and confirms a short plan before scaffolding or deploying
  (see `instructions/lab_coach.md`, injected into Claude's memory at boot).
- **The payoff.** Every build ends with the live app URL and a plain-language
  recap of what was built.
- **Start over.** If an attendee gets stuck, "start over" cleanly sets the
  current project aside and begins fresh (reusing the saved persona).

These behaviors live in `instructions/lab_coach.md` and the always-on contract
in `CLAUDE.md`; the auto-launch + fallback affordance live in `app.py` /
`static/index.html`.

## App persistence: on-demand Lakebase (no UI clicks)

When the agent builds an app that needs persistence (CRUD records, user prefs,
saved views), it provisions Lakebase **on demand** — never at boot, and never by
making the attendee click resources in the Databricks UI:

```bash
# Idempotent: creates the lab's Lakebase instance the first time, reuses it after.
uv run python scripts/lakebase_ensure.py
```

`scripts/lakebase_ensure.py`:

- Resolves a **deterministic** instance name (`--name` ›
  `LAKEBASE_INSTANCE_NAME` › `coda-lab`) so one lab reuses a single instance
  across apps, and Control Tower can find + tear it down later.
- Waits until the instance is `AVAILABLE`, then writes the binding to
  `~/.coda/lakebase.json` and prints the exact non-interactive
  `databricks apps init --resource …` flags. This is what avoids the interactive
  "missing required resource Postgres" prompt that makes a naive
  `apps init --features=lakebase` fail.
- Applies any Control-Tower-injected `LAB_RESOURCE_TAGS` (`k=v,k2=v2`) as
  `custom_tags` for cost attribution / teardown.

Apps with no saved state (read-only dashboards/viewers) skip Lakebase entirely
and incur no database cost.

**Permissions note:** provisioning a Lakebase instance requires the deploying
identity to have the database-create entitlement on the workspace. If it does
not, `lakebase_ensure.py` exits non-zero with a clear message and the agent
offers to build the app without persistence. For labs, grant the entitlement to
the deploying service principal up front (Control Tower handles this at fleet
setup).

## Deploying

```bash
# Full build, single-user (owner) — set CODA_PROFILE=full explicitly
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
- OBO agent auth: gate + mode resolver (`tests/test_agent_auth_mode.py`), token
  capture + pump (`tests/test_obo_auth.py`, `tests/test_obo_capture.py`),
  keepalive endpoint (`tests/test_obo_refresh.py`), endpoint/boot awareness
  (`tests/test_obo_endpoints.py`, `tests/test_obo_boot.py`), and headless OBO
  provisioning (`tests/test_lab_deploy_obo.py`).
- Workspace-mode admission + acceptance gate (real infra, opt-in):
  `tests/e2e/test_lab_workspace_auth.py` (see `tests/e2e/README.md`).
