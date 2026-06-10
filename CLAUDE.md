# Claude Code on Databricks

Welcome! This environment comes pre-configured with 5 AI coding agents, 39 skills, and 2 MCP servers. Hermes Agent is available alongside Claude Code, Codex, Gemini CLI, and OpenCode — launch it with `hermes chat`.

## Skills (30 total)

### Databricks Skills (16)

| Category | Skills |
|----------|--------|
| AI & Agents | agent-bricks, databricks-genie, mlflow-evaluation, model-serving |
| Analytics | aibi-dashboards, databricks-unity-catalog |
| Data Engineering | spark-declarative-pipelines, databricks-jobs, synthetic-data-generation |
| Development | asset-bundles, databricks-app-apx, databricks-app-python, databricks-python-sdk, databricks-config |
| Reference | databricks-docs, unstructured-pdf-generation |

### Development Workflow Skills (14)

From [obra/superpowers](https://github.com/obra/superpowers):

| Skill | Purpose |
|-------|---------|
| brainstorming | Design features through collaborative dialogue |
| test-driven-development | RED-GREEN-REFACTOR cycle |
| systematic-debugging | 4-phase root cause analysis |
| writing-plans | Create detailed implementation plans |
| verification-before-completion | Verify before claiming done |
| executing-plans | Batch execution with checkpoints |
| dispatching-parallel-agents | Concurrent subagent workflows |
| subagent-driven-development | Fast iteration with two-stage review |
| using-git-worktrees | Parallel development branches |
| requesting-code-review | Pre-review checklist |
| receiving-code-review | Responding to feedback |
| finishing-a-development-branch | Merge/PR decision workflow |
| writing-skills | Create new skills |
| using-superpowers | Introduction to available skills |

## MCP Servers

- **DeepWiki** - AI-powered documentation for any GitHub repository
- **Exa** - Web search and code context retrieval

## Databricks CLI

The Databricks CLI is pre-configured with your credentials. Test it:
```bash
databricks current-user me
```

Databricks can only authenticate with a PAT or CLIENT_ID and CLIENT_SECRET pair. If you have trouble logging in, remove the CLIENT_SECRET and CLIENT_ID from your environment, then try again. We want access to only be based on the app owner's credentials.

Common commands:
```bash
databricks workspace list /Workspace/Users/
databricks jobs list
databricks clusters list
```

## Project Setup

Before starting any new project or documentation:

1. **Always initialize a git repo first:**
   ```bash
   mkdir my-project && cd my-project
   git init
   ```
   Or clone an existing repo:
   ```bash
   git clone https://github.com/user/repo.git
   cd repo
   ```

2. **Why?** Git commits automatically sync your work to Databricks Workspace at `/Workspace/Users/{your-email}/projects/{project-name}/`

3. **Then start working** - your commits will be backed up to Workspace

## How to work with users — clarify, recommend, confirm

Apply this to every request (it matters most for people building their first
app):

- **Do not rush to build.** For any app or resource request, first state your
  **recommended** approach and the key choices, then confirm with the user
  before scaffolding, provisioning, or deploying anything. Plan, then build.
- **Lead with your recommendation.** When you ask a question, give your
  recommended option first (with a one-line "why"), then list alternatives.
  Never present a bare list of options with no guidance.
- **Clarify which Databricks resources are actually needed** before creating
  any — app type, whether it needs persistence (Lakebase) vs analytics (SQL
  warehouse), catalog/schema, model/serving endpoint. Don't provision resources
  the app doesn't need.
- **End every build with the payoff:** give the user the live app URL and a
  short, plain-language recap of what you built.

## Building Apps — default to AppKit + Lakebase

When the user asks to build an app, dashboard, tool, or UI **without naming a
framework**, the default is **AppKit** (React + Vite + TypeScript) with a
**Lakebase** (Postgres) backend — NOT Streamlit, NOT Dash, NOT a Delta-backed
Python app. This is a hard default, not a preference.

- Follow the **databricks-apps-python** skill. Scaffold with `databricks apps init`
  (AppKit template), then immediately apply the CoDA UX defaults in the skill's
  `7-appkit-ux.md` (branded app shell, theme provider + light/dark, mandatory
  loading/empty/error states, responsive layout, lucide icons, and the
  app-type→layout map). New apps should look polished with no UX prompting from
  the user.
- **Never make the user click resources in the Databricks UI.** Only when the
  app needs persistence, provision Lakebase on demand with
  `scripts/lakebase_ensure.py` and bind it non-interactively (see the
  golden-path scaffold in the skill). Apps with no saved state skip Lakebase.
- Use a Python framework (Streamlit/Dash/Gradio/Flask/FastAPI/Reflex) ONLY when
  the user explicitly names one, explicitly wants Python, or you are extending
  an existing Python app. If the request is ambiguous, choose AppKit.

## Architecture

Real-time terminal I/O over **WebSocket** (Flask-SocketIO) with automatic **HTTP polling fallback** via a Web Worker. Single gunicorn worker (PTY fds are process-local), 16 gthread threads. Per-session locks for WebSocket handlers; parallel agent setup at startup via ThreadPoolExecutor.

## Quick Start

- Projects sync to Databricks Workspace on git commit
- Use `/commit` for guided commits
- Ask "help me create a dashboard" to see skills in action
- Ask about any GitHub repo with DeepWiki MCP

## Credits

- Databricks skills from [databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
- Development workflow skills from [obra/superpowers](https://github.com/obra/superpowers)

# things to remember
Remember to never move .git folder to the workspace if you're running workspace import.