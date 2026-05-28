# Spec: Broaden `coda_interactive` source to any Workspace folder

**Status:** Draft, pre-critique-gate
**Date:** 2026-05-28
**Branch:** `feat/coda-mcp-interactive-handoff` (continues PR #67)
**Amends:** `docs/superpowers/specs/2026-05-28-coda-interactive-mcp-tool-design.md`

## Goal

Drop the requirement that `coda_interactive`'s `workspace_path` point to a Databricks Workspace **Git Folder**. The path can be any Workspace directory — a Git Folder *or* a plain Workspace folder. The MCP tool only needs the directory to exist in the workspace; how it got there is the caller's concern.

## Why

The original design (PR #67) used the Repos API (`client.repos.list` + `client.repos.update`) to resolve a Git Folder and optionally switch its branch before exporting. Two problems with that:

1. **Unnecessary friction.** Users with a regular Workspace folder (uploaded via the UI, written via the Jobs API, etc.) cannot hand off to `coda_interactive` even though the underlying Workspace export API (`client.workspace.list` + `client.workspace.export`) works for *both* Git Folders and plain folders. The Repos gate excludes a valid use case for no benefit.
2. **Branch convenience overlaps with caller capabilities.** The upstream MCP caller (Genie Code, Claude Desktop) already has Databricks SDK access — if they want a specific branch checked out on a Git Folder, they can do it themselves before calling. The `branch` parameter on `coda_interactive` was duplicating capability that already lives upstream.

Broadening the contract makes the tool surface smaller and the call site simpler. The user's framing: *"It may or may not be backed by git."*

## Changes

### 1. Tool signature

**Before:**
```python
async def coda_interactive(
    prompt: str,
    workspace_path: str,
    branch: str = "",
    agent: str = "claude",
    email: str = "",
) -> str:
```

**After:**
```python
async def coda_interactive(
    prompt: str,
    workspace_path: str,
    agent: str = "claude",
    email: str = "",
) -> str:
```

The `branch` parameter is removed entirely. If the caller wants a Git Folder on a specific branch, they switch it themselves before calling.

### 2. Body of `coda_interactive`

**Removed:**
- `client.repos.list(path_prefix=workspace_path)` lookup
- The exact-match filter (`next((r for r in repos if r.path == workspace_path), None)`)
- The `client.repos.update(repo_id=repo.id, branch=branch)` call

**Added (light validation):**
A single `client.workspace.get_status(workspace_path)` call before export, to give callers a clean error when the path doesn't exist or isn't a directory. This replaces the implicit "empty export" failure mode with an explicit error.

```python
try:
    status = client.workspace.get_status(workspace_path)
except Exception as e:
    return json.dumps({
        "status": "error",
        "error": f"Workspace path not found: {workspace_path}: {e}",
    })

if not _is_directory(status):
    return json.dumps({
        "status": "error",
        "error": f"Workspace path is not a directory: {workspace_path}",
    })
```

`_is_directory` already exists in `workspace_export.py` and works for both real SDK objects and mocks. Re-use it.

### 3. Return shape

**Removed field:** `"branch"`.

**After:**
```json
{
  "status": "launched",
  "viewer_url": "...",
  "agent": "claude",
  "project_dir": "/home/app/.coda/projects/pty-...",
  "workspace_path": "/Workspace/Users/me@db.com/projects/feature-X",
  "instructions": "Open viewer_url to attach. The agent is loaded with the project files exported from Workspace and your kickoff prompt typed. Type the agent's quit command (e.g. /quit) and then `exit` to end the session. Note: git history is NOT available in the session — files are an export, not a clone."
}
```

The `instructions` string is unchanged — it never claimed git history was preserved, so it stays valid for both Git Folders and plain folders.

### 4. Caller pre-condition (spec section 1a rewrite)

**Old contract:** "Project must be a Databricks Workspace Git Folder; commit and push to remote before calling."

**New contract:** "Project must be a directory at `workspace_path` in the Databricks Workspace. Files visible to `workspace.export` (notebooks, source files) will appear in the session. If the directory is a Git Folder and you want a specific branch, switch it on the Git Folder yourself before calling — the export is a server-side snapshot."

### 5. INTERACTIVE HANDOFF instructions string (server-level)

The paragraph in `coda_mcp/mcp_server.py:79` surfaced to upstream LLM callers is rewritten:

**Before (excerpt):**
> The user's project must be a Databricks Workspace Git Folder ... commit and push any local working changes back to the Git Folder's remote before calling.

**After:**
> The user's project must be a directory in the Databricks Workspace (a Git Folder or a plain Workspace folder — either works). Make sure the files you want the agent to see are present at `workspace_path` before calling. If the directory is a Git Folder, ensure the desired branch is checked out and pushed first — the export is a server-side snapshot.

## What does NOT change

- **`export_workspace_tree` helper** — already generic. No code changes in `coda_mcp/workspace_export.py`.
- **PTY lifecycle, agent launch matrix, prompt-seed stabilization** — unchanged.
- **`coda_run` and other tools** — untouched.
- **Three-mode framework table** — Mode 2 column "How invoked" stays the same; the spec for it now reads "any workspace folder, Git Folder or plain."

## Tests to update

In `tests/test_coda_interactive.py`:

1. **Drop:** `test_unknown_workspace_path_returns_error` if it covered the `repos.list` empty-result case → replace with a `workspace.get_status` raises case.
2. **Drop:** `test_branch_update_succeeds` and `test_branch_update_fails` — branch param is gone.
3. **Drop:** any test asserting `"branch"` in the return JSON.
4. **Update:** the happy-path test mock — remove `client.repos.list` and `client.repos.update` setup; add `client.workspace.get_status` returning a directory-typed mock.
5. **Add:** `test_plain_workspace_folder_succeeds` — covers a `workspace.get_status` returning ObjectType.DIRECTORY for a path that is NOT a Repo. Should reach the export step and succeed.
6. **Add:** `test_workspace_path_not_directory_returns_error` — `workspace.get_status` returns a FILE-typed mock; tool returns `"not a directory"` error without creating a PTY.

Expected test count delta: ~−3 / +2 = net −1 test.

## Tests for the SDK validation step

Since we're relying on `client.workspace.get_status` to validate, add a mock-level test that verifies:
- A non-existent path raises an exception from `get_status` → tool returns `"Workspace path not found"` error.
- A directory path returns object_type=DIRECTORY → tool proceeds.
- A file path returns object_type=FILE → tool returns `"not a directory"` error.

These belong in the same file as the existing tool tests.

## Out of scope (deferred)

- **Single-file `workspace_path`.** Not supported. If a caller wants to ship a single file, they create a directory containing it. Keeps `_export_recursive` semantics simple.
- **Recovering branch info from a Git Folder for the response.** Not added — caller already knows the branch state, and surfacing it in the response would be ornamental.
- **`workspace.get_status` for the export-failed cleanup path.** The existing `try/except` around `export_workspace_tree` still runs; this change does not affect cleanup.

## Migration notes

PR #67 is open and not yet merged — no shipped consumers depend on the `branch` parameter. Removing it is safe. The PR description should note the API change.

## Risks

- **A caller that calls with `branch="main"`** (positional or kwarg) will now error with `TypeError: unexpected keyword argument 'branch'`. Acceptable because no consumer has shipped. The FastMCP runtime surfaces this as a tool-validation error on the caller side.
- **`workspace.get_status` adds one extra API call** to the happy path. Negligible — same network plane as the export calls that follow.

## Acceptance criteria

- `coda_interactive` accepts ANY Workspace directory path, Git Folder or plain.
- `coda_interactive` no longer accepts a `branch` parameter.
- The tool gives a clean error when the path doesn't exist or isn't a directory.
- All existing tests pass (after the test updates above).
- The PR description for #67 reflects the simpler contract.
