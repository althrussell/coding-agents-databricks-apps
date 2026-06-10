---
name: databricks-apps-python
description: "Builds Databricks applications. DEFAULT to AppKit (React + Vite + TypeScript + Lakebase) for every new app — it is the hard default, not a preference. Python frameworks (Dash, Streamlit, Gradio, Flask, FastAPI, Reflex) are opt-in ONLY when the user explicitly asks for Python or names a Python framework. Handles OAuth authorization, app resources, SQL warehouse and Lakebase connectivity, model serving, foundation model APIs, and deployment. Use when building web apps, dashboards, ML demos, or REST APIs for Databricks, or when the user mentions AppKit, Streamlit, Dash, Gradio, Flask, FastAPI, Reflex, or Databricks app."
---

# Databricks Applications

Build Databricks applications. **The default for ANY new app is AppKit + Lakebase** (React + Vite + TypeScript frontend, Lakebase Postgres backend). Do not reach for Streamlit/Dash/Delta unless the user explicitly opts out of AppKit. For full examples and recipes, see the **[Databricks Apps Cookbook](https://apps-cookbook.dev/)**.

---

## AppKit + Lakebase — the DEFAULT for new apps

> **HARD DEFAULT (do not deviate without explicit user opt-out).** When a user
> asks to "build an app", "make a dashboard", "create a tool", or anything
> similar WITHOUT naming a framework, you MUST scaffold with **AppKit**
> (React + Vite + TypeScript) and use **Lakebase** (Postgres) for app state —
> NOT Streamlit, NOT Dash, NOT a Delta-table-backed Python app. Streamlit and
> the other Python frameworks are opt-in only (see
> [Python frameworks (opt-in only)](#python-frameworks-opt-in-only)).

**[AppKit](https://github.com/databricks/appkit)** is a Node.js + React SDK with a plugin architecture, built-in caching, telemetry, end-to-end type safety, and the `@databricks/appkit-ui` component library (shadcn/Radix primitives, `lucide-react` icons, charts, `DataTable`, `GenieChat`, `Sidebar`).

### Why AppKit is the default
- Modern full-stack UX out of the box (React + Vite + TypeScript), not a single-file script.
- `@databricks/appkit-ui` ships a complete, Databricks-styled design system — every new app looks polished without the user prompting for design.
- Lakebase (Postgres) is the right OLTP store for app state — far better UX than rendering a Delta table in Streamlit.
- Plugin architecture (Analytics, Genie, Files, Lakebase) covers the common Databricks app needs.

### Requirements (pre-checked at CoDA boot)
- Node.js v22+
- Databricks CLI v0.295.0+
- A pinned, known-good AppKit version is recorded at `~/.coda/appkit-version` (see [appkit-precache](#pinned-appkit-version--offline-cache)).

### Golden-path scaffold (zero prompts — never make the user click anything)

**First decide whether the app needs persistence.** Many apps (read-only
dashboards, viewers, simple tools) do NOT — those skip Lakebase entirely.

**If the app does NOT need a database** (no CRUD, no saved state):
```bash
# Scaffold the AppKit (React + Vite + TypeScript) template, no DB resource.
databricks apps init --name <app> --auto-approve
```

**If the app needs persistence** (CRUD records, user prefs, saved views), provision
Lakebase on demand FIRST, then bind it non-interactively so `apps init` never
hits the interactive "missing required resource Postgres" prompt:
```bash
# 1. Provision/reuse the lab's Lakebase instance (takes a few minutes the first
#    time; reused instantly afterwards). Writes ~/.coda/lakebase.json.
uv run python /app/python/source_code/scripts/lakebase_ensure.py

# 2. Scaffold with the lakebase feature, binding the instance from step 1 via
#    --set (non-interactive). With --auto-approve, an optional resource is only
#    configured when its values are passed via --set — which is exactly what
#    binds the DB without a prompt. The --set key is <plugin>.database.<field>;
#    confirm the plugin/resource key for the template with
#    `databricks apps init --help` (keys come from appkit.plugins.json).
databricks apps init --name <app> --features=lakebase --auto-approve \
  --set lakebase.database.instance_name=$(jq -r .name ~/.coda/lakebase.json) \
  --set lakebase.database.database_name=$(jq -r .database_name ~/.coda/lakebase.json)
```

`lakebase_ensure.py` is idempotent — a second app in the same lab reuses the one
instance. If it exits non-zero (e.g. the deploying identity lacks the
database-create entitlement), tell the user and offer to proceed without
persistence. This scaffolds the full project and installs dependencies. **Always read [7-appkit-ux.md](7-appkit-ux.md) immediately after scaffolding** and apply the CoDA UX defaults so the app ships with a branded shell, theming, and proper loading/empty/error states — without the user having to ask.

### Deploy
```bash
databricks apps deploy
```

### AppKit plugins
| Plugin | Purpose |
|--------|---------|
| **Lakebase** | OLTP PostgreSQL via Lakebase with OAuth token management — DEFAULT app-state store |
| **Analytics** | SQL queries against Databricks SQL Warehouses — file-based, typed, cached |
| **Genie** | Conversational AI/BI interface with natural language queries |
| **Files** | Browse/upload Unity Catalog Volumes |

### AI-assisted development
```bash
# Install agent skills for AI-powered scaffolding
databricks experimental aitools skills install

# Query AppKit docs inline
npx @databricks/appkit docs "your question here"
```

### AppKit documentation
- **[7-appkit-ux.md](7-appkit-ux.md)** — CoDA UX defaults + golden-path overlay (READ THIS after scaffolding)
- **[AppKit Docs](https://databricks.github.io/appkit/docs/)** — getting started, plugins, API reference
- **[AI-assisted development](https://databricks.github.io/appkit/docs/development/ai-assisted-development)** — guidance for code assistants
- **[llms.txt](https://databricks.github.io/appkit/llms.txt)** — machine-readable docs for AI context

---

## Python frameworks (opt-in only)

> **Do NOT default to Streamlit or any Python framework.** Use a Python
> framework ONLY when at least one of these is true:
> - The user explicitly names Streamlit / Dash / Gradio / Flask / FastAPI / Reflex.
> - The user explicitly says they want a Python app or cannot use Node/TypeScript.
> - You are extending an existing Python app.
>
> Otherwise, use [AppKit + Lakebase](#appkit--lakebase--the-default-for-new-apps).
> If the request is ambiguous, default to AppKit.

## Critical Rules for Python apps (always follow)

- **MUST** confirm framework choice or use [Python Framework Selection](#python-framework-selection) below
- **MUST** use SDK `Config()` for authentication (never hardcode tokens)
- **MUST** use `app.yaml` `valueFrom` for resources (never hardcode resource IDs)
- **MUST** use `dash-bootstrap-components` for Dash app layout and styling
- **MUST** use `@st.cache_resource` for Streamlit database connections
- **MUST** deploy Flask with Gunicorn, FastAPI with uvicorn (not dev servers)

## Required Steps for Python apps

Copy this checklist and verify each item:
```
- [ ] Framework selected
- [ ] Auth strategy decided: app auth, user auth, or both
- [ ] App resources identified (SQL warehouse, Lakebase, serving endpoint, etc.)
- [ ] Backend data strategy decided (SQL warehouse, Lakebase, or SDK)
- [ ] Deployment method: CLI or DABs
```

---

## Python Framework Selection

| Framework | Best For | app.yaml Command |
|-----------|----------|------------------|
| **Dash** | Production dashboards, BI tools, complex interactivity | `["python", "app.py"]` |
| **Streamlit** | Rapid prototyping, data science apps, internal tools | `["streamlit", "run", "app.py"]` |
| **Gradio** | ML demos, model interfaces, chat UIs | `["python", "app.py"]` |
| **Flask** | Custom REST APIs, lightweight apps, webhooks | `["gunicorn", "app:app", "-w", "4", "-b", "0.0.0.0:8000"]` |
| **FastAPI** | Async APIs, auto-generated OpenAPI docs | `["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]` |
| **Reflex** | Full-stack Python apps without JavaScript | `["reflex", "run", "--env", "prod"]` |

**Default**: There is no Python default — the default for any new app is [AppKit + Lakebase](#appkit--lakebase--the-default-for-new-apps). Only once the user has explicitly opted into Python: pick **Dash** for production dashboards, **FastAPI** for APIs, **Gradio** for ML demos, **Streamlit** for quick data-science prototypes.

---

## Quick Reference

| Concept | Details |
|---------|---------|
| **Runtime** | Python 3.11, Ubuntu 22.04, 2 vCPU, 6 GB RAM |
| **Pre-installed** | Dash 2.18.1, Streamlit 1.38.0, Gradio 4.44.0, Flask 3.0.3, FastAPI 0.115.0 |
| **Auth (app)** | Service principal via `Config()` — auto-injected `DATABRICKS_CLIENT_ID`/`DATABRICKS_CLIENT_SECRET` |
| **Auth (user)** | `x-forwarded-access-token` header — see [1-authorization.md](1-authorization.md) |
| **Resources** | `valueFrom` in app.yaml — see [2-app-resources.md](2-app-resources.md) |
| **Cookbook** | https://apps-cookbook.dev/ |
| **Docs** | https://docs.databricks.com/aws/en/dev-tools/databricks-apps/ |

---

## Detailed Guides

**AppKit UX defaults**: Read [7-appkit-ux.md](7-appkit-ux.md) immediately after scaffolding any AppKit app — it defines the CoDA UX contract (branded app shell, theme provider + light/dark, mandatory loading/empty/error states, responsive layout + lucide icons) and the app-type→layout map the agent must apply with no prompting. (Keywords: AppKit, UX, app shell, theme, sidebar, layout, dashboard, CRUD, chat, form)

**Authorization**: Use [1-authorization.md](1-authorization.md) when configuring app or user authorization — covers service principal auth, on-behalf-of user tokens, OAuth scopes, and per-framework code examples. (Keywords: OAuth, service principal, user auth, on-behalf-of, access token, scopes)

**App resources**: Use [2-app-resources.md](2-app-resources.md) when connecting your app to Databricks resources — covers SQL warehouses, Lakebase, model serving, secrets, volumes, and the `valueFrom` pattern. (Keywords: resources, valueFrom, SQL warehouse, model serving, secrets, volumes, connections)

**Frameworks**: See [3-frameworks.md](3-frameworks.md) for Databricks-specific patterns per framework — covers Dash, Streamlit, Gradio, Flask, FastAPI, and Reflex with auth integration, deployment commands, and Cookbook links. (Keywords: Dash, Streamlit, Gradio, Flask, FastAPI, Reflex, framework selection)

**Deployment**: Use [4-deployment.md](4-deployment.md) when deploying your app — covers Databricks CLI, Asset Bundles (DABs), app.yaml configuration, and post-deployment verification. (Keywords: deploy, CLI, DABs, asset bundles, app.yaml, logs)

**Lakebase**: Use [5-lakebase.md](5-lakebase.md) when using Lakebase (PostgreSQL) as your app's data layer — covers auto-injected env vars, psycopg2/asyncpg patterns, and when to choose Lakebase vs SQL warehouse. (Keywords: Lakebase, PostgreSQL, psycopg2, asyncpg, transactional, PGHOST)

**MCP tools**: Use [6-mcp-approach.md](6-mcp-approach.md) for managing app lifecycle via MCP tools — covers creating, deploying, monitoring, and deleting apps programmatically. (Keywords: MCP, create app, deploy app, app logs)

**Foundation Models**: See [examples/llm_config.py](examples/llm_config.py) for calling Databricks foundation model APIs — covers OAuth M2M auth, OpenAI-compatible client wiring, and token caching. (Keywords: foundation model, LLM, OpenAI client, chat completions)

---

## Workflow

1. Determine the task type:

   **New app from scratch?** → Use [AppKit + Lakebase](#appkit--lakebase--the-default-for-new-apps) (`databricks apps init`), then apply [7-appkit-ux.md](7-appkit-ux.md). This is the default — only use [Python Framework Selection](#python-framework-selection) if the user explicitly opted into Python.
   **Setting up authorization?** → Read [1-authorization.md](1-authorization.md)
   **Connecting to data/resources?** → Read [2-app-resources.md](2-app-resources.md)
   **Using Lakebase (PostgreSQL)?** → Read [5-lakebase.md](5-lakebase.md)
   **Deploying to Databricks?** → Read [4-deployment.md](4-deployment.md)
   **Using MCP tools?** → Read [6-mcp-approach.md](6-mcp-approach.md)
   **Calling foundation model/LLM APIs?** → See [examples/llm_config.py](examples/llm_config.py)

2. Follow the instructions in the relevant guide
3. For full code examples, browse https://apps-cookbook.dev/

---

## Core Architecture

All Python Databricks apps follow this pattern:

```
app-directory/
├── app.py                 # Main application (or framework-specific name)
├── models.py              # Pydantic data models
├── backend.py             # Data access layer
├── requirements.txt       # Additional Python dependencies
├── app.yaml               # Databricks Apps configuration
└── README.md
```

### Backend Toggle Pattern

```python
import os
from databricks.sdk.core import Config

USE_MOCK = os.getenv("USE_MOCK_BACKEND", "true").lower() == "true"

if USE_MOCK:
    from backend_mock import MockBackend as Backend
else:
    from backend_real import RealBackend as Backend

backend = Backend()
```

### SQL Warehouse Connection (shared across all frameworks)

```python
from databricks.sdk.core import Config
from databricks import sql

cfg = Config()  # Auto-detects credentials from environment
conn = sql.connect(
    server_hostname=cfg.host,
    http_path=f"/sql/1.0/warehouses/{os.getenv('DATABRICKS_WAREHOUSE_ID')}",
    credentials_provider=lambda: cfg.authenticate,
)
```

### Pydantic Models

```python
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum

class Status(str, Enum):
    ACTIVE = "active"
    PENDING = "pending"

class EntityOut(BaseModel):
    id: str
    name: str
    status: Status
    created_at: datetime

class EntityIn(BaseModel):
    name: str = Field(..., min_length=1)
    status: Status = Status.PENDING
```

---

## Common Issues

| Issue | Solution |
|-------|----------|
| **Connection exhausted** | Use `@st.cache_resource` (Streamlit) or connection pooling |
| **Auth token not found** | Check `x-forwarded-access-token` header — only available when deployed, not locally |
| **App won't start** | Check `app.yaml` command matches framework; check `databricks apps logs <name>` |
| **Resource not accessible** | Add resource via UI, verify SP has permissions, use `valueFrom` in app.yaml |
| **Import error on deploy** | Add missing packages to `requirements.txt` (pre-installed packages don't need listing) |
| **Lakebase app crashes on start** | `psycopg2`/`asyncpg` are NOT pre-installed — MUST add to `requirements.txt` |
| **Port conflict** | Apps must bind to `DATABRICKS_APP_PORT` env var (defaults to 8000). Never use 8080. Streamlit is auto-configured; for others, read the env var in code or use 8000 in app.yaml command |
| **Streamlit: set_page_config error** | `st.set_page_config()` must be the first Streamlit command |
| **Dash: unstyled layout** | Add `dash-bootstrap-components`; use `dbc.themes.BOOTSTRAP` |
| **Slow queries** | Use Lakebase for transactional/low-latency; SQL warehouse for analytical queries |

---

## Platform Constraints

| Constraint | Details |
|------------|---------|
| **Runtime** | Python 3.11, Ubuntu 22.04 LTS |
| **Compute** | 2 vCPUs, 6 GB memory (default) |
| **Pre-installed frameworks** | Dash, Streamlit, Gradio, Flask, FastAPI, Shiny |
| **Custom packages** | Add to `requirements.txt` in app root |
| **Network** | Apps can reach Databricks APIs; external access depends on workspace config |
| **User auth** | Public Preview — workspace admin must enable before adding scopes |

---

## Official Documentation

- **[AppKit](https://databricks.github.io/appkit/docs/)** — preferred SDK for new apps (TypeScript + React)
- **[Databricks Apps Overview](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)** — main docs hub
- **[Apps Cookbook](https://apps-cookbook.dev/)** — ready-to-use code snippets (Streamlit, Dash, Reflex, FastAPI)
- **[Authorization](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)** — app auth and user auth
- **[Resources](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/resources)** — SQL warehouse, Lakebase, serving, secrets
- **[app.yaml Reference](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/app-runtime)** — command and env config
- **[System Environment](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/system-env)** — pre-installed packages, runtime details

## Related Skills

- **[databricks-app-apx](../databricks-app-apx/SKILL.md)** - full-stack apps with FastAPI + React
- **[databricks-bundles](../databricks-bundles/SKILL.md)** - deploying apps via DABs
- **[databricks-python-sdk](../databricks-python-sdk/SKILL.md)** - backend SDK integration
- **[databricks-lakebase-provisioned](../databricks-lakebase-provisioned/SKILL.md)** - adding persistent PostgreSQL state
- **[databricks-model-serving](../databricks-model-serving/SKILL.md)** - serving ML models for app integration
