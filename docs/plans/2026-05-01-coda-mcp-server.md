# CoDA MCP Server Implementation Plan

> **⚠️ SUPERSEDED — historical reference only.** This was the v1 implementation plan (5 tools, gunicorn + WSGI bridge). The shipped implementation diverged during iteration: the production design is documented in [`docs/mcp-v2-background-execution.md`](../mcp-v2-background-execution.md) (3 tools — `coda_run`, `coda_inbox`, `coda_get_result` — on uvicorn + native ASGI). Kept in the tree so reviewers can see the design evolution; do not follow this plan as-is.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an MCP server endpoint (`/mcp`) to CoDA so Databricks Genie Code can delegate coding tasks to Hermes Agent via the MCP protocol.

**Architecture:** Python MCP SDK mounted as a stateless HTTP app at `/mcp` alongside the existing Flask app. A new `task_manager.py` module handles session/task state on disk (`~/.coda/sessions/`). The MCP tools call into the existing PTY infrastructure for session creation and input piping. Hermes is always the agent invoked.

**Tech Stack:** Python MCP SDK (`mcp` package, already installed), Flask, existing PTY session infrastructure, Hermes Agent CLI (`hermes -z`)

**Design doc:** `.humantokens/coda-mcp-design.md` (full design with all decisions)

---

### Task 1: Create Task Manager Module

The task manager handles all disk-based state for MCP sessions and tasks. It's a pure Python module with no Flask dependency — just file I/O.

**Files:**
- Create: `task_manager.py`
- Create: `tests/test_task_manager.py`

**Step 1: Write the failing tests**

```python
# tests/test_task_manager.py
import os
import json
import tempfile
import pytest
from unittest.mock import patch

# All tests use a temp dir instead of ~/.coda
@pytest.fixture
def task_mgr(tmp_path):
    with patch("task_manager.SESSIONS_DIR", str(tmp_path / "sessions")):
        import task_manager
        # Force reimport to pick up patched path
        task_manager.SESSIONS_DIR = str(tmp_path / "sessions")
        yield task_manager


def test_create_session(task_mgr):
    result = task_mgr.create_session(email="alice@example.com", user_id="123")
    assert "session_id" in result
    assert result["status"] == "ready"

    # Verify session.json on disk
    session_dir = os.path.join(task_mgr.SESSIONS_DIR, result["session_id"])
    assert os.path.isdir(session_dir)
    with open(os.path.join(session_dir, "session.json")) as f:
        data = json.load(f)
    assert data["created_by"] == "alice@example.com"
    assert data["status"] == "idle"
    assert data["current_task"] is None


def test_create_task(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]

    result = task_mgr.create_task(
        session_id=sid,
        prompt="create a pipeline",
        email="alice@example.com",
        context={"tables": ["sales.transactions"]},
    )
    assert "task_id" in result
    assert result["status"] == "running"

    # Verify task dir and files
    task_dir = os.path.join(task_mgr.SESSIONS_DIR, sid, "tasks", result["task_id"])
    assert os.path.isfile(os.path.join(task_dir, "prompt.txt"))

    # Session should be busy
    with open(os.path.join(task_mgr.SESSIONS_DIR, sid, "session.json")) as f:
        data = json.load(f)
    assert data["status"] == "busy"
    assert data["current_task"] == result["task_id"]


def test_create_task_rejects_when_busy(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]

    task_mgr.create_task(session_id=sid, prompt="task 1", email="alice@example.com")
    with pytest.raises(task_mgr.SessionBusyError):
        task_mgr.create_task(session_id=sid, prompt="task 2", email="alice@example.com")


def test_get_status_running(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]
    task = task_mgr.create_task(session_id=sid, prompt="do work", email="alice@example.com")

    status = task_mgr.get_task_status(task["task_id"], sid)
    assert status["status"] == "running"
    assert "elapsed_s" in status
    assert status.get("progress") is None  # no status.jsonl yet


def test_get_status_with_progress(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]
    task = task_mgr.create_task(session_id=sid, prompt="do work", email="alice@example.com")
    tid = task["task_id"]

    # Simulate agent writing status.jsonl
    status_file = os.path.join(task_mgr.SESSIONS_DIR, sid, "tasks", tid, "status.jsonl")
    with open(status_file, "a") as f:
        f.write(json.dumps({"step": "planning", "message": "Analyzing requirements"}) + "\n")
        f.write(json.dumps({"step": "coding", "message": "Writing pipeline"}) + "\n")

    status = task_mgr.get_task_status(tid, sid)
    assert status["status"] == "running"
    assert status["progress"]["step"] == "coding"


def test_get_result_completed(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]
    task = task_mgr.create_task(session_id=sid, prompt="do work", email="alice@example.com")
    tid = task["task_id"]

    # Simulate agent writing result.json
    result_file = os.path.join(task_mgr.SESSIONS_DIR, sid, "tasks", tid, "result.json")
    with open(result_file, "w") as f:
        json.dump({
            "status": "completed",
            "summary": "Created pipeline",
            "files_changed": ["pipeline.py"],
            "artifacts": {"job_id": "123"},
            "errors": []
        }, f)

    result = task_mgr.get_task_result(tid, sid)
    assert result["status"] == "completed"
    assert result["summary"] == "Created pipeline"


def test_get_result_not_done(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]
    task = task_mgr.create_task(session_id=sid, prompt="do work", email="alice@example.com")

    result = task_mgr.get_task_result(task["task_id"], sid)
    assert result["status"] == "running"
    assert result.get("summary") is None


def test_complete_task(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]
    task = task_mgr.create_task(session_id=sid, prompt="do work", email="alice@example.com")
    tid = task["task_id"]

    # Simulate result.json written by agent
    result_file = os.path.join(task_mgr.SESSIONS_DIR, sid, "tasks", tid, "result.json")
    with open(result_file, "w") as f:
        json.dump({"status": "completed", "summary": "Done", "files_changed": [], "artifacts": {}, "errors": []}, f)

    task_mgr.complete_task(sid, tid)

    # Session should be idle again
    with open(os.path.join(task_mgr.SESSIONS_DIR, sid, "session.json")) as f:
        data = json.load(f)
    assert data["status"] == "idle"
    assert data["current_task"] is None
    assert tid in data["completed_tasks"]


def test_close_session(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]

    result = task_mgr.close_session(sid)
    assert result["status"] == "closed"

    with open(os.path.join(task_mgr.SESSIONS_DIR, sid, "session.json")) as f:
        data = json.load(f)
    assert data["status"] == "closed"


def test_wrap_prompt(task_mgr):
    wrapped = task_mgr.wrap_prompt(
        task_id="task-007",
        session_id="sess-abc",
        email="alice@example.com",
        prompt="create a pipeline",
        context={"tables": ["sales.transactions"]},
        results_dir="/tmp/test"
    )
    assert "---CODA-TASK---" in wrapped
    assert "task-007" in wrapped
    assert "create a pipeline" in wrapped
    assert "sales.transactions" in wrapped
    assert "result.json" in wrapped
    assert "---END-CODA-TASK---" in wrapped
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run pytest tests/test_task_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'task_manager'`

**Step 3: Write the task_manager module**

```python
# task_manager.py
"""Disk-based state manager for MCP sessions and tasks.

Manages the lifecycle of sessions (PTY-backed Hermes instances) and tasks
(units of work within a session). All state is persisted to ~/.coda/sessions/
so the MCP transport can remain stateless.
"""
import json
import os
import time
import uuid

HOME = os.environ.get("HOME", os.path.expanduser("~"))
SESSIONS_DIR = os.path.join(HOME, ".coda", "sessions")


class SessionBusyError(Exception):
    """Raised when a task is submitted to a session that's already running one."""
    pass


class SessionNotFoundError(Exception):
    """Raised when a session_id doesn't exist."""
    pass


def _session_dir(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, session_id)


def _task_dir(session_id: str, task_id: str) -> str:
    return os.path.join(SESSIONS_DIR, session_id, "tasks", task_id)


def _read_session(session_id: str) -> dict:
    path = os.path.join(_session_dir(session_id), "session.json")
    if not os.path.isfile(path):
        raise SessionNotFoundError(f"Session {session_id} not found")
    with open(path) as f:
        return json.load(f)


def _write_session(session_id: str, data: dict):
    path = os.path.join(_session_dir(session_id), "session.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def create_session(email: str, user_id: str = "", label: str = "") -> dict:
    """Create a new session directory and session.json. Returns {session_id, status}."""
    session_id = f"sess-{uuid.uuid4().hex[:12]}"
    session_dir = _session_dir(session_id)
    os.makedirs(os.path.join(session_dir, "tasks"), exist_ok=True)

    session_data = {
        "created_by": email,
        "user_id": user_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "idle",
        "current_task": None,
        "completed_tasks": [],
        "label": label,
    }
    _write_session(session_id, session_data)

    return {"session_id": session_id, "status": "ready"}


def create_task(
    session_id: str,
    prompt: str,
    email: str,
    context: dict = None,
    context_hint: str = None,
    timeout_s: int = 3600,
    permissions: str = "smart",
) -> dict:
    """Create a new task within a session. Returns {task_id, status}.

    Raises SessionBusyError if the session already has a running task.
    """
    session_data = _read_session(session_id)

    if session_data["status"] == "busy":
        raise SessionBusyError(f"Session {session_id} is busy with task {session_data['current_task']}")

    if session_data["status"] == "closed":
        raise SessionNotFoundError(f"Session {session_id} is closed")

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    task_dir = _task_dir(session_id, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # Write prompt file
    results_dir = task_dir
    wrapped = wrap_prompt(
        task_id=task_id,
        session_id=session_id,
        email=email,
        prompt=prompt,
        context=context,
        results_dir=results_dir,
        context_hint=context_hint,
    )
    with open(os.path.join(task_dir, "prompt.txt"), "w") as f:
        f.write(wrapped)

    # Write task metadata
    with open(os.path.join(task_dir, "meta.json"), "w") as f:
        json.dump({
            "task_id": task_id,
            "session_id": session_id,
            "email": email,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "timeout_s": timeout_s,
            "permissions": permissions,
            "context_hint": context_hint,
        }, f, indent=2)

    # Update session state
    session_data["status"] = "busy"
    session_data["current_task"] = task_id
    _write_session(session_id, session_data)

    return {"task_id": task_id, "status": "running"}


def get_task_status(task_id: str, session_id: str) -> dict:
    """Get current status of a task. Reads status.jsonl for progress."""
    task_dir = _task_dir(session_id, task_id)

    # Check if result.json exists (task completed)
    result_path = os.path.join(task_dir, "result.json")
    if os.path.isfile(result_path):
        with open(result_path) as f:
            result = json.load(f)
        return {
            "task_id": task_id,
            "status": result.get("status", "completed"),
            "elapsed_s": _elapsed(task_dir),
        }

    # Check for progress in status.jsonl
    status_path = os.path.join(task_dir, "status.jsonl")
    progress = None
    if os.path.isfile(status_path):
        with open(status_path) as f:
            lines = f.readlines()
        if lines:
            try:
                progress = json.loads(lines[-1].strip())
            except json.JSONDecodeError:
                pass

    return {
        "task_id": task_id,
        "status": "running",
        "elapsed_s": _elapsed(task_dir),
        "progress": progress,
    }


def get_task_result(task_id: str, session_id: str) -> dict:
    """Get the result of a completed task."""
    task_dir = _task_dir(session_id, task_id)
    result_path = os.path.join(task_dir, "result.json")

    if not os.path.isfile(result_path):
        return {
            "task_id": task_id,
            "status": "running",
            "elapsed_s": _elapsed(task_dir),
        }

    with open(result_path) as f:
        result = json.load(f)

    result["task_id"] = task_id
    result["elapsed_s"] = _elapsed(task_dir)
    return result


def complete_task(session_id: str, task_id: str):
    """Mark a task as completed and update session state back to idle."""
    session_data = _read_session(session_id)
    session_data["status"] = "idle"
    session_data["current_task"] = None
    if task_id not in session_data.get("completed_tasks", []):
        session_data.setdefault("completed_tasks", []).append(task_id)
    _write_session(session_id, session_data)


def close_session(session_id: str) -> dict:
    """Mark a session as closed."""
    session_data = _read_session(session_id)
    session_data["status"] = "closed"
    _write_session(session_id, session_data)
    return {"session_id": session_id, "status": "closed"}


def wrap_prompt(
    task_id: str,
    session_id: str,
    email: str,
    prompt: str,
    context: dict = None,
    results_dir: str = "",
    context_hint: str = None,
) -> str:
    """Wrap a user prompt with the CODA-TASK convention."""
    context_block = ""
    if context:
        context_block = json.dumps(context, indent=2)

    hint_line = ""
    if context_hint:
        hint_line = f"context_hint: {context_hint}\n"

    return f"""---CODA-TASK---
task_id: {task_id}
session_id: {session_id}
user: {email}
{hint_line}results_dir: {results_dir}

CONTEXT:
{context_block}

TASK:
{prompt}

INSTRUCTIONS:
1. Append progress to {results_dir}/status.jsonl
   Format: {{"step": "label", "message": "description"}}
2. When done, write {results_dir}/result.json with:
   {{"status", "summary", "files_changed", "artifacts", "errors"}}
3. If you delegate to a sub-agent (Claude, Codex, Gemini), update
   status.jsonl with delegation steps so the caller can track progress.
---END-CODA-TASK---"""


def _elapsed(task_dir: str) -> float:
    """Calculate elapsed seconds since task started."""
    meta_path = os.path.join(task_dir, "meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        started = meta.get("started_at", "")
        if started:
            try:
                started_ts = time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ"))
                return round(time.time() - started_ts, 1)
            except ValueError:
                pass
    # Fallback: use directory creation time
    return round(time.time() - os.path.getctime(task_dir), 1)
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run pytest tests/test_task_manager.py -v`
Expected: All 10 tests PASS

**Step 5: Commit**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
git add task_manager.py tests/test_task_manager.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: add task manager for MCP session/task state"
```

---

### Task 2: Create MCP Server Module

The MCP server registers 5 tools and delegates to `task_manager.py` for state. It also integrates with the existing PTY session infrastructure in `app.py` for creating terminal sessions and piping prompts.

**Files:**
- Create: `mcp_server.py`
- Create: `tests/test_mcp_server.py`

**Step 1: Write the failing tests**

```python
# tests/test_mcp_server.py
import json
import pytest
from unittest.mock import patch, MagicMock


def test_mcp_tool_list():
    """Verify all 5 tools are registered."""
    from mcp_server import mcp
    # The server should have 5 tools registered
    tools = mcp._tool_manager._tools  # internal access for testing
    tool_names = [t.name for t in tools.values()]
    assert "create_session" in tool_names
    assert "run_task" in tool_names
    assert "get_status" in tool_names
    assert "get_result" in tool_names
    assert "close_session" in tool_names
    assert len(tool_names) == 5
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_server'`

**Step 3: Write the MCP server module**

```python
# mcp_server.py
"""MCP server for CoDA — exposes coding agent capabilities to Genie Code.

Registers 5 tools: create_session, run_task, get_status, get_result, close_session.
Uses the Python MCP SDK with stateless HTTP transport as required by Genie Code.
"""
import json
import logging
import os
import threading

from mcp.server.fastmcp import FastMCP

import task_manager

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "coda",
    stateless_http=True,
)

# Reference to app.py's session infrastructure — set by mount_mcp()
_app_create_session = None
_app_send_input = None
_app_close_session = None


def set_app_hooks(create_session_fn, send_input_fn, close_session_fn):
    """Called by app.py to wire MCP tools to the PTY session infrastructure."""
    global _app_create_session, _app_send_input, _app_close_session
    _app_create_session = create_session_fn
    _app_send_input = send_input_fn
    _app_close_session = close_session_fn


@mcp.tool()
def create_session(
    email: str,
    user_id: str = "",
    label: str = "",
) -> str:
    """Create a new coding agent session backed by Hermes Agent.

    Returns a session_id that can be used with run_task to send work.
    Sessions are long-lived — reuse them for follow-up tasks to maintain context.
    """
    # Create task manager state on disk
    result = task_manager.create_session(email=email, user_id=user_id, label=label)
    session_id = result["session_id"]

    # Create the actual PTY session via app.py infrastructure
    if _app_create_session:
        pty_session_id = _app_create_session(label="hermes-mcp")
        # Map our session_id to the PTY session_id
        task_manager._update_session_field(session_id, "pty_session_id", pty_session_id)

    return json.dumps(result)


@mcp.tool()
def run_task(
    session_id: str,
    prompt: str,
    email: str,
    user_id: str = "",
    context: str = "{}",
    context_hint: str = "",
    timeout_s: int = 3600,
    permissions: str = "smart",
) -> str:
    """Send a coding task to Hermes Agent in an existing session.

    The task runs asynchronously — use get_status to poll progress
    and get_result to retrieve the outcome.

    Args:
        session_id: From create_session
        prompt: Natural language task description
        email: User email for audit trail
        context: JSON string with Unity Catalog context (tables, schemas, etc.)
        context_hint: "new_topic" to signal unrelated work in same session
        timeout_s: Max seconds before timeout (default 3600)
        permissions: "smart" (default, safe) or "yolo" (full autonomy)
    """
    try:
        context_dict = json.loads(context) if context else {}
    except json.JSONDecodeError:
        context_dict = {}

    try:
        result = task_manager.create_task(
            session_id=session_id,
            prompt=prompt,
            email=email,
            context=context_dict,
            context_hint=context_hint or None,
            timeout_s=timeout_s,
            permissions=permissions,
        )
    except task_manager.SessionBusyError as e:
        return json.dumps({"error": str(e)})
    except task_manager.SessionNotFoundError as e:
        return json.dumps({"error": str(e)})

    task_id = result["task_id"]

    # Read the wrapped prompt from disk
    task_dir = task_manager._task_dir(session_id, task_id)
    with open(os.path.join(task_dir, "prompt.txt")) as f:
        wrapped_prompt = f.read()

    # Build hermes command
    yolo_flag = " --yolo" if permissions == "yolo" else ""
    hermes_cmd = f'hermes -z "{task_dir}/prompt.txt"{yolo_flag}\n'

    # Pipe to PTY session in background
    if _app_send_input:
        session_data = task_manager._read_session(session_id)
        pty_session_id = session_data.get("pty_session_id")
        if pty_session_id:
            # Send the hermes command to the terminal
            _app_send_input(pty_session_id, hermes_cmd)

            # Start background watcher for task completion
            thread = threading.Thread(
                target=_watch_task,
                args=(session_id, task_id, timeout_s),
                daemon=True,
            )
            thread.start()

    return json.dumps(result)


@mcp.tool()
def get_status(task_id: str, session_id: str) -> str:
    """Check the current status and progress of a running task.

    Returns status (running/completed/failed/timeout), elapsed time,
    and the latest progress update from the agent if available.
    """
    try:
        result = task_manager.get_task_status(task_id, session_id)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_result(task_id: str, session_id: str) -> str:
    """Retrieve the structured result of a completed task.

    Returns summary, files changed, artifacts (job IDs, commit hashes, etc.),
    and any errors. If the task isn't done yet, returns running status.
    """
    try:
        result = task_manager.get_task_result(task_id, session_id)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def close_session(session_id: str) -> str:
    """Close a session and clean up resources.

    The PTY process is terminated and session state is marked as closed.
    """
    try:
        # Close task manager state
        result = task_manager.close_session(session_id)

        # Close the PTY session
        if _app_close_session:
            session_data = task_manager._read_session(session_id)
            pty_session_id = session_data.get("pty_session_id")
            if pty_session_id:
                _app_close_session(pty_session_id)

        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _watch_task(session_id: str, task_id: str, timeout_s: int):
    """Background thread that watches for task completion or timeout."""
    import time

    task_dir = task_manager._task_dir(session_id, task_id)
    result_path = os.path.join(task_dir, "result.json")
    status_path = os.path.join(task_dir, "status.jsonl")
    start = time.time()
    last_activity = start
    stale_threshold = 300  # 5 minutes with no status update = stale

    while True:
        elapsed = time.time() - start

        # Check for result.json (task completed)
        if os.path.isfile(result_path):
            task_manager.complete_task(session_id, task_id)
            logger.info(f"Task {task_id} completed in {elapsed:.0f}s")
            return

        # Check for stale (no activity in 5 min)
        if os.path.isfile(status_path):
            mtime = os.path.getmtime(status_path)
            if mtime > last_activity:
                last_activity = mtime

        # Timeout: wall clock exceeded AND stale
        if elapsed > timeout_s and (time.time() - last_activity) > stale_threshold:
            logger.warning(f"Task {task_id} timed out after {elapsed:.0f}s")
            # Write a timeout result
            with open(result_path, "w") as f:
                json.dump({
                    "status": "timeout",
                    "summary": f"Task timed out after {elapsed:.0f} seconds",
                    "files_changed": [],
                    "artifacts": {},
                    "errors": ["timeout"],
                }, f)
            task_manager.complete_task(session_id, task_id)
            return

        time.sleep(5)  # Poll every 5 seconds
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run pytest tests/test_mcp_server.py -v`
Expected: PASS

**Step 5: Commit**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
git add mcp_server.py tests/test_mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: add MCP server with 5 tools for Genie Code integration"
```

---

### Task 3: Mount MCP Server in Flask App

Wire the MCP server into the existing Flask app. Add CORS support, skip auth for `/mcp` (Databricks proxy handles it), and expose helper functions for PTY integration.

**Files:**
- Modify: `app.py` (add mount + helper functions)
- Modify: `pyproject.toml` (add flask-cors dependency)

**Step 1: Add flask-cors to dependencies**

In `pyproject.toml`, add `"flask-cors>=4.0"` to dependencies list.

**Step 2: Add PTY helper functions to app.py**

Add these functions after the existing `create_session` route (around line 1081), before the `send_input` route:

```python
# ── MCP Integration Helpers ──────────────────────────────────────────────

def mcp_create_pty_session(label: str = "hermes-mcp") -> str:
    """Create a PTY session for MCP use. Returns the PTY session_id."""
    master_fd, slave_fd = pty.openpty()
    shell_env = os.environ.copy()
    shell_env["TERM"] = "xterm-256color"
    shell_env.pop("CLAUDECODE", None)
    shell_env.pop("CLAUDE_CODE_SESSION", None)
    shell_env.pop("DATABRICKS_TOKEN", None)
    shell_env.pop("DATABRICKS_HOST", None)
    shell_env.pop("GEMINI_API_KEY", None)
    if not shell_env.get("HOME") or shell_env["HOME"] == "/":
        shell_env["HOME"] = "/app/python/source_code"
    local_bin = f"{shell_env['HOME']}/.local/bin"
    shell_env["PATH"] = f"{local_bin}:{shell_env.get('PATH', '')}"
    projects_dir = os.path.join(shell_env["HOME"], "projects")
    os.makedirs(projects_dir, exist_ok=True)

    pid = subprocess.Popen(
        ["/bin/bash"],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid,
        env=shell_env,
        cwd=projects_dir
    ).pid
    os.close(slave_fd)

    session_id = str(uuid.uuid4())
    with sessions_lock:
        if len(sessions) >= MAX_CONCURRENT_SESSIONS:
            os.close(master_fd)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            raise RuntimeError(f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached")
        sessions[session_id] = {
            "master_fd": master_fd,
            "pid": pid,
            "output_buffer": deque(maxlen=1000),
            "lock": threading.Lock(),
            "last_poll_time": time.time(),
            "created_at": time.time(),
            "label": label,
        }

    thread = threading.Thread(target=read_pty_output, args=(session_id, master_fd), daemon=True)
    thread.start()
    log_telemetry("agent", label)
    return session_id


def mcp_send_input(session_id: str, data: str):
    """Send input to a PTY session. Used by MCP to pipe hermes commands."""
    sess = _get_session(session_id)
    if not sess:
        return
    with sess["lock"]:
        try:
            os.write(sess["master_fd"], data.encode())
        except OSError:
            pass


def mcp_close_pty_session(session_id: str):
    """Close a PTY session. Used by MCP close_session tool."""
    sess = _get_session(session_id)
    if not sess:
        return
    terminate_session(session_id, sess["pid"], sess["master_fd"])
```

**Step 3: Mount the MCP app and add CORS**

At the end of `app.py`, before the `if __name__ == "__main__"` block (around line 1298), add:

```python
# ── MCP Server Mount ─────────────────────────────────────────────────────
from flask_cors import CORS
from mcp_server import mcp, set_app_hooks

# CORS for Genie Code cross-origin requests
databricks_host = os.environ.get("DATABRICKS_HOST", "")
if databricks_host:
    CORS(app, origins=[ensure_https(databricks_host)], supports_credentials=True)

# Wire MCP tools to PTY infrastructure
set_app_hooks(
    create_session_fn=mcp_create_pty_session,
    send_input_fn=mcp_send_input,
    close_session_fn=mcp_close_pty_session,
)

# Mount MCP as ASGI app at /mcp
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from a]syncio import run as arun

mcp_asgi_app = mcp.streamable_http_app()

# Bridge ASGI MCP app into Flask's WSGI world
# We use a thin WSGI wrapper since Flask is WSGI and MCP SDK produces ASGI
import asyncio
from io import BytesIO

def mcp_wsgi_app(environ, start_response):
    """WSGI-to-ASGI bridge for the MCP endpoint."""
    # Read request body
    content_length = int(environ.get('CONTENT_LENGTH', 0) or 0)
    body = environ['wsgi.input'].read(content_length) if content_length else b''

    async def run_asgi():
        response_started = False
        status_code = None
        response_headers = None
        response_body = BytesIO()

        async def receive():
            return {"type": "http.request", "body": body}

        async def send(message):
            nonlocal response_started, status_code, response_headers
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = [
                    (k.decode() if isinstance(k, bytes) else k,
                     v.decode() if isinstance(v, bytes) else v)
                    for k, v in message.get("headers", [])
                ]
                response_started = True
            elif message["type"] == "http.response.body":
                response_body.write(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": environ["REQUEST_METHOD"],
            "path": environ.get("PATH_INFO", "/"),
            "query_string": environ.get("QUERY_STRING", "").encode(),
            "headers": [
                (k.lower().replace("http_", "").replace("_", "-").encode(),
                 v.encode())
                for k, v in environ.items()
                if k.startswith("HTTP_")
            ] + (
                [(b"content-type", environ["CONTENT_TYPE"].encode())]
                if environ.get("CONTENT_TYPE") else []
            ),
            "server": (environ.get("SERVER_NAME", "localhost"),
                      int(environ.get("SERVER_PORT", 8000))),
        }

        await mcp_asgi_app(scope, receive, send)
        return status_code, response_headers, response_body.getvalue()

    status_code, headers, body_bytes = asyncio.run(run_asgi())
    status_str = f"{status_code} OK"
    start_response(status_str, headers or [])
    return [body_bytes]

app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/mcp": mcp_wsgi_app})
```

**Step 4: Update auth bypass for /mcp path**

In `app.py` line 808, update the auth bypass to include `/mcp`:

```python
# Before:
if request.path in ("/health", "/api/setup-status", ...):
# After:
if request.path in ("/health", "/api/setup-status", "/api/pat-status", "/api/configure-pat", "/api/app-state") or request.path.startswith("/socket.io") or request.path.startswith("/mcp"):
```

Note: `/mcp` auth is handled by the Databricks Apps proxy (same as all other routes), but the Flask `before_request` check would reject because MCP requests from Genie Code may not carry the same headers as browser requests. The Databricks Apps proxy still enforces authentication before the request reaches CoDA.

**Step 5: Run the app locally to verify mount**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run python -c "from app import app; print('MCP mounted at /mcp'); print([rule.rule for rule in app.url_map.iter_rules()])"`
Expected: No import errors, `/mcp` visible in routes

**Step 6: Commit**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
git add app.py pyproject.toml mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: mount MCP server at /mcp with CORS and PTY integration"
```

---

### Task 4: Add _update_session_field to task_manager

The MCP server needs to store the `pty_session_id` mapping. Add the helper and its test.

**Files:**
- Modify: `task_manager.py` (add `_update_session_field`)
- Modify: `tests/test_task_manager.py` (add test)

**Step 1: Add test**

```python
# Append to tests/test_task_manager.py

def test_update_session_field(task_mgr):
    session = task_mgr.create_session(email="alice@example.com", user_id="123")
    sid = session["session_id"]

    task_mgr._update_session_field(sid, "pty_session_id", "pty-abc-123")

    with open(os.path.join(task_mgr.SESSIONS_DIR, sid, "session.json")) as f:
        data = json.load(f)
    assert data["pty_session_id"] == "pty-abc-123"
```

**Step 2: Add the function to task_manager.py**

After the `_write_session` function:

```python
def _update_session_field(session_id: str, key: str, value):
    """Update a single field in session.json."""
    data = _read_session(session_id)
    data[key] = value
    _write_session(session_id, data)
```

**Step 3: Run tests**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run pytest tests/test_task_manager.py -v`
Expected: All 11 tests PASS

**Step 4: Commit**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
git add task_manager.py tests/test_task_manager.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: add _update_session_field helper for PTY mapping"
```

---

### Task 5: Update requirements.txt

Regenerate requirements after adding flask-cors.

**Files:**
- Modify: `pyproject.toml` (already done in Task 3)
- Regenerate: `requirements.txt`

**Step 1: Regenerate requirements**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv pip compile pyproject.toml -o requirements.txt`

**Step 2: Commit**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
git add pyproject.toml requirements.txt
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "chore: add flask-cors dependency"
```

---

### Task 6: Integration Test — End-to-End MCP Flow

Test the full flow: create session → run task → check status → get result → close session.

**Files:**
- Create: `tests/test_mcp_integration.py`

**Step 1: Write the integration test**

```python
# tests/test_mcp_integration.py
"""Integration test for MCP server flow (no real PTY, mocked app hooks)."""
import json
import os
import pytest
from unittest.mock import patch, MagicMock

import task_manager
import mcp_server


@pytest.fixture(autouse=True)
def setup_env(tmp_path):
    """Redirect all state to temp dir and mock PTY hooks."""
    with patch.object(task_manager, "SESSIONS_DIR", str(tmp_path / "sessions")):
        # Mock the app hooks (no real PTY in tests)
        mcp_server.set_app_hooks(
            create_session_fn=lambda label: "pty-mock-123",
            send_input_fn=MagicMock(),
            close_session_fn=MagicMock(),
        )
        yield tmp_path


def test_full_mcp_flow():
    """End-to-end: create → run → status → result → close."""
    # 1. Create session
    result = json.loads(mcp_server.create_session(email="alice@test.com", user_id="u1"))
    assert result["status"] == "ready"
    sid = result["session_id"]

    # 2. Run task
    result = json.loads(mcp_server.run_task(
        session_id=sid,
        prompt="create a sales pipeline",
        email="alice@test.com",
        context='{"tables": ["sales.transactions"]}',
    ))
    assert result["status"] == "running"
    tid = result["task_id"]

    # 3. Check status (running, no progress yet)
    status = json.loads(mcp_server.get_status(task_id=tid, session_id=sid))
    assert status["status"] == "running"
    assert status["progress"] is None

    # 4. Simulate agent writing progress
    task_dir = task_manager._task_dir(sid, tid)
    with open(os.path.join(task_dir, "status.jsonl"), "w") as f:
        f.write(json.dumps({"step": "coding", "message": "Writing pipeline"}) + "\n")

    status = json.loads(mcp_server.get_status(task_id=tid, session_id=sid))
    assert status["progress"]["step"] == "coding"

    # 5. Simulate agent writing result
    with open(os.path.join(task_dir, "result.json"), "w") as f:
        json.dump({
            "status": "completed",
            "summary": "Created sales pipeline with 3 stages",
            "files_changed": ["pipelines/sales.py"],
            "artifacts": {"job_id": "789"},
            "errors": []
        }, f)

    # 6. Get result
    result = json.loads(mcp_server.get_result(task_id=tid, session_id=sid))
    assert result["status"] == "completed"
    assert result["summary"] == "Created sales pipeline with 3 stages"
    assert result["artifacts"]["job_id"] == "789"

    # 7. Complete and close
    task_manager.complete_task(sid, tid)
    result = json.loads(mcp_server.close_session(session_id=sid))
    assert result["status"] == "closed"


def test_busy_session_rejects():
    """Running a second task on a busy session should return error."""
    result = json.loads(mcp_server.create_session(email="bob@test.com"))
    sid = result["session_id"]

    # First task
    json.loads(mcp_server.run_task(session_id=sid, prompt="task 1", email="bob@test.com"))

    # Second task should fail
    result = json.loads(mcp_server.run_task(session_id=sid, prompt="task 2", email="bob@test.com"))
    assert "error" in result
    assert "busy" in result["error"].lower()
```

**Step 2: Run tests**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run pytest tests/test_mcp_integration.py -v`
Expected: All 2 tests PASS

**Step 3: Run all tests together**

Run: `cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp && uv run pytest tests/ -v`
Expected: All tests PASS

**Step 4: Commit**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
git add tests/test_mcp_integration.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "test: add end-to-end MCP integration test"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | Task manager (disk state) | `task_manager.py`, `tests/test_task_manager.py` |
| 2 | MCP server (5 tools) | `mcp_server.py`, `tests/test_mcp_server.py` |
| 3 | Flask mount + CORS + PTY helpers | `app.py`, `pyproject.toml` |
| 4 | Session field helper | `task_manager.py`, `tests/test_task_manager.py` |
| 5 | Dependencies | `pyproject.toml`, `requirements.txt` |
| 6 | Integration test | `tests/test_mcp_integration.py` |

Total: 4 new files, 2 modified files, ~400 lines of production code, ~250 lines of tests.
