# `coda_interactive` Broaden Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop the Git-Folder requirement from `coda_interactive`. `workspace_path` accepts any Databricks Workspace directory. Remove the `branch` parameter. Add a `workspace.get_status` validation step.

**Architecture:** Single MCP tool simplification on the open PR #67. We replace the Repos API lookup (`client.repos.list` + `client.repos.update`) with a single existence/type check (`client.workspace.get_status` → `_is_directory`). The export helper (`export_workspace_tree`) is unchanged because it already uses the generic Workspace API. Tests are rewritten to match: drop branch-related tests, swap `repos.list` mocks for `workspace.get_status` mocks, add a not-a-directory case.

**Tech Stack:** Python 3.11, FastMCP, databricks-sdk WorkspaceClient, pytest, MagicMock.

---

## Files modified by this plan

- **Modify:** `coda_mcp/mcp_server.py` — remove `branch` param, remove repos lookup, add `get_status` validation, update INTERACTIVE HANDOFF instructions paragraph and tool docstring, update import line
- **Modify:** `tests/test_coda_interactive.py` — drop 3 tests, update 4 tests, add 2 tests
- **No change:** `coda_mcp/workspace_export.py` — already generic; we just re-use its `_is_directory` helper via import

## Pre-flight context

- Worktree path: `/Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp`
- Branch: `feat/coda-mcp-interactive-handoff` (PR #67, open)
- Run tests with `uv run pytest <path>` (per user's `always use uv` directive)
- Commit identity: `-c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty"` (per user's directive). No AI co-author lines.

The current `coda_interactive` body is at `coda_mcp/mcp_server.py:370-517`. The current INTERACTIVE HANDOFF paragraph is at `coda_mcp/mcp_server.py:79-93`. The current test file is `tests/test_coda_interactive.py` (385 lines, 11 tests).

---

## Task 1: Rewrite tests for the broadened contract (RED state)

This task replaces the test file's mocking shape and assertions. Implementation in Task 2 is what makes them pass.

**Files:**
- Modify: `tests/test_coda_interactive.py` (drop 3 tests, update 4 tests, add 2 tests)

- [ ] **Step 1: Delete the three branch/git-folder-only tests**

These three tests no longer make sense because the corresponding code paths are being removed. Remove them entirely from `tests/test_coda_interactive.py`:

1. `test_coda_interactive_workspace_path_not_found` (lines 42-58) — tests `repos.list()` returning empty. The new code uses `workspace.get_status`, not `repos.list`. A different test covers the missing-path case.
2. `test_coda_interactive_branch_update_failure` (lines 61-83) — tests `repos.update` raising. The `branch` parameter is going away entirely.
3. `test_coda_interactive_skips_branch_update_when_empty` (lines 86-107) — tests that `repos.update` isn't called when branch is empty. The `branch` parameter is going away entirely.

- [ ] **Step 2: Update the four tests that have stale mock setup**

These four tests currently set up `fake_repo` and `fake_client.repos.list.return_value = [fake_repo]`. After the change, `coda_interactive` no longer calls `repos.list`. Replace that scaffolding with a `workspace.get_status` mock returning a directory-typed object.

Add this helper at the top of the file (just after `_no_wait`):

```python
def _make_dir_status():
    """Build a mock object_type=DIRECTORY response from workspace.get_status."""
    from unittest.mock import MagicMock
    status = MagicMock()
    status.object_type = "DIRECTORY"
    return status
```

Then update these four tests by replacing the `fake_repo` + `fake_client.repos.list.return_value = [fake_repo]` block with:

```python
fake_client = MagicMock()
fake_client.workspace.get_status.return_value = _make_dir_status()
```

The tests:
- `test_coda_interactive_export_failure_cleans_partial_dir` (currently line 110)
- `test_coda_interactive_happy_path_sends_agent_command_and_prompt` (currently line 164)
- `test_coda_interactive_agent_command_matrix` (currently line 224)
- `test_coda_interactive_does_not_use_blocking_sleep` (currently line 272)

In `test_coda_interactive_happy_path_sends_agent_command_and_prompt`, also remove the assertion line referencing `branch` in the return shape if present (re-check after edit — current return shape includes `"branch"`; the new shape does not). The current test does not assert on `result["branch"]`, so no change needed there, but verify after edit.

- [ ] **Step 3: Add `test_coda_interactive_workspace_path_does_not_exist`**

Append to the file:

```python
def test_coda_interactive_workspace_path_does_not_exist(monkeypatch):
    """If workspace.get_status raises, return error and don't proceed to PTY."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    fake_client = MagicMock()
    fake_client.workspace.get_status.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    pty_created = []
    monkeypatch.setattr(
        mcp_server, "_app_create_session",
        lambda **kw: pty_created.append(kw) or "should-not-be-used",
    )

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/nonexistent",
    ))
    result = json.loads(result_str)

    assert result["status"] == "error"
    assert "not found" in result["error"].lower() or "does_not_exist" in result["error"].lower()
    # No PTY may be created if validation fails.
    assert pty_created == [], f"PTY must not be created when workspace_path is invalid; got {pty_created}"
```

- [ ] **Step 4: Add `test_coda_interactive_workspace_path_not_directory`**

Append to the file:

```python
def test_coda_interactive_workspace_path_not_directory(monkeypatch):
    """If workspace.get_status returns object_type=FILE (or anything not DIRECTORY), return error."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    file_status = MagicMock()
    file_status.object_type = "FILE"
    fake_client = MagicMock()
    fake_client.workspace.get_status.return_value = file_status
    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    pty_created = []
    monkeypatch.setattr(
        mcp_server, "_app_create_session",
        lambda **kw: pty_created.append(kw) or "should-not-be-used",
    )

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/some-file.py",
    ))
    result = json.loads(result_str)

    assert result["status"] == "error"
    assert "directory" in result["error"].lower()
    assert pty_created == [], "PTY must not be created when workspace_path is not a directory"
```

- [ ] **Step 5: Add `test_coda_interactive_no_branch_parameter`**

Signature regression guard so the `branch` arg cannot quietly come back. Append to the file:

```python
def test_coda_interactive_no_branch_parameter():
    """The branch parameter must not exist on coda_interactive's signature."""
    import inspect
    from coda_mcp import mcp_server

    sig = inspect.signature(mcp_server.coda_interactive)
    assert "branch" not in sig.parameters, (
        f"coda_interactive must not accept a `branch` parameter (got {list(sig.parameters)}). "
        f"The broadened contract handles git-folder branch state on the caller side."
    )
```

- [ ] **Step 6: Run the test file — expect failures**

Run: `uv run pytest tests/test_coda_interactive.py -v`

Expected: At least the two new tests (`workspace_path_does_not_exist`, `workspace_path_not_directory`), the signature guard (`no_branch_parameter`), and the four updated mock-shape tests all FAIL — because `coda_interactive` still uses `repos.list` and still accepts `branch`. The unchanged tests (`unknown_agent`, `default_agent_is_claude`, the three `_wait_for_agent_ready` tests) should still PASS.

This is the intended RED state — proves the new tests actually exercise the new code path.

- [ ] **Step 7: Commit the tests**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add tests/test_coda_interactive.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "test: rewrite coda_interactive tests for broadened workspace-folder contract"
```

---

## Task 2: Simplify `coda_interactive` implementation (GREEN state)

**Files:**
- Modify: `coda_mcp/mcp_server.py` (signature, body, import, return shape)

- [ ] **Step 1: Update the import line to include the directory check helper**

In `coda_mcp/mcp_server.py:31`, change:

```python
from coda_mcp.workspace_export import export_workspace_tree
```

to:

```python
from coda_mcp.workspace_export import export_workspace_tree, _is_directory
```

`_is_directory` is currently module-private in `workspace_export.py:35`. We import it directly rather than aliasing for two reasons: (a) it is a stable, narrowly-scoped helper already used internally; (b) renaming it would force an unrelated edit. Python permits underscore imports; the cost is one symbol shared across two modules in the same package.

- [ ] **Step 2: Replace the function signature and body**

In `coda_mcp/mcp_server.py:370-517`, replace the entire `async def coda_interactive(...)` definition. The full new function body:

```python
@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    ),
)
async def coda_interactive(
    prompt: str,
    workspace_path: str,
    agent: str = "claude",
    email: str = "",
) -> str:
    """Launch an interactive agent session in CoDA, handed off via a viewer URL.

    The MCP caller passes a Databricks Workspace directory path (a Git Folder
    or a plain Workspace folder — either works). Coda exports its file tree,
    launches the chosen agent (claude default) in that directory, auto-types
    ``prompt`` as the first user input, and returns a ``viewer_url`` the
    calling user opens in a browser to drive the session.

    Pre-condition: ``workspace_path`` must point to a directory that already
    exists in the Databricks Workspace. If the directory is a Git Folder and
    the caller wants a specific branch checked out, they must do that
    themselves before calling — the export is a server-side snapshot.

    Interactive sessions do NOT appear in ``coda_inbox`` and ``coda_get_result``
    will not return anything for them. The viewer URL is the only handle.

    Allowed agents: claude (default), hermes, codex, gemini, opencode.
    """
    if agent not in _ALLOWED_AGENTS:
        return json.dumps({
            "status": "error",
            "error": f"Unknown agent: {agent!r}. Allowed: {sorted(_ALLOWED_AGENTS)}",
        })

    if WorkspaceClient is None:
        return json.dumps({
            "status": "error",
            "error": "databricks-sdk not installed",
        })

    client = WorkspaceClient()

    # Validate that the path exists and is a directory.
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

    # Create PTY FIRST so we have its session_id for the project_dir name.
    if _app_create_session is None:
        return json.dumps({
            "status": "error",
            "error": "PTY hook not wired",
        })

    pty_session_id = None
    project_dir = None
    try:
        pty_session_id = _app_create_session(
            label=f"{agent}-interactive",
            replay_only=False,
        )

        # Build the project dir at the canonical path keyed by PTY id.
        project_dir = os.path.join(
            os.path.expanduser("~/.coda/projects"),
            pty_session_id,
        )

        # Export the Workspace tree into project_dir.
        try:
            export_workspace_tree(client, workspace_path, project_dir)
        except Exception as e:
            # Close the PTY and clean up the partial dir.
            if _app_close_session is not None:
                try:
                    _app_close_session(pty_session_id)
                except Exception:
                    pass
            if os.path.isdir(project_dir):
                shutil.rmtree(project_dir, ignore_errors=True)
            return json.dumps({
                "status": "error",
                "error": f"Failed to export workspace tree: {e}",
            })

        # cd into the project dir.
        if _app_send_input is None:
            return json.dumps({
                "status": "error",
                "error": "PTY send hook not wired",
            })
        _app_send_input(pty_session_id, f"cd {shlex.quote(project_dir)}\n")

        # Launch the agent.
        launch_cmd = _AGENT_LAUNCH_CMDS[agent]
        _app_send_input(pty_session_id, launch_cmd + "\n")

        # Wait briefly for agent initialization, then paste the prompt.
        await _wait_for_agent_ready(pty_session_id)
        _app_send_input(pty_session_id, prompt + "\n")

        viewer_url = url_builder.build_viewer_url(pty_session_id)

        return json.dumps({
            "status": "launched",
            "viewer_url": viewer_url,
            "agent": agent,
            "project_dir": project_dir,
            "workspace_path": workspace_path,
            "instructions": (
                "Open viewer_url to attach. The agent is loaded with the "
                "project files exported from Workspace and your kickoff "
                "prompt typed. Type the agent's quit command (e.g. /quit) "
                "and then `exit` to end the session. Note: git history is "
                "NOT available in the session — files are an export, not "
                "a clone."
            ),
        })
    except Exception as e:
        # Catch-all: ensure no resource leak.
        if pty_session_id and _app_close_session is not None:
            try:
                _app_close_session(pty_session_id)
            except Exception:
                pass
        if project_dir and os.path.isdir(project_dir):
            shutil.rmtree(project_dir, ignore_errors=True)
        return json.dumps({
            "status": "error",
            "error": f"coda_interactive failed: {e}",
        })
```

Key changes vs. the existing body:
- `branch: str = ""` parameter removed.
- `client.repos.list` / exact-match filter / `client.repos.update` block removed.
- Replaced by `client.workspace.get_status(workspace_path)` + `_is_directory` check.
- `"branch": branch,` dropped from the return JSON.
- Docstring rewritten to say "Git Folder or plain Workspace folder" and drop the "commit and push to remote" admonition.

- [ ] **Step 3: Run the test file — expect green**

Run: `uv run pytest tests/test_coda_interactive.py -v`

Expected: All tests PASS. If any fail, fix the implementation (not the tests) and re-run.

- [ ] **Step 4: Run the full unit test suite to catch regressions**

Run: `uv run pytest tests/ -v --no-header -x` (stop on first failure)

Expected: All previously-passing tests still pass. The skipped PTY-gated and Docker-gated tests stay skipped (those auto-skip on this machine; no behaviour to verify here).

If unrelated tests fail, stop and investigate before committing.

- [ ] **Step 5: Commit the implementation**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: coda_interactive accepts any Workspace folder, drop branch param

Replaces the Repos API lookup (repos.list + repos.update) with a single
workspace.get_status check. Caller is now responsible for managing
Git Folder branch state. Workspace path can be a Git Folder or a plain
Workspace folder — either works."
```

---

## Task 3: Update INTERACTIVE HANDOFF instructions string

The server-level instructions string surfaced to upstream LLM callers still says "must be a Git Folder ... commit and push to remote." Rewrite to match the broadened contract.

**Files:**
- Modify: `coda_mcp/mcp_server.py:79-93` (INTERACTIVE HANDOFF paragraph in the `mcp = FastMCP(instructions=...)` block)

- [ ] **Step 1: Write a test that pins the instructions string content**

Append to `tests/test_coda_interactive.py`:

```python
def test_interactive_handoff_instructions_describe_broadened_contract():
    """The server-level INTERACTIVE HANDOFF paragraph must reflect the broadened contract."""
    from coda_mcp import mcp_server

    instructions = mcp_server.mcp.instructions

    # Must mention coda_interactive.
    assert "coda_interactive" in instructions

    # Must NOT still claim a Git Folder is required.
    lowered = instructions.lower()
    assert "must be a databricks workspace git folder" not in lowered, (
        "Instructions still require a Git Folder — broadened contract was not applied."
    )
    assert "commit and push" not in lowered, (
        "Instructions still tell the caller to commit and push — only relevant for Git Folders, "
        "but the broadened contract accepts plain folders too."
    )

    # Must mention that plain folders work.
    # Either "git folder or" phrasing, or "plain workspace folder" — accept either.
    assert (
        "git folder or" in lowered
        or "plain workspace folder" in lowered
        or "plain folder" in lowered
    ), "Instructions must mention that plain Workspace folders are accepted."
```

Run: `uv run pytest tests/test_coda_interactive.py::test_interactive_handoff_instructions_describe_broadened_contract -v`

Expected: FAIL — the current instructions string still says "must be a Databricks Workspace Git Folder."

- [ ] **Step 2: Rewrite the INTERACTIVE HANDOFF paragraph in `mcp_server.py:79-93`**

In `coda_mcp/mcp_server.py`, find the block beginning at line 79:

```python
        "INTERACTIVE HANDOFF (coda_interactive): When the user wants a human to "
        "drive a coding agent in CoDA — not autonomous execution — call "
        "coda_interactive instead of coda_run. The user's project must be a "
        "Databricks Workspace Git Folder, and any in-progress changes must be "
        "committed and pushed to the Git Folder's remote BEFORE the call. The tool "
        "exports the committed HEAD state into a Coda-local directory, launches "
        "the chosen agent (claude default; also hermes, codex, gemini, opencode), "
        "and types the prompt as the first user input. The return shape includes "
        "a viewer_url the user opens to attach — share it immediately in plain "
        "text; it is the only handle to the session, and the user drives it until "
        "they exit. Interactive sessions do NOT appear in coda_inbox, and "
        "coda_get_result returns nothing for them — do not try to poll or fetch "
        "results. Note that git history is NOT available inside the session "
        "(files-only export); if the user needs history context, include a git "
        "log summary in the prompt string."
```

Replace it with:

```python
        "INTERACTIVE HANDOFF (coda_interactive): When the user wants a human to "
        "drive a coding agent in CoDA — not autonomous execution — call "
        "coda_interactive instead of coda_run. The user's project must be a "
        "directory in the Databricks Workspace (a Git Folder or a plain "
        "Workspace folder — either works); make sure the files you want the "
        "agent to see are present at workspace_path before calling. If the "
        "directory is a Git Folder, ensure the desired branch is checked out "
        "and pushed first — the export is a server-side snapshot. The tool "
        "exports the directory into a Coda-local directory, launches the "
        "chosen agent (claude default; also hermes, codex, gemini, opencode), "
        "and types the prompt as the first user input. The return shape "
        "includes a viewer_url the user opens to attach — share it "
        "immediately in plain text; it is the only handle to the session, "
        "and the user drives it until they exit. Interactive sessions do "
        "NOT appear in coda_inbox, and coda_get_result returns nothing for "
        "them — do not try to poll or fetch results. Note that git history "
        "is NOT available inside the session (files-only export); if the "
        "user needs history context, include a git log summary in the "
        "prompt string."
```

- [ ] **Step 3: Run the pinned-instructions test plus full suite**

Run: `uv run pytest tests/test_coda_interactive.py -v`
Expected: All PASS (including the new instructions test).

Run: `uv run pytest tests/ -v --no-header`
Expected: All previously-passing tests still pass.

- [ ] **Step 4: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_server.py tests/test_coda_interactive.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: update INTERACTIVE HANDOFF instructions for broadened contract

Tells upstream LLM callers that workspace_path can be either a Git Folder
or a plain Workspace folder. Drops the 'commit and push' admonition that
only applied to Git Folders."
```

---

## Task 4: Push and update PR #67 description

**Files:**
- None (remote/PR update)

- [ ] **Step 1: Verify the branch's git state**

```bash
git status
git log --oneline origin/feat/coda-mcp-interactive-handoff..HEAD
```

Expected: Clean working tree. Three new commits since the previous remote head (tests rewrite, impl, instructions string).

- [ ] **Step 2: Push the branch**

```bash
git push origin feat/coda-mcp-interactive-handoff
```

Expected: Successful fast-forward.

- [ ] **Step 3: Update PR #67 description**

Add a "Follow-up: broadened source" section at the bottom of the PR body via `gh pr edit` (or, if gh CLI's TLS bug hits, via curl + REST). Content:

```
## Follow-up: broadened source contract

`coda_interactive` no longer requires a Databricks Workspace **Git Folder**.
Any Workspace directory (Git Folder or plain Workspace folder) is accepted.
The `branch` parameter has been removed — callers manage Git Folder branch
state themselves before calling.

API change (no shipped consumers — safe):
- `coda_interactive(prompt, workspace_path, branch=..., agent=..., email=...)` →
  `coda_interactive(prompt, workspace_path, agent=..., email=...)`
- Return shape: `"branch"` key dropped.

Validation is now a `workspace.get_status` call with a directory-type check
(replaces the `repos.list` + exact-match filter).
```

Try the gh path first:

```bash
gh pr edit 67 --body-file <(gh pr view 67 --json body -q .body; echo; echo; cat <<'EOF'
## Follow-up: broadened source contract

`coda_interactive` no longer requires a Databricks Workspace **Git Folder**.
Any Workspace directory (Git Folder or plain Workspace folder) is accepted.
The `branch` parameter has been removed — callers manage Git Folder branch
state themselves before calling.

API change (no shipped consumers — safe):
- `coda_interactive(prompt, workspace_path, branch=..., agent=..., email=...)` →
  `coda_interactive(prompt, workspace_path, agent=..., email=...)`
- Return shape: `"branch"` key dropped.

Validation is now a `workspace.get_status` call with a directory-type check
(replaces the `repos.list` + exact-match filter).
EOF
)
```

If gh fails with the known `x509: OSStatus -26276` issue on this machine, fall back to curl:

```bash
TOKEN=$(gh auth token)
EXISTING_BODY=$(curl -s -k -H "Authorization: token $TOKEN" \
  https://api.github.com/repos/databrickslabs/coding-agents-databricks-apps/pulls/67 | jq -r .body)

NEW_BODY="$EXISTING_BODY

## Follow-up: broadened source contract

\`coda_interactive\` no longer requires a Databricks Workspace **Git Folder**.
Any Workspace directory (Git Folder or plain Workspace folder) is accepted.
The \`branch\` parameter has been removed — callers manage Git Folder branch
state themselves before calling.

API change (no shipped consumers — safe):
- \`coda_interactive(prompt, workspace_path, branch=..., agent=..., email=...)\` →
  \`coda_interactive(prompt, workspace_path, agent=..., email=...)\`
- Return shape: \`\"branch\"\` key dropped.

Validation is now a \`workspace.get_status\` call with a directory-type check
(replaces the \`repos.list\` + exact-match filter)."

jq -n --arg body "$NEW_BODY" '{body: $body}' | curl -s -k -X PATCH \
  -H "Authorization: token $TOKEN" \
  -H "Content-Type: application/json" \
  -d @- \
  https://api.github.com/repos/databrickslabs/coding-agents-databricks-apps/pulls/67
```

Confirm the PR description has the new section by visiting the PR URL or via `gh pr view 67`.

---

## Self-review of this plan against the spec

**Spec section 1 — Tool signature.** Task 2 Step 2 replaces the signature, dropping `branch`. Task 1 Step 5 adds a signature regression guard. ✓

**Spec section 2 — Body of `coda_interactive`.** Task 2 Step 2 contains the full new body. `repos.list`/`repos.update` removed, `workspace.get_status` + `_is_directory` added. ✓

**Spec section 3 — Return shape.** Task 2 Step 2 omits the `"branch"` key. The existing happy-path test does not assert on `"branch"`, so no test change needed; the regression is the signature test. ✓

**Spec section 4 — Caller pre-condition rewrite.** Task 3 rewrites the INTERACTIVE HANDOFF paragraph. Task 2 also rewrites the tool's docstring. Both surfaces updated. ✓

**Spec section 5 — INTERACTIVE HANDOFF string.** Task 3 covers it with a pinned-content test (Step 1) then the rewrite (Step 2). ✓

**Spec "Tests to update."** Task 1 covers every bullet: 3 drops, 4 updates, 2 adds. The pinned-instructions test in Task 3 is a fifth add. ✓

**Spec "Tests for the SDK validation step."** Task 1 Steps 3 and 4 cover the missing-path and not-a-directory cases. ✓

**Spec "Out of scope."** This plan does not add single-file workspace_path, branch-info surfacing in the response, or extra cleanup paths. ✓

**Spec "Acceptance criteria."**
- `coda_interactive` accepts any Workspace directory → Task 2. ✓
- No `branch` parameter → Task 2 + signature guard test. ✓
- Clean error for missing/non-directory paths → Task 2 + 2 new tests. ✓
- Existing tests pass after updates → Task 1 + Task 2 Steps 3-4. ✓
- PR description reflects simpler contract → Task 4 Step 3. ✓

**Placeholder scan:** No TBD/TODO. Every step has explicit code or a concrete command. ✓

**Type consistency:** `_is_directory(status)` accepts an object with `.object_type` attribute — matches what `workspace.get_status` returns and matches the mock helper in tests. The mock helper in Task 1 Step 2 (`_make_dir_status`) returns a MagicMock with `object_type = "DIRECTORY"`, which `_is_directory` accepts via its string-fallback branch (`str(ot) == "DIRECTORY"`). ✓
