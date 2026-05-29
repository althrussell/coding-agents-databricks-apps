# Spec: `coda_interactive` Terminal-Side Workspace Pull

**Status:** Draft, design-critic passed (SOUND-WITH-FIXES, all fixes folded in)
**Date:** 2026-05-28
**Branch:** `feat/coda-mcp-interactive-handoff` (continues PR #67)
**Supersedes the export mechanism in:** `docs/superpowers/specs/2026-05-28-coda-interactive-mcp-tool-design.md`

## Problem

`coda_interactive` currently does a **server-side** export of a Databricks Workspace folder into a local project directory via `WorkspaceClient().workspace.export(...)` (module `coda_mcp/workspace_export.py`), then launches an agent in that directory. In the deployed app this produces an **empty directory** — the agent has no idea about the user's files.

### Confirmed root cause

The deployed app runs as its **own service principal** (`app-167dcd mcp-test-coda-labs-feat`, client_id `460e920e-…`), confirmed via the Apps API. The MCP server calls `WorkspaceClient()` with no args → that resolves to the **app SP**. The app SP can `get_status` the user's `/Users/<user>/WAM` folder (so the tool reports `"launched"`) but **cannot `list`/`export` its contents**. `workspace_export.py._export_recursive` **swallows** those errors (`logger.warning` + `return`), so `export_workspace_tree` raises nothing and the agent launches over an empty directory.

### Evidence

- **REST as the user** (curl): `list`, `get_status`, and `export` (SOURCE and AUTO, with and without `/Workspace` prefix) all succeed for the 5 `.md` files in WAM. So the API, the export format, and the path prefix are NOT the problem.
- **Live CoDA terminal:** `databricks current-user me` returns the **user** (`sathish.gangichetty@databricks.com`), not the app SP. `databricks workspace list /Users/.../WAM` from the terminal returns the 5 files.
- **Conclusion:** the identity that can read the files is the **terminal** (the app owner / user), not the **MCP server** (app SP). Move the file access to the terminal.

## Goal

Stop exporting server-side. `coda_interactive` hands the location to the **terminal** (authenticated as the user) and pulls the files there with `databricks workspace export-dir`, then launches the agent in the pulled directory. Net effect: the agent starts in a directory that actually contains the workspace files, and any failure is visible (a real tool error or terminal output) instead of silently swallowed.

## Non-goals

- `/Workspace` FUSE-mount access — `export-dir` works regardless of whether the mount exists. Not pursued.
- Pushing edits back to the Workspace (`import-dir`) — the agent can do that itself if asked. Out of scope.
- Git Folder branch checkout — caller's responsibility, as before.
- Changing `coda_run` (mode 3) or any other tool.
- Hardening the existing `_wait_for_agent_ready` heuristic beyond what this change needs (see Risks).

---

## Design

### New `coda_interactive` flow

```
1. Validate `agent` ∈ _ALLOWED_AGENTS                       (unchanged)
2. Verify PTY hooks wired (_app_create_session/_app_send_input)  (unchanged)
3. pty_session_id = _app_create_session(label=f"{agent}-interactive", replay_only=False)
4. project_dir = os.path.join(os.path.expanduser("~/.coda/projects"), pty_session_id)
   os.makedirs(project_dir, exist_ok=True)
5. name        = _safe_dirname(workspace_path)         # e.g. "WAM"
   source_path = _normalize_workspace_path(workspace_path)   # strip leading /Workspace
6. Type ONE chained line into the PTY (runs as the user):
   cd <project_dir> && databricks workspace export-dir <source_path> ./<name> && cd <name>
7. await _wait_for_output_stable(pty, _EXPORT_MAX_WAIT_S, _EXPORT_STABILITY_S)
      # wait for the pull to finish — shell goes truly idle after export-dir,
      # so stabilization here is reliable (no agent-cold-start gap to confuse it)
8. SERVER-SIDE post-condition check (does NOT depend on the app SP — stats local disk):
      target_dir = os.path.join(project_dir, name)
      if not os.path.isdir(target_dir) or not os.listdir(target_dir):
          close PTY; shutil.rmtree(project_dir, ignore_errors=True)
          return {"status":"error", "error": "<no files pulled; check path + access>"}
9. Launch the agent (fresh — identical to the proven existing path):
      _app_send_input(pty, _AGENT_LAUNCH_CMDS[agent] + "\n")
      await _wait_for_agent_ready(pty)         # existing 5s/1s window, unchanged behavior
10. Paste kickoff prompt, prefixed with a context line naming workspace_path:
      "Your working directory contains files exported from the Databricks
       Workspace path <workspace_path>.\n\n<prompt>"
11. return {"status":"launched", "viewer_url", "agent", "project_dir": target_dir,
            "workspace_path", "instructions"}
```

### Why split the waits (design-critic CRITICAL fix)

The naive design (`cd && export-dir && cd`, then launch agent, then a single `_wait_for_agent_ready`) risks `_wait_for_agent_ready` returning **early** in the silent gap between `export-dir` finishing and the agent's TUI producing output — pasting the prompt into a half-initialized agent or the shell.

The split removes that risk:
- **Step 7** waits for the *pull* to finish. After `export-dir` completes the shell is genuinely idle (output stops), so stabilization is reliable. It is NOT waiting across an agent cold-start.
- **Step 9** waits for the *agent* exactly the way the current working code does (launch → wait → prompt), with no preceding network op. It inherits the known-good behavior.
- **Step 8** (the filesystem post-check) is the safety net: if the pull produced nothing, we error out cleanly instead of launching into an empty directory. This also resolves the `&&`-failure ambiguity — a failed `export-dir` short-circuits the chain, leaves `target_dir` absent, and step 8 turns that into a proper tool error.

### Helpers

```python
def _safe_dirname(workspace_path: str) -> str:
    """Local directory name for the pulled folder = sanitized basename."""
    base = os.path.basename(workspace_path.rstrip("/"))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return safe or "workspace"


def _normalize_workspace_path(workspace_path: str) -> str:
    """Canonical Workspace API path: drop the /Workspace FUSE prefix if present.

    The deployed terminal's CLI uses the unprefixed form (/Users/...); REST
    accepts both, but normalizing matches what the CLI expects and is harmless.
    """
    p = workspace_path.rstrip("/")
    if p.startswith("/Workspace/"):
        p = p[len("/Workspace"):]   # "/Workspace/Users/x" -> "/Users/x"
    return p
```

### Wait-helper refactor (backward compatible)

Generalize the existing poller so the export wait can use a longer budget while `coda_run`'s call site stays unchanged:

```python
_PROMPT_SEED_MAX_WAIT_S = 5.0       # existing — agent TUI settle
_PROMPT_SEED_STABILITY_S = 1.0      # existing
_EXPORT_MAX_WAIT_S = 120.0          # new — generous; export-dir prints per-file so it won't prematurely stabilize on a slow pull
_EXPORT_STABILITY_S = 1.5           # new

async def _wait_for_output_stable(pty_session_id, max_wait, stability):
    # exact body of the current _wait_for_agent_ready, parametrized on max_wait/stability

async def _wait_for_agent_ready(pty_session_id):
    await _wait_for_output_stable(pty_session_id, _PROMPT_SEED_MAX_WAIT_S, _PROMPT_SEED_STABILITY_S)
```

`coda_run` already calls `_wait_for_agent_ready` — that call and its behavior are unchanged.

### `databricks workspace export-dir` (verified)

`databricks workspace export-dir SOURCE_PATH TARGET_PATH`:
- Exports a directory recursively from the Workspace to the local filesystem.
- **Creates** `TARGET_PATH`.
- Auto-appends notebook extensions (`.py/.scala/.sql/.r`) by language — natively replaces the hand-rolled logic in `workspace_export.py`.
- `--overwrite` flag exists; not needed here (the session `<name>` dir is fresh).

### Deletions

- `coda_mcp/workspace_export.py` — whole module.
- `tests/test_workspace_export.py` — whole file.
- In `coda_mcp/mcp_server.py`: remove `from coda_mcp.workspace_export import export_workspace_tree, _is_directory`, the `WorkspaceClient` import guard (verify no other use first), the `WorkspaceClient()` instantiation, the `get_status` validation, and the `_is_directory` call.
- `tests/test_replay_only_flag.py:166` — only a **comment** mentions `export_workspace_tree` (not an import). Refresh the wording so it doesn't reference a deleted symbol. Non-breaking.

### Kept

PTY creation (`replay_only=False`), `project_dir` + `os.makedirs`, `_wait_for_agent_ready` (now a wrapper), `viewer_url`, `_ALLOWED_AGENTS`, `_AGENT_LAUNCH_CMDS`, the existing try/except resource cleanup. `email` stays in the signature (upstream callers pass it; currently unused, reserved).

### Cleanup on session end (no new code)

`app.py:terminate_session` already `shutil.rmtree`s `os.path.expanduser("~/.coda/projects/<pty_session_id>")` on both graceful exit and idle-reaper paths. The pulled `<name>` dir lives inside `project_dir`, so it is cleaned up automatically.

---

## Error handling

| Situation | Behavior |
|-----------|----------|
| Unknown `agent` | Immediate `{"status":"error"}` (unchanged) |
| PTY hooks not wired | Immediate `{"status":"error"}` (unchanged) |
| Bad `workspace_path` / no access / empty folder | `export-dir` fails or pulls nothing → step-8 FS check fails → close PTY, rmtree, `{"status":"error", "error": "No files were pulled from <path>; check it exists and you have read access."}` |
| Pull succeeds | Agent launches in `target_dir`; prompt seeded; `{"status":"launched", viewer_url, ...}` |
| Unexpected exception anywhere | Catch-all: close PTY if created, rmtree `project_dir`, `{"status":"error"}` (unchanged) |

No server-side path validation via `WorkspaceClient` — the app SP can't reliably validate the user's folder anyway (that was the bug). The step-8 FS check is the validation, and it reads the local disk the *terminal* wrote (correct identity).

---

## Testing strategy

### `tests/test_workspace_export.py` — DELETE

### `tests/test_replay_only_flag.py` — refresh the stale comment at line 166 (no logic change)

### `tests/test_coda_interactive.py` — rewrite

Mock `_app_create_session` (returns a fake `pty_session_id`), `_app_send_input` (records inputs; on the pull command, side-effect creates `target_dir` + a dummy file to simulate a successful `export-dir`), `_app_close_session`, and the wait helpers (return immediately). Set `HOME` to a `tmp_path` so `project_dir` resolves under the test sandbox.

| Test | Pins |
|------|------|
| `test_pull_command_is_sent_first` | First `_app_send_input` is the chained `cd … && databricks workspace export-dir <source> ./<name> && cd <name>`; source has no `/Workspace` prefix; `<name>` is the sanitized basename |
| `test_agent_launches_after_successful_pull` | After the simulated pull creates files, the launch command (`_AGENT_LAUNCH_CMDS[agent]`) is sent |
| `test_prompt_seeded_with_context_line` | Final input starts with the "exported from the Databricks Workspace path <workspace_path>" line, then the user prompt |
| `test_empty_pull_returns_error_and_no_launch` | When the pull side-effect creates nothing, result is `{"status":"error"}`, PTY is closed, and the launch command is NEVER sent |
| `test_no_workspaceclient_or_get_status_called` | `WorkspaceClient` is not referenced (import removed); no `get_status` call path |
| `test_happy_path_returns_launched_with_viewer_url` | `{"status":"launched"}`, `viewer_url` present, `project_dir` == `target_dir` |
| `test_unknown_agent_rejected` | Unknown agent → error (unchanged) |
| `test_pty_hook_not_wired` | Hooks `None` → error (unchanged) |
| `test_agent_matrix` | Each of claude/hermes/codex/gemini/opencode sends the right launch cmd |
| `test_no_blocking_sleep` | `coda_interactive` source contains no `time.sleep(` (async regression guard, kept) |

### `tests/test_mcp_server.py` (or wherever helpers are tested) — add

| Test | Pins |
|------|------|
| `test_safe_dirname_basename` | `/Users/x/WAM` → `WAM`; trailing slash stripped |
| `test_safe_dirname_sanitizes` | spaces / special chars → `_` |
| `test_safe_dirname_empty_fallback` | `"/"` or `""` → `"workspace"` |
| `test_normalize_strips_workspace_prefix` | `/Workspace/Users/x/WAM` → `/Users/x/WAM` |
| `test_normalize_leaves_plain_path` | `/Users/x/WAM` → `/Users/x/WAM` |
| `test_wait_for_agent_ready_still_wrapper` | `_wait_for_agent_ready` delegates to `_wait_for_output_stable` with the prompt-seed constants |

### Regression

Run together (per the established flake note — `test_replay_only_flag.py::test_coda_run_creates_pty_with_replay_only_true` is PTY-fd flaky in multi-file runs; re-run alone if it fails):

```
uv run pytest tests/test_coda_interactive.py tests/test_mcp_server.py tests/test_replay_only_flag.py tests/test_task_manager.py tests/test_databricks_preamble.py -v
```

---

## Acceptance criteria

1. `coda_interactive` no longer imports or calls `workspace_export` / `WorkspaceClient` / `get_status`.
2. `coda_mcp/workspace_export.py` and `tests/test_workspace_export.py` are deleted; no remaining importers.
3. `_safe_dirname` and `_normalize_workspace_path` exist with the specified behavior.
4. `_wait_for_output_stable(pty, max_wait, stability)` exists; `_wait_for_agent_ready` is a wrapper preserving the `5.0/1.0` budget; `coda_run`'s call is unaffected.
5. The first PTY input is the chained pull command using the normalized (unprefixed) source path and the sanitized `<name>`.
6. The agent launch command is sent **only** when the post-pull FS check finds files; otherwise a `{"status":"error"}` is returned and the PTY is closed.
7. The kickoff prompt is prefixed with the context line naming `workspace_path`.
8. All new/updated tests pass; existing suites (minus the known PTY-fd flake) stay green.

---

## Risks

1. **Slow / huge folders.** `_EXPORT_MAX_WAIT_S = 120s`; if a pull exceeds it, step 7 returns while `export-dir` is still running and step 8 may see a partial dir and (incorrectly) proceed. Mitigation: 120s is generous for the interactive-handoff use case (docs / small projects); `export-dir` prints per-file so it won't prematurely stabilize during an active pull. Larger-folder support is a future tweak, not in scope.
2. **HOME equivalence.** Step 4/8 resolve `project_dir` via `os.path.expanduser` in the MCP-server process; the PTY `cd`/write uses that same absolute string and the terminal's `$HOME` resolves identically in the deployed container (observed: both `/app/python/source_code/.coda/...`). If a future environment gave the server and PTY different `$HOME`, the `cd` and FS check would diverge. Documented assumption; matches existing code (the deleted export and `terminate_session` cleanup already rely on it).
3. **`_wait_for_agent_ready` cold-start (pre-existing).** The agent wait can still, in principle, fire during a long agent cold-start silence — but this is the current production behavior, unchanged by this spec. A marker-based ready gate is a possible future hardening, explicitly out of scope here.
4. **`export-dir` on `/Workspace`-prefixed paths.** Mitigated by `_normalize_workspace_path` (we pass the `/Users/...` form the CLI expects and that REST verified).
