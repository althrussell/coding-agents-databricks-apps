# Spec: `coda_interactive` MCP Tool

**Status:** Draft, pre-critique-gate
**Date:** 2026-05-28
**Branch:** `feat/coda-mcp-live-session-url` (same as Todo 1)
**Related:** `docs/superpowers/specs/2026-05-28-coda-run-replay-only-design.md` (Todo 1 — establishes the three-mode framework this spec slots into as Mode 2)

> **Amended by:** [`docs/superpowers/specs/2026-05-28-coda-interactive-broaden-source-design.md`](2026-05-28-coda-interactive-broaden-source-design.md) — the `branch` parameter and the Git-Folder-only requirement have been removed. `coda_interactive` now accepts any Workspace directory (Git Folder or plain). The `repos.list` + `repos.update` flow described in Section 3 of this spec has been replaced by a single `workspace.get_status` directory check. The return shape no longer includes a `"branch"` key.

## Goal

Add a new MCP tool, `coda_interactive`, that lets an upstream MCP client (Genie Code, Claude Desktop, Cursor) hand off an in-flight coding session to a human via a CoDA viewer URL. The handoff carries:
- A **chosen coding agent** (`claude` by default; pluggable to `hermes`, `codex`, `gemini`, `opencode`)
- A **project source**: a Databricks Workspace Git Folder path, optionally on a specific branch
- A **kickoff prompt** that gets auto-typed into the agent as the first user message

The human opens the URL, attaches to a live PTY where the agent is already loaded with the project as CWD and the prompt already typed, drives the session, and exits when done. The URL is the only handle — no `result.json`, no `coda_get_result`, no `coda_inbox` integration.

## Why

Mode 3 (`coda_run`) is fire-and-forget batch — the MCP caller can't iterate mid-task. Mode 1 (direct web UI) requires the human to already be inside CoDA and manually wire their project. Neither covers the "I was working in Genie Code on a repo and want to continue with a coding agent inside CoDA" workflow.

`coda_interactive` is built for that handoff. **Critically, this design uses Databricks Workspace Git Folders as the source of truth** — Coda already has Databricks authentication via its existing PAT, so no new credentials need to be configured for the tool to clone repos. The MCP caller's Git Folder in Workspace is the durable artifact that survives between local sessions and Coda sessions.

## The Three-Mode Framework (reminder)

See Todo 1's spec for the canonical table. This spec finalizes Mode 2:

| Mode | How invoked | PTY tag | Lifecycle | URL semantics |
|---|---|---|---|---|
| **1. Direct launch** | User opens web UI, creates a tab | (none) | 24h idle / WS-extends | No external URL |
| **2. `coda_interactive`** *(this spec)* | MCP client calls the tool, passes the URL to a human | `replay_only=False` | 24h idle / WS-extends | Live attach |
| **3. `coda_run`** | MCP client fires the tool, URL is post-hoc replay only | `replay_only=True` | Immediate teardown on hermes -z exit | Replay only |

## Design

### 1. Tool signature

```python
@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    ),
)
async def coda_interactive(
    prompt: str,                          # initial kickoff message; auto-typed as first user input
    workspace_path: str,                  # required, e.g. "/Workspace/Users/me@db.com/projects/feature-X"
    branch: str = "",                     # optional — if set, updates the Git Folder to this branch first
    agent: str = "claude",                # claude | hermes | codex | gemini | opencode
    email: str = "",                      # X-Forwarded-Email passthrough (single-user app, kept for parity)
) -> str:
    ...
```

**Return shape** (JSON string):
```json
{
  "status": "launched",
  "viewer_url": "https://<app>.<workspace>.aws.databricksapps.com/?session=pty-...",
  "agent": "claude",
  "project_dir": "/home/app/.coda/projects/pty-...",
  "workspace_path": "/Workspace/Users/me@db.com/projects/feature-X",
  "branch": "feature/X",
  "instructions": "Open viewer_url to attach. The agent is loaded with the project files exported from Workspace and your kickoff prompt typed. Continue from there; type the agent's quit command (e.g. /quit) and then `exit` to end the session. Note: git history is NOT available in the session — files are an export, not a clone."
}
```

### 1a. Caller pre-condition: project must be in Databricks Workspace

This is a contract the **upstream MCP caller** (Genie Code, Claude Desktop, etc.) is responsible for satisfying — `coda_interactive` cannot create a Git Folder, it can only consume one.

**The caller must:**
1. Ensure the project of interest is a **Databricks Workspace Git Folder** (created via the workspace UI's "Create > Git Folder" or via the Repos API). Plain Workspace folders without a git remote backing will not work — the branch-update step has no remote to fetch from.
2. **Commit and push** any local working changes back to the Git Folder's remote (GitHub/GitLab/etc.) **before** calling `coda_interactive`. The export is a server-side snapshot — uncommitted local changes are invisible to Coda.
3. If a specific branch is needed, ensure that branch exists on the remote and is reachable by the Databricks Workspace's stored credentials for the Git Folder.

**The MCP tool's `instructions` string surfaces this requirement to the calling LLM:**

> Before calling `coda_interactive`, ensure the user's project is a Databricks Workspace Git Folder and that any in-progress changes have been pushed to the Git Folder's remote. The tool exports a server-side snapshot — uncommitted local changes will not appear in the Coda session. If unsure, prompt the user to push their changes first or pass `workspace_path` for a recently-synced Git Folder.

This text becomes the tool's surfaced description in the FastMCP server's instruction block, alongside the existing `coda_run` guidance.

On error: `{"status": "error", "error": "<message>"}`. No partial state — if export fails or PTY creation fails, no PTY is created and no `viewer_url` is returned.

### 2. Agent launch matrix

Each agent has a known interactive-launch command (verified against the deployed setup scripts):

| `agent` value | Launch command sent to PTY |
|---|---|
| `claude` (default) | `claude\n` |
| `hermes` | `hermes chat\n` |
| `codex` | `codex\n` |
| `gemini` | `gemini\n` |
| `opencode` | `opencode\n` |

Unknown `agent` values return an error immediately — no Workspace API call, no PTY.

### 3. Project source: Workspace Git Folder export

Coda's existing Databricks authentication (PAT in `DATABRICKS_TOKEN`) is sufficient for both steps. No new tokens, no `repo_token` parameter, no GitHub credential plumbing.

Working directory on Coda: `~/.coda/projects/<pty_session_id>/`.

**Step 3a — (Optional) Update the Git Folder to the requested branch.** Skip if `branch` is empty.

**Side-effect note:** `repos.update(branch=...)` mutates the Git Folder's server-side state — the folder is now on the requested branch for *any* tools/processes accessing it (other notebooks, jobs, parallel `coda_interactive` calls, etc.). For Coda's single-user-app model this is acceptable: the user is the only one mutating the Git Folder. If multi-user support is ever added, this design must be revisited — likely by cloning a sibling Git Folder per session.

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()  # picks up DATABRICKS_HOST + DATABRICKS_TOKEN from env

# Resolve the Repos / Git Folder ID from the workspace_path
repos = w.repos.list(path_prefix=workspace_path)
repo = next((r for r in repos if r.path == workspace_path), None)
if repo is None:
    return {"status": "error", "error": f"No Git Folder found at {workspace_path}"}

# Update to the requested branch — Databricks performs the actual fetch + checkout server-side
w.repos.update(repo_id=repo.id, branch=branch)
```

**Step 3b — Export the file tree into Coda's local disk.**

The Databricks Workspace API exposes a `workspace export-dir`-equivalent through the SDK:

```python
import os
project_dir = os.path.join(os.path.expanduser("~/.coda/projects"), pty_session_id)
os.makedirs(project_dir, exist_ok=True)

# Recursive export — files only, no `.git` directory.
# (Implementation may use the workspace.export() loop or shell out to `databricks workspace export-dir`.)
_export_workspace_tree(w, workspace_path, project_dir)
```

`_export_workspace_tree` is a small helper that:
1. Lists the workspace_path recursively (`w.workspace.list(workspace_path)` with recursive traversal)
2. For each file: calls `w.workspace.export(path=..., format=ExportFormat.SOURCE)` and writes the content to the local mirror
3. Preserves directory structure
4. Handles files (NOT notebooks — notebooks export to `.py`/`.ipynb` via the `SOURCE` format)

Implementation note: if the SDK's recursive-export is awkward, fall back to shelling out: `subprocess.run(["databricks", "workspace", "export-dir", workspace_path, project_dir, "--overwrite"], check=True, capture_output=True, timeout=300)`. The CLI is preconfigured on Coda. Either approach is acceptable; the planner will pick after a small spike.

**Important:** Only the working tree is exported. The `.git/` directory is NOT included — Workspace Git Folders manage git state server-side and don't expose `.git` via the API. Git history is unavailable inside the session. This trade-off is acknowledged in §7 (Out of Scope) and surfaced to the caller via the `instructions` field in the response.

**Snapshot semantics:** `workspace.export()` reflects the **committed HEAD state** of the Git Folder — not any uncommitted changes that exist in the Databricks Workspace UI editor. If the caller's user has uncommitted edits in the Workspace UI for this Git Folder, those changes will NOT appear in the Coda session. This is the same constraint the caller pre-condition (§1a) communicates: push commits first.

**Binary file handling:** `workspace.export(format=ExportFormat.SOURCE)` may fail (HTTP 400) on binary files (images, PDFs, compiled artifacts). The export helper must wrap each per-file export in a try/except and skip-and-log files that error out, rather than aborting the entire export. The agent in the session gets a partial tree (text/source files); the human can decide whether the missing binaries matter.

**Empty export:** If the Workspace Git Folder is empty OR if all files are non-exportable, the project dir ends up empty after the export. The PTY is still launched (the agent will sit in an empty dir). This is acceptable — the human can investigate via the agent.

Export timeout: 300 s (5 min). Big repos may need bumping later; not parameterizable in MVP.

### 4. Prompt seeding

After the PTY is created and the agent launched:

```python
import time
# Wait briefly for the agent to initialize and present its prompt.
time.sleep(2)

# Type the prompt into the PTY as the first user message.
_app_send_input(pty_session_id, prompt + "\n")
```

The 2 s delay is a pragmatic choice — agents typically print a banner + prompt within that window. If the timing misses on slow startup, the prompt still lands; the agent sees it as part of the kickoff. No assertion that the agent is "ready" — that's a brittle race we don't need.

### 5. PTY + project lifecycle

`coda_interactive` PTYs inherit Mode 1's lifecycle exactly:
- Created with `replay_only=False`
- 24h idle TTL via existing `SESSION_TIMEOUT_SECONDS = 86400` cleanup
- WS heartbeat extends while the human is attached
- Teardown via human typing `exit` (which closes bash, which EOFs the PTY) OR 24h idle

**Cleanup hook:** `mcp_close_pty_session(pty_id)` (in `app.py`) gains a side-effect: if `~/.coda/projects/<pty_id>/` exists, delete it (recursively) after closing the PTY. Single cleanup path means the disk lifecycle matches the PTY lifecycle — no new timer or state.

### 6. Where this lives in the codebase

- Modified: `coda_mcp/mcp_server.py` — add `coda_interactive` tool definition next to `coda_run`. **Also update the FastMCP `instructions` string** (currently around lines 43-70) to add a paragraph describing `coda_interactive` so calling LLMs don't treat it like `coda_run` (e.g., don't try to poll for results). The new paragraph must include: the pre-condition that the project must be a Workspace Git Folder, the contract that interactive sessions don't appear in `coda_inbox`, and a note that `coda_get_result` won't return anything for these sessions.
- Modified: `app.py` — extend `mcp_close_pty_session` to clean up the project dir; add `cwd` kwarg to `mcp_create_pty_session` so the spawned bash starts in the project dir. **Prerequisite refactor (security-relevant):** `mcp_create_pty_session`'s inline env-stripping at `app.py:1435-1441` only strips a handful of keys (CLAUDECODE, CLAUDE_CODE_SESSION, DATABRICKS_TOKEN, DATABRICKS_HOST, GEMINI_API_KEY). The HTTP `create_session` route uses `_build_terminal_shell_env(os.environ)` which ALSO strips `NPM_TOKEN`, `UV_DEFAULT_INDEX`, `UV_INDEX_*_PASSWORD`, `UV_INDEX_*_USERNAME`, and `npm_config_//*` registry credential patterns. Today, any MCP-created PTY (including `coda_run`'s) leaks these registry credentials to the child shell via `env`. Fix this as a prerequisite to Todo 2: refactor `mcp_create_pty_session` to call `_build_terminal_shell_env(os.environ)` instead of the inline copy. Zero behavioral impact on the happy path; closes a latent security gap.
- Modified: `coda_mcp/mcp_endpoint.py` — register `coda_interactive` in the Flask-fallback tool dispatch (parity with how `coda_run` is wired).
- New helper: `coda_mcp/workspace_export.py` — encapsulates the Workspace-tree-to-local-dir export logic. Keeps `mcp_server.py` focused on tool orchestration.
- New tests: `tests/test_coda_interactive.py` covering signature validation, branch update, export, agent allow-list, prompt seeding, cleanup on PTY close. Plus `tests/test_workspace_export.py` for the helper. Plus `tests/test_mcp_env_strip.py` (or extending an existing env-strip test file) to assert `mcp_create_pty_session` properly strips registry credentials post-refactor.

**Implementation note on SDK calls:** `WorkspaceClient()` is constructed inside the `coda_interactive` tool function (in the server process). The SDK calls happen BEFORE `mcp_create_pty_session` is invoked, so they execute with the full server environment (including `DATABRICKS_TOKEN`). The PTY child shell's env is separately filtered via `_build_terminal_shell_env` and does NOT receive the Databricks token (which is the correct behavior — we don't want agents in the PTY to see deployer credentials). Future implementers must not move the SDK calls into the PTY subprocess.

### 7. What does NOT change

- `coda_run` is untouched (Todo 1 already finalized).
- `coda_inbox` and `coda_get_result` ignore `coda_interactive` PTYs (no task records get written for them).
- The Mode 1 web-UI launch path is untouched.
- `replay_only` flag plumbing from Todo 1 — `coda_interactive` passes `replay_only=False`, which is already the default.
- `MAX_CONCURRENT_SESSIONS` enforcement — `coda_interactive` PTYs count against the cap exactly like Mode 1 sessions do.

## Architecture

```
                  ┌──────────────────────────────────────────┐
                  │ MCP client calls coda_interactive        │
                  │ (prompt, workspace_path, branch, agent)  │
                  └────────────────┬─────────────────────────┘
                                   ▼
              ┌────────────────────────────────────────────────────┐
              │ Validate agent ∈ allow-list                         │
              │ [if branch]: w.repos.update(branch=branch)          │
              │ _export_workspace_tree(w, ws_path, project_dir)     │
              └────────────────────┬───────────────────────────────┘
                                   ▼
              ┌────────────────────────────────────────────────────┐
              │ pty_session_id = mcp_create_pty_session(            │
              │     label="<agent>-interactive",                    │
              │     replay_only=False,                              │
              │     cwd=project_dir,            # NEW kwarg         │
              │ )                                                   │
              │ _app_send_input(pty_session_id, "<agent_cmd>\n")    │
              │ time.sleep(2)                                       │
              │ _app_send_input(pty_session_id, prompt + "\n")      │
              │ return {viewer_url, agent, ...}                     │
              └────────────────────┬───────────────────────────────┘
                                   ▼
              (Human opens viewer_url; attaches to a live PTY
               already cd'd into the exported project, agent
               running, kickoff prompt already typed.)
                                   ▼
              ┌────────────────────────────────────────────────────┐
              │ Human types `/quit` (agent) and `exit` (shell), OR  │
              │ 24h idle reaper fires                               │
              │ → mcp_close_pty_session(pty_id)                     │
              │ → shutil.rmtree(~/.coda/projects/pty_id/)           │
              └────────────────────────────────────────────────────┘
```

**New `cwd` kwarg on `mcp_create_pty_session`:** required so the PTY's bash spawns in the exported project dir. Default is the existing behavior (bash uses `$HOME`). Additive change; no other callers need updates.

## Data flow scenarios

**Happy path:**
1. User is working locally in their Workspace Git Folder. Pushes recent commits via the Git Folder UI or via the post-commit hook from their existing local Coda environment.
2. MCP client (Genie Code) calls `coda_interactive(prompt="continue debugging the auth flow", workspace_path="/Workspace/Users/me@db.com/projects/auth-feature", branch="feature/auth", agent="claude")`
3. Server validates agent; updates Git Folder to `feature/auth` via Repos API (Databricks does the git fetch); exports tree to `~/.coda/projects/<pty_id>/`; creates PTY in that dir; launches `claude`; types the prompt.
4. Returns `viewer_url`
5. Human opens URL → attaches to live Claude session in the exported project, with kickoff prompt already in the chat
6. Human iterates with Claude; eventually exits the agent and the shell
7. PTY teardown deletes the project dir

**Branch update failure:**
1. MCP client passes a nonexistent `branch`
2. `w.repos.update(...)` raises (Databricks API returns 4xx)
3. Server returns `{"status": "error", "error": "Failed to update Git Folder to branch X: <reason>"}`
4. No export, no PTY, no leak

**Workspace path not found:**
1. MCP client passes a `workspace_path` that isn't a Git Folder or doesn't exist
2. The `repos.list(...)` lookup returns no match, OR the workspace API returns 404
3. Server returns `{"status": "error", "error": "No Git Folder found at <path>"}`
4. No PTY, no leak

**Agent allow-list rejection:**
1. MCP client passes `agent="vim"`
2. Server returns `{"status": "error", "error": "Unknown agent: vim. Allowed: claude, hermes, codex, gemini, opencode"}`
3. No Workspace API call, no PTY

**Concurrent-session limit:**
1. `MAX_CONCURRENT_SESSIONS` already at cap when call arrives
2. Server returns `"Maximum 5 concurrent sessions reached."` (same shape as `coda_run`)
3. No export, no PTY

**Human never attaches:**
1. PTY sits at the agent's prompt, with the kickoff already typed
2. 24h elapses → existing idle cleanup reaps the PTY
3. Project dir deleted as part of `mcp_close_pty_session`

**Human attaches, drives, but closes tab without exiting agent:**
1. WS heartbeat stops
2. 24h idle countdown begins
3. If human reopens within 24h: WS resumes, session continues
4. Else: idle cleanup, project dir cleanup, done

## Error handling

| Error | Returned to MCP client | Server-side cleanup |
|---|---|---|
| Unknown `agent` value | `{"status":"error","error":"Unknown agent: ..."}` | None needed |
| `workspace_path` doesn't exist / not a Git Folder | `{"status":"error","error":"No Git Folder found at <path>"}` | None needed |
| `repos.update(branch=...)` fails (bad branch, network) | `{"status":"error","error":"Failed to update Git Folder to branch X: <reason>"}` | Remove partial project dir |
| Export fails midway (disk full, network) | `{"status":"error","error":"Failed to export workspace tree: <reason>"}` | Remove partial project dir |
| `MAX_CONCURRENT_SESSIONS` reached | `{"status":"error","error":"Maximum N concurrent sessions reached."}` | None needed |
| PTY creation fails | `{"status":"error","error":"Failed to allocate PTY: <reason>"}` | Remove project dir |

No `result.json` is written — no watcher, no completion machinery. Cleanup happens via the PTY's own teardown path.

## Testing strategy

### Unit tests (no PTY, mock Databricks SDK)

1. `test_coda_interactive_unknown_agent_returns_error` — `agent="vim"` → status=error, no SDK call
2. `test_coda_interactive_missing_workspace_path_returns_error` — empty `workspace_path` → error
3. `test_coda_interactive_workspace_not_found` — mock `repos.list()` returns empty → status=error
4. `test_coda_interactive_branch_update_failure_returns_error` — mock `repos.update()` raises → error + no PTY
5. `test_coda_interactive_export_failure_cleans_partial_dir` — mock export raises mid-way → partial dir is removed
6. `test_coda_interactive_skips_branch_update_when_empty` — mock confirms `repos.update()` is NOT called when `branch=""`

### Integration tests (PTY-gated via `_pty_skip`, with mocked Databricks SDK)

7. `test_coda_interactive_happy_path_mocked_export` — mock the Workspace SDK to "export" a fake tree into the local dir, assert PTY is created with the right CWD, agent command is sent, prompt is typed.
8. `test_coda_interactive_concurrent_limit` — fill up `MAX_CONCURRENT_SESSIONS` → call returns error
9. `test_mcp_close_pty_session_removes_project_dir` — create PTY with project dir, close it, assert dir deleted
10. `test_mcp_close_pty_session_handles_missing_project_dir` — no project dir present → close still succeeds (no exception)
11. `test_mcp_create_pty_session_respects_cwd_kwarg` — bash spawns in the requested dir

### Helper tests

12. `tests/test_workspace_export.py`: tests for `_export_workspace_tree` covering: nested dirs, file content fidelity, empty dirs, files-only (skips notebooks), error handling for individual file export failures.

### Regression guard

13. `test_coda_run_does_not_create_project_dir` — calling `coda_run` doesn't touch `~/.coda/projects/`. Defends the lifecycle separation between Modes 2 and 3.

## Out of scope (for Todo 2)

- **Git history inside the session.** Files-only export. Inside the PTY, `git log`, `git diff`, `git blame` return nothing. If history matters for a particular session, the MCP caller can include a `git log --oneline -50` summary in the `prompt` string. A future Todo can layer on a git-clone path with token-based auth.
- **Notebooks as `.ipynb`.** The export uses `ExportFormat.SOURCE` which converts Databricks notebooks to `.py` (or equivalent). MVP doesn't attempt to round-trip notebooks back to Workspace; agents work on the exported source files.
- **Conversation history transfer from the MCP client's local session.** Not in scope. Caller summarizes context into `prompt`.
- **Listing live `coda_interactive` sessions via `coda_inbox`.** URL is the only handle.
- **`coda_get_result` for interactive sessions.** No result.json, no inbox entry.
- **Incremental Workspace updates during the session.** If the user wants to pull newer changes mid-session, they'd need to push to Workspace and re-launch `coda_interactive`. No in-session sync mechanism.
- **Multiple-agent sessions in one PTY.** One agent per call.
- **Non-Workspace sources** (raw zips, external git remotes). Future Todo if needed.
- **Pushing changes BACK from the session to Workspace.** The agent can run Coda's existing post-commit hook (which syncs `~/projects/` to Workspace), but the exported dir at `~/.coda/projects/<pty_id>/` is OUTSIDE that hook's scope by design — we don't want every interactive session to clobber Workspace state. If write-back is needed, that's a follow-up design.

## Migration / Rollout

- Single commit chain on the `feat/coda-mcp-live-session-url` branch on top of Todo 1's work.
- No data migration: new tool, no existing state to update.
- No config flag — the new tool is unconditionally available once the code lands.
- App restart picks up the new tool registration.
- MCP clients (Genie Code, etc.) will see the new tool listed via `tools/list` and can call it immediately.

## Critique gate

**Cleared** (2026-05-28). Critic verdict: APPROVE WITH CHANGES. All flagged issues incorporated above:

- **MAJOR** — pre-existing env-strip gap in `mcp_create_pty_session` (misses `NPM_TOKEN`, `UV_DEFAULT_INDEX`, `UV_INDEX_*_PASSWORD`, etc.) → added as prerequisite refactor task in §6
- **HIGH-PRIORITY GAP** — FastMCP `instructions` string update for the new tool → added explicitly in §6
- Section 3 expanded with snapshot-semantics, binary-file handling, and empty-export notes
- Section 3a expanded with multi-user side-effect caveat
- Section 6 expanded with SDK-call placement note (calls happen in server process, not PTY subprocess)
- Tool description text guidance integrated (instructions string must mention `coda_inbox` invisibility, no `coda_get_result` integration, Git Folder pre-condition)

Original 10 critique questions, all answered in the critique pass:

1. **Auth model** — Confirmed. Coda's PAT covers both `repos.update()` and `workspace.export()`; no scope gotcha for single-user.
2. **Export performance** — Both SDK loop and `databricks workspace export-dir` CLI are viable; planner picks after a small spike. CLI is faster.
3. **Git Folder vs. ordinary folder** — Hard error is correct. `repos.list()` returns empty for non-Git folders; clear error message.
4. **Branch update side effect** — Acceptable for single-user app; multi-user caveat added to §3a.
5. **Notebook handling** — `ExportFormat.SOURCE` converts notebooks to `.py`/`.scala`/`.sql`. Acceptable; out-of-scope to round-trip back to notebooks.
6. **Concurrent branch race** — Acceptable for single-user; documented as user error.
7. **Disk lifecycle** — UUID-based session IDs prevent collisions; rmtree failure orphans the dir but doesn't break next session.
8. **Prompt seeding** — 2-second sleep is pragmatic; bash buffers stdin if agent is slow to read.
9. **`cwd` kwarg** — Only `coda_interactive` needs it. Additive change, no other callers affected.
10. **Test coverage** — Mocked Databricks SDK is the right MVP approach; E2E against real workspace deferred as nice-to-have behind CI flag.

Plus eight additional critic-eye questions (11–18), all resolved:

11. **Mode separation drift** — No drift. Regression guard test (`test_coda_run_does_not_create_project_dir`) defends the separation.
12. **PTY exhaustion** — Production PTY limit is ~4096; `MAX_CONCURRENT_SESSIONS=5` is nowhere near. macOS dev exhaustion is a known local-test concern, handled via `_pty_skip`.
13. **Project dir collision** — UUID-based IDs make collision probability negligible; `exist_ok=True` on `makedirs` handles the unlikely case.
14. **Pre-condition realism** — Realistic for Genie Code (primary target); secondary clients (Claude Desktop, Cursor) get clear guidance via `instructions` string.
15. **Dirty Workspace UI state** — Export reflects committed HEAD; uncommitted UI edits NOT included. Documented in §3 snapshot-semantics note.
16. **Binary files** — Per-file try/except + skip-and-log added to §3 binary-file note.
17. **`coda_inbox` invisibility** — Documented in `instructions` string per §6.
18. **Tool description text** — Spelled out in §6 (instructions string must explain the new tool's contract).

Spec is ready for planning.
