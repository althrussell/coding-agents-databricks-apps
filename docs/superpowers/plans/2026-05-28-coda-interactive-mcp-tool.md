# `coda_interactive` MCP Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `coda_interactive` MCP tool that lets an upstream MCP client hand off a coding session to a human via a CoDA viewer URL — the human attaches to a live PTY with the chosen agent (claude default) already loaded with the user's Databricks Workspace Git Folder as CWD and the kickoff prompt typed.

**Architecture:** Mode 2 in the three-mode framework (see `docs/superpowers/specs/2026-05-28-coda-run-replay-only-design.md`). The tool resolves a `workspace_path` to a Databricks Workspace Git Folder, optionally updates it to a specified branch, exports the file tree to `~/.coda/projects/<pty_session_id>/`, creates a PTY with that dir as CWD, launches the agent, and auto-pastes the prompt. The PTY inherits Mode 1's existing 24h-idle lifecycle. Cleanup of the project dir is tied to PTY teardown.

**Tech Stack:** Python 3.11 + FastMCP + Databricks SDK (`databricks-sdk` already in requirements) + Flask + uvicorn + pytest. No new dependencies. All work localized to `app.py`, `coda_mcp/`, and the test suite.

---

## Pre-flight check (do before Task 1)

- [ ] **P1: Verify baseline tests pass.**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: `524 passed, 15 skipped` (or close to it — matches Todo 1's final state).

- [ ] **P2: Confirm worktree is on the `feat/coda-mcp-live-session-url` branch.**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
git branch --show-current
```

Expected: `feat/coda-mcp-live-session-url`

- [ ] **P3: Capture the baseline SHA for downstream code-quality reviews.**

```bash
git rev-parse HEAD
```

Note the SHA — reviewer subagents need it as BASE_SHA.

---

## Task 1: Prerequisite — refactor `mcp_create_pty_session` to use `_build_terminal_shell_env`

Closes a pre-existing security gap. Today, `mcp_create_pty_session`'s inline env strip only removes 5 keys, while the HTTP `create_session` path uses `_build_terminal_shell_env` which also strips `NPM_TOKEN`, `UV_DEFAULT_INDEX`, `UV_INDEX_*_PASSWORD`, `UV_INDEX_*_USERNAME`, and `npm_config_//*` registry credential patterns. The refactor closes the gap for all MCP-created PTYs (current `coda_run` and future `coda_interactive`).

**Important context:** The current session dict in `mcp_create_pty_session` (around `app.py:1488`) does **NOT** store the child shell's env. The test below would silently pass if it relied on `sessions[sid]["env"]` alone (a missing key returns `{}` from `.get()`). To get a TDD red-then-green cycle that means something, **Task 1 explicitly adds an `"env"` key to the session dict AND swaps the env-strip to use `_build_terminal_shell_env`** — both changes happen together so the test fails ONLY because of credential leaks, not because of a missing key.

**Files:**
- Modify: `app.py` (function `mcp_create_pty_session` at line 1420, env-strip block at line 1435, session dict insert at line 1488)
- Create: `tests/test_mcp_env_strip.py`

- [ ] **Step 1: Write the failing test.**

Create `tests/test_mcp_env_strip.py`:

```python
"""Tests for env-stripping consistency between MCP and HTTP PTY creation paths."""
import os
import pytest

try:
    import pty as _pty
    _master, _slave = _pty.openpty()
    os.close(_master)
    os.close(_slave)
    _PTY_AVAILABLE = True
except Exception:
    _PTY_AVAILABLE = False

_pty_skip = pytest.mark.skipif(
    not _PTY_AVAILABLE,
    reason="PTY not allocatable in this environment",
)


@_pty_skip
def test_mcp_create_pty_session_strips_registry_credentials(monkeypatch):
    """mcp_create_pty_session must strip NPM_TOKEN, UV_DEFAULT_INDEX, UV_INDEX_*_PASSWORD,
    UV_INDEX_*_USERNAME, and npm_config_//* from the child shell's environment —
    matching the HTTP create_session path. Today, these leak into MCP-created PTYs.
    """
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    # Plant registry-credential env vars before creating the PTY.
    monkeypatch.setenv("NPM_TOKEN", "leak-me-npm")
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://leaked-index.example/")
    monkeypatch.setenv("UV_INDEX_MYREG_PASSWORD", "leak-me-uv-pw")
    monkeypatch.setenv("UV_INDEX_MYREG_USERNAME", "leak-me-uv-user")
    monkeypatch.setenv("npm_config_//registry.example/:_authToken", "leak-me-npm-cfg")

    sid = mcp_create_pty_session(label="t-env-strip")
    try:
        env = sessions[sid].get("env", {})
        assert "NPM_TOKEN" not in env, f"NPM_TOKEN leaked into MCP PTY: keys={list(env)}"
        assert "UV_DEFAULT_INDEX" not in env, "UV_DEFAULT_INDEX leaked"
        assert "UV_INDEX_MYREG_PASSWORD" not in env, "UV_INDEX_*_PASSWORD leaked"
        assert "UV_INDEX_MYREG_USERNAME" not in env, "UV_INDEX_*_USERNAME leaked"
        assert not any(k.startswith("npm_config_//") for k in env), "npm_config_// keys leaked"
    finally:
        mcp_close_pty_session(sid)
```

**Note on the test:** The test reads `sessions[sid]["env"]`. The session dict currently has NO `"env"` key, so without Step 3 changes the test would silently pass (`.get("env", {})` returns `{}` and all `not in {}` assertions trivially pass). Step 3 fixes BOTH (a) adds the `"env"` key, (b) swaps the env-strip to use `_build_terminal_shell_env`. Step 2 verifies failure ONLY after Step 3a (key added) — that gives a meaningful red, then Step 3b (strip refactor) gives the green.

- [ ] **Step 2: Add the `"env"` key to the session dict (this alone makes the test runnable but failing).**

In `app.py`, find the session dict literal inside `mcp_create_pty_session` (around line 1488 — the block that has `"master_fd"`, `"pid"`, `"output_buffer"`, etc.). Add a new key:

```python
sessions[session_id] = {
    ...,
    "replay_only": replay_only,
    "env": env_for_child,        # NEW — exposed for env-strip test
    ...
}
```

`env_for_child` is the variable name used in the env-construction block above. If it's named differently in the actual code, use the actual variable name.

- [ ] **Step 3: Run the test and verify it fails for the RIGHT reason.**

```bash
.venv/bin/python -m pytest tests/test_mcp_env_strip.py -v 2>&1 | tail -10
```

Expected: FAIL — at least one of NPM_TOKEN/UV_*/npm_config_// keys is present in `sessions[sid]["env"]` (because the existing inline env-strip doesn't remove them). If the test PASSES at this point, the `"env"` key didn't get added — go back to Step 2.

- [ ] **Step 4: Refactor `mcp_create_pty_session` env-stripping.**

In `app.py`, find the env-construction block inside `mcp_create_pty_session` (around line 1435). It currently looks like:

```python
env_for_child = os.environ.copy()
for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION", "DATABRICKS_TOKEN", "DATABRICKS_HOST", "GEMINI_API_KEY"):
    env_for_child.pop(k, None)
```

Replace with:

```python
env_for_child = _build_terminal_shell_env(os.environ)
```

`_build_terminal_shell_env` is already defined in `app.py` (around line 210). It returns a dict with ALL the right strips applied (registry creds + the 5 keys above + others).

- [ ] **Step 5: Run the test and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_mcp_env_strip.py -v 2>&1 | tail -10
```

Expected: PASS — registry credentials are now stripped.

- [ ] **Step 6: Run the full suite to confirm no regression.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: 525 passed, 15 skipped (one more pass than baseline).

- [ ] **Step 7: Commit.**

```bash
git add app.py tests/test_mcp_env_strip.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "fix: mcp_create_pty_session strips registry credentials like HTTP path does

Pre-existing gap: the MCP PTY-creation path stripped only 5 env vars
while the HTTP create_session path used _build_terminal_shell_env which
also strips NPM_TOKEN, UV_DEFAULT_INDEX, UV_INDEX_*_PASSWORD,
UV_INDEX_*_USERNAME, and npm_config_// keys. This let deployer-level
registry credentials leak into the agent's child shell visible via env.
Refactor mcp_create_pty_session to use _build_terminal_shell_env."
```

---

## Task 2: Add `cwd` kwarg to `mcp_create_pty_session`

`coda_interactive` needs the spawned bash to start in a specific directory (the exported project dir). Add an optional `cwd: str | None = None` kwarg; default `None` preserves current behavior.

**Files:**
- Modify: `app.py` (`mcp_create_pty_session` signature and PTY spawn call)
- Modify: `tests/test_mcp_env_strip.py` (add new test in this same file for compactness)

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_mcp_env_strip.py`:

```python
@_pty_skip
def test_mcp_create_pty_session_respects_cwd_kwarg(tmp_path):
    """When cwd is passed, the spawned bash starts in that directory."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    # Create a sentinel file in tmp_path so we can detect the CWD via shell output.
    sentinel = tmp_path / "SENTINEL_FILE"
    sentinel.write_text("hello")

    sid = mcp_create_pty_session(label="t-cwd", cwd=str(tmp_path))
    try:
        # The session dict should record the cwd.
        assert sessions[sid].get("cwd") == str(tmp_path)
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_mcp_create_pty_session_cwd_defaults_to_none():
    """When cwd is not passed, sessions[sid]['cwd'] is None (preserves current behavior)."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = mcp_create_pty_session(label="t-no-cwd")
    try:
        assert sessions[sid].get("cwd") is None
    finally:
        mcp_close_pty_session(sid)
```

- [ ] **Step 2: Run and verify failure.**

```bash
.venv/bin/python -m pytest tests/test_mcp_env_strip.py::test_mcp_create_pty_session_respects_cwd_kwarg tests/test_mcp_env_strip.py::test_mcp_create_pty_session_cwd_defaults_to_none -v 2>&1 | tail -10
```

Expected: FAIL — `TypeError: unexpected keyword argument 'cwd'` for the first test.

- [ ] **Step 3: Add the `cwd` kwarg.**

In `app.py`, change the `mcp_create_pty_session` signature to:

```python
def mcp_create_pty_session(
    label: str = "hermes-mcp",
    transcript_path: str | None = None,
    replay_only: bool = False,
    cwd: str | None = None,
) -> str:
```

Inside the function, find the PTY spawn / `subprocess.Popen` call (it's the one that launches bash inside the PTY). It should currently look something like:

```python
process = subprocess.Popen(
    ["/bin/bash", "-l"],
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    env=env_for_child,
    preexec_fn=os.setsid,
    close_fds=True,
)
```

Add `cwd=cwd` (which is None by default, meaning the child uses the parent's CWD — current behavior):

```python
process = subprocess.Popen(
    ["/bin/bash", "-l"],
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    env=env_for_child,
    cwd=cwd,                   # NEW
    preexec_fn=os.setsid,
    close_fds=True,
)
```

Also add `cwd` to the session dict:

```python
sessions[session_id] = {
    ...,
    "cwd": cwd,            # NEW
    ...
}
```

- [ ] **Step 4: Run tests and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_mcp_env_strip.py -v 2>&1 | tail -10
```

Expected: all tests in the file pass.

- [ ] **Step 5: Run the full suite.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: 527 passed, 15 skipped.

- [ ] **Step 6: Commit.**

```bash
git add app.py tests/test_mcp_env_strip.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: mcp_create_pty_session accepts cwd kwarg

Adds optional cwd parameter so callers can spawn the PTY's bash in a
specific directory. Default None preserves current behavior. Required
for coda_interactive (which needs to start agents in the exported
project dir)."
```

---

## Task 3: Create `coda_mcp/workspace_export.py` helper

Encapsulates the Workspace-tree-to-local-dir export logic. Single responsibility: given a Databricks Workspace path and a local destination, copy the file tree.

**Files:**
- Create: `coda_mcp/workspace_export.py`
- Create: `tests/test_workspace_export.py`

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_workspace_export.py`:

```python
"""Tests for coda_mcp.workspace_export.export_workspace_tree."""
import os
from unittest.mock import MagicMock, patch

import pytest

from coda_mcp.workspace_export import export_workspace_tree


def _fake_object(path, object_type):
    """Minimal stand-in for databricks.sdk.service.workspace.ObjectInfo."""
    o = MagicMock()
    o.path = path
    o.object_type = object_type
    return o


def test_export_workspace_tree_creates_dest_dir(tmp_path):
    """Helper creates the destination directory if it doesn't exist."""
    dest = tmp_path / "subdir"
    assert not dest.exists()

    client = MagicMock()
    client.workspace.list.return_value = []
    export_workspace_tree(client, "/Workspace/Users/x/empty", str(dest))

    assert dest.exists() and dest.is_dir()


def test_export_workspace_tree_writes_single_file(tmp_path):
    """A workspace with one file gets that file written to the local dir."""
    client = MagicMock()
    client.workspace.list.return_value = [
        _fake_object("/Workspace/Users/x/proj/main.py", "FILE"),
    ]
    # Export returns an object with .content (base64-encoded bytes)
    import base64
    mock_export = MagicMock()
    mock_export.content = base64.b64encode(b"print('hi')\n").decode("ascii")
    client.workspace.export.return_value = mock_export

    export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))

    main_py = tmp_path / "main.py"
    assert main_py.exists()
    assert main_py.read_text() == "print('hi')\n"


def test_export_workspace_tree_handles_nested_dirs(tmp_path):
    """Nested directory structure is preserved in the destination."""
    client = MagicMock()
    # First list call returns the top-level entries
    # Subsequent recursive calls return the subdir contents
    def list_side_effect(path, **kwargs):
        if path == "/Workspace/Users/x/proj":
            return [
                _fake_object("/Workspace/Users/x/proj/main.py", "FILE"),
                _fake_object("/Workspace/Users/x/proj/lib", "DIRECTORY"),
            ]
        elif path == "/Workspace/Users/x/proj/lib":
            return [
                _fake_object("/Workspace/Users/x/proj/lib/util.py", "FILE"),
            ]
        return []
    client.workspace.list.side_effect = list_side_effect

    import base64
    def export_side_effect(path, **kwargs):
        mock = MagicMock()
        if path.endswith("main.py"):
            mock.content = base64.b64encode(b"main\n").decode("ascii")
        else:
            mock.content = base64.b64encode(b"util\n").decode("ascii")
        return mock
    client.workspace.export.side_effect = export_side_effect

    export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))

    assert (tmp_path / "main.py").read_text() == "main\n"
    assert (tmp_path / "lib" / "util.py").read_text() == "util\n"


def test_export_workspace_tree_skips_binary_files_gracefully(tmp_path, caplog):
    """Files that fail to export (e.g. binaries) are skipped and logged, not fatal."""
    client = MagicMock()
    client.workspace.list.return_value = [
        _fake_object("/Workspace/Users/x/proj/text.py", "FILE"),
        _fake_object("/Workspace/Users/x/proj/image.png", "FILE"),
    ]

    import base64
    def export_side_effect(path, **kwargs):
        if path.endswith(".png"):
            raise Exception("400 Bad Request: cannot export binary as SOURCE")
        mock = MagicMock()
        mock.content = base64.b64encode(b"hello\n").decode("ascii")
        return mock
    client.workspace.export.side_effect = export_side_effect

    # Should NOT raise; should skip and log.
    export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))

    assert (tmp_path / "text.py").exists()
    assert not (tmp_path / "image.png").exists()


def test_export_workspace_tree_empty_workspace(tmp_path):
    """Empty workspace path produces empty destination dir (no error)."""
    client = MagicMock()
    client.workspace.list.return_value = []

    export_workspace_tree(client, "/Workspace/Users/x/empty", str(tmp_path))

    assert tmp_path.exists()
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run and verify failure.**

```bash
.venv/bin/python -m pytest tests/test_workspace_export.py -v 2>&1 | tail -10
```

Expected: ImportError (`No module named coda_mcp.workspace_export`).

- [ ] **Step 3: Implement the helper.**

Create `coda_mcp/workspace_export.py`:

```python
"""Export a Databricks Workspace tree (Git Folder contents) to a local directory.

Used by ``coda_interactive`` to materialize a Workspace Git Folder onto the
Coda container's disk before launching an agent in that directory.

Only the working tree is exported — Git Folder server-side metadata (the
``.git/`` directory) is not exposed by the Workspace API.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def export_workspace_tree(client: Any, workspace_path: str, dest_dir: str) -> None:
    """Export the Workspace tree rooted at ``workspace_path`` into ``dest_dir``.

    ``client`` is a ``databricks.sdk.WorkspaceClient`` (or compatible mock).
    Recursively lists entries, calls ``workspace.export()`` per file with
    ``ExportFormat.SOURCE``, decodes the base64 content, and writes to the
    local mirror.

    Per-file export errors (e.g. binaries that fail SOURCE export) are logged
    and skipped — they do not abort the export. The agent in the session may
    not have access to those files; the human can decide whether that matters.
    """
    os.makedirs(dest_dir, exist_ok=True)

    try:
        from databricks.sdk.service.workspace import ExportFormat
        export_format = ExportFormat.SOURCE
    except Exception:
        export_format = None  # mocks won't care

    _export_recursive(client, workspace_path, dest_dir, export_format)


def _export_recursive(client, workspace_path: str, dest_dir: str, export_format) -> None:
    """Walk one level of the workspace and export files / recurse into dirs."""
    try:
        entries = list(client.workspace.list(workspace_path))
    except Exception as e:
        logger.warning("workspace.list(%s) failed: %s", workspace_path, e)
        return

    for entry in entries:
        rel_name = os.path.basename(entry.path)
        local_path = os.path.join(dest_dir, rel_name)
        object_type = str(getattr(entry, "object_type", ""))

        if object_type == "DIRECTORY" or object_type.endswith(".DIRECTORY"):
            _export_recursive(client, entry.path, local_path, export_format)
        elif object_type == "FILE" or object_type.endswith(".FILE") or object_type == "NOTEBOOK" or object_type.endswith(".NOTEBOOK"):
            try:
                if export_format is not None:
                    exported = client.workspace.export(path=entry.path, format=export_format)
                else:
                    exported = client.workspace.export(path=entry.path)
                content_b64 = getattr(exported, "content", "") or ""
                content_bytes = base64.b64decode(content_b64) if content_b64 else b""
                with open(local_path, "wb") as f:
                    f.write(content_bytes)
            except Exception as e:
                logger.warning("workspace.export(%s) failed; skipping: %s", entry.path, e)
                continue
        else:
            # Unknown object type; skip with a log line.
            logger.info("Skipping unknown object_type=%r at %s", object_type, entry.path)
```

- [ ] **Step 4: Run tests and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_workspace_export.py -v 2>&1 | tail -15
```

Expected: 5 passed.

- [ ] **Step 5: Commit.**

```bash
git add coda_mcp/workspace_export.py tests/test_workspace_export.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: add coda_mcp.workspace_export.export_workspace_tree helper

Recursively exports a Databricks Workspace Git Folder's file tree to
a local directory. Used by coda_interactive (next commit) to
materialize project files before launching an agent.

Per-file export errors (binary files etc.) are logged and skipped
rather than aborting the export."
```

---

## Task 4: Extend `mcp_close_pty_session` to clean up the project dir

When a `coda_interactive` PTY is torn down, the corresponding `~/.coda/projects/<pty_session_id>/` directory should be removed. Same cleanup hook fires on graceful exit and idle reaper.

**Files:**
- Modify: `app.py` (function `mcp_close_pty_session` — find its definition by grep)
- Modify: `tests/test_mcp_env_strip.py` (append cleanup-hook test for compactness; could also be a new file)

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_mcp_env_strip.py`:

```python
@_pty_skip
def test_mcp_close_pty_session_removes_project_dir(tmp_path, monkeypatch):
    """When the PTY is closed, any project dir at ~/.coda/projects/<pty_id>/ is removed."""
    import os
    from app import mcp_create_pty_session, mcp_close_pty_session

    # Point HOME at tmp_path so ~/.coda lives in a controllable place.
    monkeypatch.setenv("HOME", str(tmp_path))

    sid = mcp_create_pty_session(label="t-cleanup")

    project_dir = os.path.join(str(tmp_path), ".coda", "projects", sid)
    os.makedirs(project_dir, exist_ok=True)
    sentinel = os.path.join(project_dir, "SENTINEL")
    with open(sentinel, "w") as f:
        f.write("present-before-close")
    assert os.path.exists(sentinel)

    mcp_close_pty_session(sid)

    assert not os.path.exists(project_dir), \
        f"Expected project dir to be removed after PTY close: {project_dir} still exists"


@_pty_skip
def test_mcp_close_pty_session_handles_missing_project_dir(monkeypatch, tmp_path):
    """No project dir present → close still succeeds (no exception)."""
    from app import mcp_create_pty_session, mcp_close_pty_session

    monkeypatch.setenv("HOME", str(tmp_path))

    sid = mcp_create_pty_session(label="t-no-projdir")
    # Do NOT create the project dir — verify close still works.
    mcp_close_pty_session(sid)  # must not raise
```

- [ ] **Step 2: Run and verify failure.**

```bash
.venv/bin/python -m pytest tests/test_mcp_env_strip.py::test_mcp_close_pty_session_removes_project_dir -v 2>&1 | tail -10
```

Expected: FAIL — the sentinel still exists after `mcp_close_pty_session(sid)`.

- [ ] **Step 3: Add the cleanup hook.**

In `app.py`, find `def mcp_close_pty_session(` (search for it). Inside the function, after the existing close logic (closing master_fd, killing process, popping from sessions), add the project-dir cleanup:

```python
def mcp_close_pty_session(session_id: str) -> None:
    # ... existing close logic ...

    # NEW: clean up the project dir if coda_interactive created one.
    import shutil
    project_dir = os.path.join(
        os.path.expanduser("~/.coda/projects"),
        session_id,
    )
    if os.path.isdir(project_dir):
        try:
            shutil.rmtree(project_dir)
        except OSError as e:
            logger.warning("Failed to clean up project dir %s: %s", project_dir, e)
```

Place this near the END of the function so the PTY is fully closed before disk cleanup. The `try/except OSError` is intentional — a stuck file (rare) shouldn't break the close path.

- [ ] **Step 4: Run tests and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_mcp_env_strip.py -v 2>&1 | tail -15
```

Expected: all tests in the file pass.

- [ ] **Step 5: Run the full suite.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: 529 passed, 15 skipped.

- [ ] **Step 6: Commit.**

```bash
git add app.py tests/test_mcp_env_strip.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: mcp_close_pty_session removes project dir on teardown

When coda_interactive creates ~/.coda/projects/<pty_id>/, that directory
should be deleted when the PTY is closed. Single cleanup path ties the
project's disk lifecycle to the PTY's lifecycle — no separate timer or
state to track."
```

---

## Task 5: Stub `coda_interactive` with agent validation

First slice: register the tool, validate the agent kwarg, return error for unknown agents. No SDK calls, no PTY yet.

**Files:**
- Modify: `coda_mcp/mcp_server.py` (add tool definition near `coda_run`)
- Create: `tests/test_coda_interactive.py`

- [ ] **Step 1: Write failing tests.**

Create `tests/test_coda_interactive.py`:

```python
"""Tests for the coda_interactive MCP tool."""
import asyncio
import json
import os

import pytest

ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}


def test_coda_interactive_unknown_agent_returns_error():
    """An agent value not in the allow-list returns status=error and lists allowed values."""
    from coda_mcp import mcp_server

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
        agent="vim",
    ))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert "vim" in result["error"]
    # Error message lists all allowed agents so the calling LLM can correct itself.
    for allowed in ALLOWED_AGENTS:
        assert allowed in result["error"]


def test_coda_interactive_default_agent_is_claude():
    """Calling with no agent kwarg defaults to claude (assertion via signature inspection)."""
    import inspect
    from coda_mcp import mcp_server

    sig = inspect.signature(mcp_server.coda_interactive)
    assert sig.parameters["agent"].default == "claude"
```

- [ ] **Step 2: Run and verify failure.**

```bash
.venv/bin/python -m pytest tests/test_coda_interactive.py -v 2>&1 | tail -10
```

Expected: FAIL — `AttributeError: module 'coda_mcp.mcp_server' has no attribute 'coda_interactive'`.

- [ ] **Step 3: Add the stub tool to `coda_mcp/mcp_server.py`.**

In `coda_mcp/mcp_server.py`, locate the `@mcp.tool(...)` block for `coda_run` (around line 190 in the current file). The `coda_run` function ends around line 289 (before `coda_inbox`). Add the new tool definition between `coda_run` and `coda_inbox`:

```python
_ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}


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
    branch: str = "",
    agent: str = "claude",
    email: str = "",
) -> str:
    """Launch an interactive agent session in CoDA, handed off via a viewer URL.

    The MCP caller passes a Databricks Workspace Git Folder path; Coda exports
    its file tree, launches the chosen agent (claude default) in that directory,
    auto-types ``prompt`` as the first user input, and returns a ``viewer_url``
    the calling user opens in a browser to drive the session.

    Pre-condition: ``workspace_path`` must be a Databricks Workspace Git Folder
    and any in-progress changes must have been committed and pushed to its
    remote before this call. The export reflects the committed HEAD state.

    Interactive sessions do NOT appear in ``coda_inbox`` and ``coda_get_result``
    will not return anything for them. The viewer URL is the only handle.

    Allowed agents: claude (default), hermes, codex, gemini, opencode.
    """
    if agent not in _ALLOWED_AGENTS:
        return json.dumps({
            "status": "error",
            "error": f"Unknown agent: {agent!r}. Allowed: {sorted(_ALLOWED_AGENTS)}",
        })

    # TODO(Task 6+): workspace lookup, branch update, export, PTY launch.
    return json.dumps({
        "status": "error",
        "error": "Not yet implemented (stub).",
    })
```

Notes:
- `json` is already imported at top of file. If not, add `import json`.
- The `# TODO` comment is acceptable here because the function is being built incrementally across Tasks 5–8; each task removes one TODO.

- [ ] **Step 4: Run tests and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_coda_interactive.py -v 2>&1 | tail -10
```

Expected: 2 passed.

- [ ] **Step 5: Commit.**

```bash
git add coda_mcp/mcp_server.py tests/test_coda_interactive.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: stub coda_interactive MCP tool with agent validation

First slice. Validates the agent kwarg against the allow-list
(claude, hermes, codex, gemini, opencode); returns a clear error
listing the allowed values when an unknown agent is passed.
Workspace lookup, branch update, export, and PTY launch come in
follow-up commits."
```

---

## Task 6: Add workspace lookup + branch update to `coda_interactive`

Resolve `workspace_path` to a Git Folder via `WorkspaceClient.repos.list()`; if `branch` is non-empty, call `repos.update(repo_id, branch=branch)`.

**Files:**
- Modify: `coda_mcp/mcp_server.py` (`coda_interactive` body)
- Modify: `tests/test_coda_interactive.py`

- [ ] **Step 1: Write failing tests.**

Append to `tests/test_coda_interactive.py`:

```python
def test_coda_interactive_workspace_path_not_found(monkeypatch):
    """If repos.list() returns no match for workspace_path, status=error."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    fake_client = MagicMock()
    fake_client.repos.list.return_value = []   # no Git Folder at that path

    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/nonexistent",
    ))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert "No Git Folder found" in result["error"]


def test_coda_interactive_branch_update_failure(monkeypatch):
    """If repos.update() raises, return error and don't proceed to PTY."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"

    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]
    fake_client.repos.update.side_effect = Exception("404 branch not found: nonexistent")

    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
        branch="nonexistent",
    ))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert "branch" in result["error"].lower() or "404" in result["error"]


def test_coda_interactive_skips_branch_update_when_empty(monkeypatch):
    """If branch is empty, repos.update() must NOT be called."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"

    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]

    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    # We don't expect a successful return yet (export+PTY not wired); we just
    # verify that repos.update was not called.
    asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
        branch="",
    ))
    fake_client.repos.update.assert_not_called()
```

- [ ] **Step 2: Run and verify failure.**

```bash
.venv/bin/python -m pytest tests/test_coda_interactive.py -v 2>&1 | tail -10
```

Expected: 3 new tests fail (function returns the stub error, not the lookup-based errors expected).

- [ ] **Step 3: Implement workspace lookup + branch update.**

In `coda_mcp/mcp_server.py`, near the top of the file (with other imports), add:

```python
try:
    from databricks.sdk import WorkspaceClient
except ImportError:
    WorkspaceClient = None  # type: ignore
```

(This guards against tests that mock the SDK by monkey-patching `mcp_server.WorkspaceClient`.)

Replace the body of `coda_interactive` (the part after the agent-validation `if` block, currently just the `# TODO` and stub return) with:

```python
    # Resolve the Git Folder by listing under the workspace_path prefix.
    if WorkspaceClient is None:
        return json.dumps({
            "status": "error",
            "error": "databricks-sdk not installed",
        })

    client = WorkspaceClient()

    try:
        repos = list(client.repos.list(path_prefix=workspace_path))
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"Failed to list Git Folders: {e}",
        })

    repo = next((r for r in repos if r.path == workspace_path), None)
    if repo is None:
        return json.dumps({
            "status": "error",
            "error": f"No Git Folder found at {workspace_path}",
        })

    # Optional branch update.
    if branch:
        try:
            client.repos.update(repo_id=repo.id, branch=branch)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": f"Failed to update Git Folder to branch {branch!r}: {e}",
            })

    # TODO(Task 7+): export tree, create PTY, launch agent.
    return json.dumps({
        "status": "error",
        "error": "Not yet implemented (stub).",
    })
```

- [ ] **Step 4: Run tests and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_coda_interactive.py -v 2>&1 | tail -15
```

Expected: 5 passed (2 from Task 5 + 3 new).

- [ ] **Step 5: Commit.**

```bash
git add coda_mcp/mcp_server.py tests/test_coda_interactive.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: coda_interactive resolves Git Folder and optionally updates branch

Uses WorkspaceClient.repos.list to resolve workspace_path to a Git
Folder; returns a clear error if no match. If branch is non-empty,
calls repos.update which performs the actual git fetch+checkout
server-side. Export and PTY launch land in follow-up commits."
```

---

## Task 7: Implement `coda_interactive`'s full happy path

Combined task: export workspace tree, create PTY, cd into project dir, launch agent, seed prompt, return viewer URL. **Single task with a single commit** — avoids the intermediate orphaned-state problem of the previous Task 7→Task 8 split (where the project dir's name didn't match the PTY's session id).

**Ordering insight:** PTY is created FIRST (so we know its session_id), THEN we build `project_dir = ~/.coda/projects/<pty_session_id>/`, THEN export into it, THEN `cd` the PTY into the dir via input, THEN launch the agent, THEN paste the prompt. This single chronology eliminates the chicken-and-egg between project_dir naming and PTY id.

**Files:**
- Modify: `coda_mcp/mcp_server.py` (`coda_interactive` body — replace the stub return from Task 6 with the full happy path; also add module-level imports and constants)
- Modify: `tests/test_coda_interactive.py` (append happy-path test + export-failure test + agent-matrix test)

- [ ] **Step 1: Write failing tests.**

Append to `tests/test_coda_interactive.py`:

```python
def test_coda_interactive_export_failure_cleans_partial_dir(monkeypatch, tmp_path):
    """If export raises mid-way, the partial project dir is removed and the PTY is closed."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    monkeypatch.setenv("HOME", str(tmp_path))

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"
    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]
    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    # PTY-creation hook returns a deterministic id we can predict.
    monkeypatch.setattr(
        mcp_server, "_app_create_session", lambda **kw: "pty-exportfail-id",
    )

    closed = []
    monkeypatch.setattr(
        mcp_server, "_app_close_session", lambda sid: closed.append(sid),
    )

    def fake_export(client, workspace_path, dest_dir):
        # Create the dir + a partial file, then raise.
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "partial.txt"), "w") as f:
            f.write("partial")
        raise RuntimeError("simulated export failure")

    monkeypatch.setattr(mcp_server, "export_workspace_tree", fake_export)

    # send_input hook should NOT be called for export-failure path (we close before launch).
    sent = []
    monkeypatch.setattr(
        mcp_server, "_app_send_input", lambda sid, payload: sent.append((sid, payload)),
    )

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
    ))
    result = json.loads(result_str)

    assert result["status"] == "error"
    assert "export" in result["error"].lower()
    # PTY was created — must be closed on failure.
    assert "pty-exportfail-id" in closed, "PTY must be closed when export fails"
    # Project dir cleaned up.
    project_dir = tmp_path / ".coda" / "projects" / "pty-exportfail-id"
    assert not project_dir.exists(), "Partial project dir must be removed after export failure"


def test_coda_interactive_happy_path_sends_agent_command_and_prompt(monkeypatch, tmp_path):
    """End-to-end mock: export succeeds, PTY created, cd + agent + prompt sent in order."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    monkeypatch.setenv("HOME", str(tmp_path))

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"
    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]
    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    monkeypatch.setattr(
        mcp_server,
        "export_workspace_tree",
        lambda client, ws_path, dest_dir: os.makedirs(dest_dir, exist_ok=True),
    )
    monkeypatch.setattr(
        mcp_server, "_app_create_session", lambda **kw: "pty-happy-id",
    )

    sent_to_pty = []
    monkeypatch.setattr(
        mcp_server,
        "_app_send_input",
        lambda sid, payload: sent_to_pty.append((sid, payload)),
    )

    # Stub the sleep so the test runs fast.
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_DELAY_S", 0)

    monkeypatch.setattr(
        mcp_server.url_builder,
        "build_viewer_url",
        lambda pty_id: f"https://test.example/?session={pty_id}",
    )

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="continue debugging the auth flow",
        workspace_path="/Workspace/Users/x/proj",
        agent="claude",
    ))
    result = json.loads(result_str)

    assert result["status"] == "launched"
    assert result["agent"] == "claude"
    assert result["viewer_url"] == "https://test.example/?session=pty-happy-id"
    assert result["project_dir"].endswith("/pty-happy-id")

    # Three PTY writes, in order: cd, agent command, prompt.
    assert len(sent_to_pty) == 3, f"Expected 3 PTY writes; got {sent_to_pty}"
    assert sent_to_pty[0][0] == "pty-happy-id"
    assert sent_to_pty[0][1].startswith("cd "), \
        f"First write should be cd; got {sent_to_pty[0][1]!r}"
    assert sent_to_pty[1] == ("pty-happy-id", "claude\n")
    assert sent_to_pty[2] == ("pty-happy-id", "continue debugging the auth flow\n")


def test_coda_interactive_agent_command_matrix(monkeypatch, tmp_path):
    """Each allowed agent maps to its expected launch command."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    expected = {
        "claude": "claude\n",
        "hermes": "hermes chat\n",
        "codex": "codex\n",
        "gemini": "gemini\n",
        "opencode": "opencode\n",
    }

    for agent, expected_cmd in expected.items():
        monkeypatch.setenv("HOME", str(tmp_path / agent))

        fake_repo = MagicMock(); fake_repo.id = 1; fake_repo.path = "/W/x/p"
        fake_client = MagicMock()
        fake_client.repos.list.return_value = [fake_repo]
        monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)
        monkeypatch.setattr(
            mcp_server, "export_workspace_tree",
            lambda client, ws_path, dest_dir: os.makedirs(dest_dir, exist_ok=True),
        )
        monkeypatch.setattr(
            mcp_server, "_app_create_session", lambda **kw: f"pty-{agent}",
        )
        sent = []
        monkeypatch.setattr(
            mcp_server, "_app_send_input", lambda sid, p: sent.append(p),
        )
        monkeypatch.setattr(mcp_server, "_PROMPT_SEED_DELAY_S", 0)
        monkeypatch.setattr(
            mcp_server.url_builder, "build_viewer_url",
            lambda pty_id: f"https://test/?s={pty_id}",
        )

        result_str = asyncio.run(mcp_server.coda_interactive(
            prompt="x", workspace_path="/W/x/p", agent=agent,
        ))
        result = json.loads(result_str)
        assert result["status"] == "launched", f"agent {agent}: {result}"

        # sent[0] is cd, sent[1] is the agent command, sent[2] is the prompt.
        assert sent[1] == expected_cmd, \
            f"agent {agent}: expected {expected_cmd!r}, got {sent[1]!r}"
```

- [ ] **Step 2: Run and verify failure.**

```bash
.venv/bin/python -m pytest tests/test_coda_interactive.py -v 2>&1 | tail -15
```

Expected: 3 new tests fail (stub returns "Not yet implemented", happy-path assertions trip).

- [ ] **Step 3: Implement the full happy path.**

In `coda_mcp/mcp_server.py`:

(a) Near the existing imports at the top of the file, add:

```python
import shlex
import time
from coda_mcp import url_builder
from coda_mcp.workspace_export import export_workspace_tree
```

(b) Near other module-level constants, add:

```python
_PROMPT_SEED_DELAY_S = 2  # seconds to wait for agent to initialize before pasting prompt

_AGENT_LAUNCH_CMDS = {
    "claude": "claude",
    "hermes": "hermes chat",
    "codex": "codex",
    "gemini": "gemini",
    "opencode": "opencode",
}
```

(c) Replace the trailing stub `return json.dumps({"status": "error", "error": "Not yet implemented (stub)."})` in `coda_interactive` (the one added by Task 6 after the branch-update block) with the full implementation:

```python
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
            import shutil
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
        time.sleep(_PROMPT_SEED_DELAY_S)
        _app_send_input(pty_session_id, prompt + "\n")

        viewer_url = url_builder.build_viewer_url(pty_session_id)

        return json.dumps({
            "status": "launched",
            "viewer_url": viewer_url,
            "agent": agent,
            "project_dir": project_dir,
            "workspace_path": workspace_path,
            "branch": branch,
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
            import shutil
            shutil.rmtree(project_dir, ignore_errors=True)
        return json.dumps({
            "status": "error",
            "error": f"coda_interactive failed: {e}",
        })
```

Delete the now-unused `# TODO(Task 7+)` comments from Task 6's stub if they remain.

- [ ] **Step 4: Run tests and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_coda_interactive.py -v 2>&1 | tail -15
```

Expected: 8 passed (2 from Task 5 + 3 from Task 6 + 3 from Task 7). If any earlier test breaks because they didn't anticipate `_app_send_input` being called (the export-failure test from Task 6 patches `_app_create_session` but not `_app_send_input`), patch it accordingly with `monkeypatch.setattr(mcp_server, "_app_send_input", lambda *a, **k: None)`.

- [ ] **Step 5: Run the full suite.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: 537+ passed, 15 skipped.

- [ ] **Step 6: Commit.**

```bash
git add coda_mcp/mcp_server.py tests/test_coda_interactive.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: coda_interactive end-to-end happy path

Combined task: creates the PTY first (to get its id), builds the project
dir at ~/.coda/projects/<pty_id>/, exports the Workspace tree into it,
cds the PTY into the dir, launches the chosen agent, waits 2s for
initialization, then pastes the prompt as the first user input.
Returns the viewer URL.

Agent matrix (claude/hermes/codex/gemini/opencode) maps to each
agent's known interactive launch command. Export failure cleanly
closes the PTY and removes the partial project dir."
```

**Acknowledgment**: Task 2's `cwd` kwarg on `mcp_create_pty_session` ends up unused by this implementation (we `cd` via PTY input instead because the project_dir doesn't exist when the PTY is spawned). Leaving the tested optional kwarg in place is acceptable; reverting is more churn for no behavioral gain.

---

## Task 8: Register `coda_interactive` in Flask fallback dispatch

`coda_mcp/mcp_endpoint.py` has a Flask-based MCP fallback used in non-ASGI environments. It needs `coda_interactive` in its dispatch table.

**Files:**
- Modify: `coda_mcp/mcp_endpoint.py` (imports + `_TOOL_DISPATCH`)

- [ ] **Step 1: Read the existing dispatch.**

```bash
grep -n "_TOOL_DISPATCH\|coda_run\|coda_inbox\|coda_get_result" coda_mcp/mcp_endpoint.py
```

Confirm the dispatch is a dict keyed by tool name → function reference.

- [ ] **Step 2: Add the import + dispatch entry.**

In `coda_mcp/mcp_endpoint.py`, find the import block that pulls in the existing tools (around line 22):

```python
from coda_mcp.mcp_server import (
    mcp as mcp_instance,
    coda_run,
    coda_inbox,
    coda_get_result,
)
```

Add `coda_interactive`:

```python
from coda_mcp.mcp_server import (
    mcp as mcp_instance,
    coda_run,
    coda_inbox,
    coda_get_result,
    coda_interactive,
)
```

Find `_TOOL_DISPATCH` (around line 31):

```python
_TOOL_DISPATCH = {
    "coda_run": coda_run,
    "coda_inbox": coda_inbox,
    "coda_get_result": coda_get_result,
}
```

Add `coda_interactive`:

```python
_TOOL_DISPATCH = {
    "coda_run": coda_run,
    "coda_inbox": coda_inbox,
    "coda_get_result": coda_get_result,
    "coda_interactive": coda_interactive,
}
```

- [ ] **Step 3: Run the test suite.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 4: Spot-check the Flask fallback path with a quick test.**

```bash
.venv/bin/python -c "from coda_mcp.mcp_endpoint import _TOOL_DISPATCH; print(list(_TOOL_DISPATCH))"
```

Expected output: `['coda_run', 'coda_inbox', 'coda_get_result', 'coda_interactive']`

- [ ] **Step 5: Commit.**

```bash
git add coda_mcp/mcp_endpoint.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: wire coda_interactive into Flask-fallback MCP dispatch

The Flask blueprint at coda_mcp/mcp_endpoint.py is the WSGI-compatible
fallback used by tests and local dev. Without this entry, those paths
can't call coda_interactive."
```

---

## Task 9: Update FastMCP `instructions` string

The instructions block at `coda_mcp/mcp_server.py:43-70` currently describes only `coda_run` (after Todo 1's update). Add a paragraph for `coda_interactive` so MCP-client LLMs understand the new tool's contract.

**Files:**
- Modify: `coda_mcp/mcp_server.py` (the `instructions` string passed to `FastMCP(...)`)

- [ ] **Step 1: Read the current instructions block.**

```bash
grep -n "SHARE THE REPLAY URL\|FIRE AND FORGET\|WORKFLOW" coda_mcp/mcp_server.py | head -10
```

Open the file and locate the `FastMCP(name=..., instructions="""...""")` block.

- [ ] **Step 2: Add the new paragraph.**

After the existing `SHARE THE REPLAY URL` paragraph and before the `WORKFLOW` paragraph, insert:

```
INTERACTIVE HANDOFF (coda_interactive): When the user wants a human to drive
a coding agent in CoDA — not autonomous execution — call coda_interactive
instead of coda_run. The user must have their project as a Databricks
Workspace Git Folder, and any in-progress changes must be committed and
pushed to the Git Folder's remote BEFORE the call. The tool exports the
committed HEAD state into a Coda-local directory, launches the chosen agent
(claude default; also hermes, codex, gemini, opencode), and types the prompt
as the first user input. Return shape includes a viewer_url the user opens
to attach — they then drive the session until they exit. Interactive sessions
do NOT appear in coda_inbox; coda_get_result returns nothing for them. The
viewer URL is the only handle — pass it to the user immediately. Note that
git history is NOT available inside the session (files-only export); if the
user needs history context, include a git log summary in the prompt string.
```

The exact wording can be tightened to match the existing paragraphs' tone — read the surrounding text first.

- [ ] **Step 3: Run the suite.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: all pass (no tests assert on instruction text strings).

- [ ] **Step 4: Commit.**

```bash
git add coda_mcp/mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "docs: add INTERACTIVE HANDOFF paragraph to MCP instructions

Describes coda_interactive's contract for calling LLMs: Git Folder
pre-condition, viewer URL handoff, no coda_inbox / coda_get_result
integration, git history unavailable trade-off. Prevents calling LLMs
from treating coda_interactive like coda_run (e.g., trying to poll
results)."
```

---

## Task 10: Add regression guard test

Defends the mode separation: calling `coda_run` must NOT create anything under `~/.coda/projects/`. Protects against future drift that accidentally couples the two modes.

**Files:**
- Modify: `tests/test_replay_only_flag.py` (append to keep regression guards together)

- [ ] **Step 1: Append the test.**

Append to `tests/test_replay_only_flag.py`:

```python
@_pty_skip
def test_coda_run_does_not_create_project_dir(tmp_path, monkeypatch):
    """Regression guard: coda_run is Mode 3 (replay-only, no project dir).
    Only coda_interactive (Mode 2) creates dirs under ~/.coda/projects/.

    If a future change accidentally calls export_workspace_tree from
    coda_run or otherwise creates a per-session project dir, this test fires.
    """
    import asyncio
    import json
    from app import sessions, mcp_close_pty_session
    from coda_mcp import mcp_server, task_manager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path / "sessions"))
    # Stop the watcher from racing the test.
    monkeypatch.setattr(mcp_server, "_watch_task", lambda *a, **kw: None)

    result_str = asyncio.run(mcp_server.coda_run(
        prompt="ignored", email="t@example.com",
    ))
    result = json.loads(result_str)
    pty_id = None
    try:
        sess = task_manager._read_session(result["session_id"])
        pty_id = sess.get("pty_session_id")

        # Project dir must NOT exist for coda_run.
        projects_root = os.path.join(str(tmp_path), ".coda", "projects")
        assert not os.path.isdir(projects_root) or not os.listdir(projects_root), (
            f"coda_run unexpectedly created project dirs under {projects_root}: "
            f"{os.listdir(projects_root) if os.path.isdir(projects_root) else 'n/a'}"
        )
    finally:
        if pty_id is not None:
            mcp_close_pty_session(pty_id)
```

- [ ] **Step 2: Run.**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py -v 2>&1 | tail -10
```

Expected: all pass (this test specifically asserts coda_run's NEGATIVE behavior).

- [ ] **Step 3: Run the full suite.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: ~540 passed, 15 skipped (depending on PTY availability — some Task 7 tests skip on this Mac).

- [ ] **Step 4: Commit.**

```bash
git add tests/test_replay_only_flag.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "test: regression guard against coda_run creating project dirs

Mode separation is the spine of the three-mode framework: coda_run is
replay-only (no project_dir, no workspace export), coda_interactive
is the only path that creates ~/.coda/projects/. If a future refactor
accidentally couples them, this test fails loudly."
```

---

## Final verification (post-task)

- [ ] **F1: Full suite green.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q 2>&1 | tail -5
```

Expected: all pass.

- [ ] **F2: No grace/dead references re-introduced.**

```bash
grep -rn "grace\|GRACE_PERIOD\|_mark_grace\|_bump_session_last_poll\|_schedule_deferred_close" coda_mcp/ app.py | grep -v "graceful\|GRACEFUL_"
```

Expected: no matches.

- [ ] **F3: Mode separation still holds.**

```bash
grep -n "_TOOL_DISPATCH" coda_mcp/mcp_endpoint.py
.venv/bin/python -c "from coda_mcp.mcp_endpoint import _TOOL_DISPATCH; print(sorted(_TOOL_DISPATCH))"
```

Expected: `['coda_get_result', 'coda_inbox', 'coda_interactive', 'coda_run']`.

- [ ] **F4: Manual smoke (optional, requires deployed environment + a real Workspace Git Folder).**

1. Restart the app: `uvicorn coda_mcp.mcp_asgi:app`.
2. From an MCP client, call `coda_interactive(prompt="explain this repo", workspace_path="/Workspace/Users/you@db.com/your-git-folder")`.
3. Open the returned `viewer_url`. Confirm: live attach lands you in a session with `claude` running, prompt visible in the chat, CWD is the project dir.
4. Type `/quit` then `exit`. Reattach to the URL — confirm replay or expired-session page.
5. SSH into the container (or check `/health`) — confirm `~/.coda/projects/<pty_id>/` is gone.

---

## Self-review checklist (run on completed plan)

1. **Spec coverage** ✓
   - §1 Tool signature → Task 5 (stub + signature), Task 6 (workspace lookup/branch), Task 7 (full happy path: export+PTY+launch+prompt+viewer_url)
   - §1a Caller pre-condition → Task 9 (MCP instructions string)
   - §2 Agent launch matrix → Task 7 (`_AGENT_LAUNCH_CMDS`)
   - §3 Project source export → Task 3 (`workspace_export.py`) + Task 7 wiring
   - §4 Prompt seeding → Task 7 (`_PROMPT_SEED_DELAY_S` + send_input ordering)
   - §5 PTY lifecycle → Task 4 (cleanup hook)
   - §6 Where this lives + env-strip prereq → Task 1 (env-strip), Task 2 (cwd kwarg), Task 8 (Flask dispatch), Task 9 (instructions)
   - Regression guard → Task 10

2. **Placeholders** ✓ — every step has concrete code/commands. The `# TODO(Task N+)` markers inside intermediate `coda_interactive` versions are explicit hand-offs between tasks, not deferred work.

3. **Type consistency** ✓
   - `_ALLOWED_AGENTS: set[str]` — used identically in Tasks 5 and 7
   - `_AGENT_LAUNCH_CMDS: dict[str, str]` — defined in Task 7
   - `_PROMPT_SEED_DELAY_S: int` — defined in Task 7
   - `pty_session_id: str` — comes from `_app_create_session(...)`'s return; project_dir built from it
   - `workspace_path: str`, `branch: str = ""`, `agent: str = "claude"` consistent across signature, tests, and instructions

4. **Ordering safety** ✓
   - Prereq env-strip (Task 1) runs first — no Todo-2-specific dependency, just security cleanup
   - `cwd` kwarg (Task 2) added before any caller uses it (Task 7, though ultimately unused — see Task 7 acknowledgment)
   - `workspace_export.py` (Task 3) created before `coda_interactive` imports it (Task 7)
   - Cleanup hook (Task 4) added before any project dir gets created (Task 7)
   - `coda_interactive` built incrementally Tasks 5→7 with each task's tests gating progress
   - Flask dispatch (Task 8) and instructions (Task 9) come after the tool itself exists
   - Regression guard (Task 10) verifies the final state

5. **Test discipline** ✓
   - Every code-adding task has a failing test in Step 1, verified failure in Step 2, implementation in Step 3, verified pass in Step 4
   - Tasks 8 (wiring) and 9 (docs) are not TDD but are minimal-risk
   - Final regression guard (Task 10) defends against future drift

---

## Plan critique gate

**Cleared** (2026-05-28). Critic verdict: APPROVE WITH CHANGES. All flagged issues incorporated:

1. **CRITICAL — Task 1 `sessions[sid]["env"]` key didn't exist.** Fixed: Task 1 now has an explicit Step 2 that adds the `"env"` key to the session dict before the env-strip refactor. Step 3 verifies the test fails for the RIGHT reason (credentials present), not silently passes.
2. **MAJOR — Task 7→Task 8 orphaned-state rework.** Fixed: Tasks 7 and 8 merged into a single Task 7 that creates the PTY FIRST, then builds the project_dir keyed by the PTY's session_id, then exports + cds + launches + seeds. Eliminates the intermediate state where the project dir's name didn't match the PTY's actual session id.
3. **MAJOR — Line number drift.** Fixed: `app.py:1402` → `app.py:1420`. `mcp_server.py:218` → "around line 190; insert between `coda_run` (ends near 289) and `coda_inbox`". Other line refs verified accurate.

Original 10 critique questions, all answered in the critique pass:

1. **Task 7 chicken-and-egg** — Resolved by merging Tasks 7+8.
2. **`cwd` kwarg unused** — Acceptable; tested optional kwarg left in place. Documented in Task 7 Acknowledgment.
3. **`WorkspaceClient` monkeypatch target** — Confirmed correct. Task 6 imports it module-level.
4. **`sessions[sid]["env"]` key** — Added explicitly in Task 1 Step 2 (was missing).
5. **`_PROMPT_SEED_DELAY_S` flake risk** — Tests patch to 0. Acceptable.
6. **`_app_create_session is None` null-check** — Consistent with `coda_run`'s pattern.
7. **`os.makedirs(exist_ok=True)`** — UUID collision probability negligible. Acceptable.
8. **Per-task commits** — Matches Todo 1's commit conventions.
9. **Line numbers** — Two references corrected (see MAJOR #3 above).
10. **Test count expectation** — Plausible estimates; exact counts depend on PTY availability.

Plus eight additional critic-eye questions (spec coverage, ordering, TDD discipline, line numbers, test correctness, fragile assumptions, plan gate), all resolved. See the critic's verdict in the conversation history.

Plan is ready for execution.
