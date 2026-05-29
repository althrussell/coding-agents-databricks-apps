"""MCP server exposing CoDA session/task tools via FastMCP.

v2: Background execution + inbox pattern.
- ``coda_run`` — fire-and-forget task submission (auto-creates ephemeral session)
- ``coda_inbox`` — dashboard of all background tasks
- ``coda_get_result`` — pull full structured result for a completed task

Delegates all disk state to ``task_manager.py``.  PTY operations are
handled through app hooks (create/send/close) set via ``set_app_hooks()``.

Run standalone for testing::

    python mcp_server.py          # stdio transport
"""

import asyncio
import json
import logging
import os
import shlex
import shutil
import threading
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from mcp.types import ToolAnnotations

from coda_mcp import task_manager
from coda_mcp import url_builder
from coda_mcp.workspace_export import export_workspace_tree, _is_directory

try:
    from databricks.sdk import WorkspaceClient
except ImportError:
    WorkspaceClient = None  # type: ignore

logger = logging.getLogger(__name__)

# ── FastMCP instance ────────────────────────────────────────────────

# Build allowed origins from DATABRICKS_HOST for Genie Code requests
_databricks_host = os.environ.get("DATABRICKS_HOST", "")
_allowed_origins = []
if _databricks_host:
    # Ensure https:// prefix, strip trailing slash
    origin = _databricks_host if _databricks_host.startswith("https://") else f"https://{_databricks_host}"
    _allowed_origins.append(origin.rstrip("/"))

mcp = FastMCP(
    "coda",
    instructions=(
        "CoDA MCP server — delegate coding tasks to AI agents on Databricks.\n\n"
        "CRITICAL — FIRE AND FORGET:\n"
        "coda_run submits work and returns IMMEDIATELY. The task runs autonomously "
        "in the background. After calling coda_run, DO NOT call coda_inbox or "
        "coda_get_result to check on it. Do NOT loop, poll, or wait. Simply tell "
        "the user the task was submitted and MOVE ON to their next request.\n\n"
        "WHEN TO CHECK INBOX:\n"
        "Call coda_inbox ONLY when the user explicitly asks about background tasks "
        "(e.g. 'how's my task going?', 'check on that', 'what's in my inbox'). "
        "Never call it proactively, automatically, or in a loop.\n\n"
        "WORKFLOW:\n"
        "1) coda_run — submit work, get back task_id. Tell user it's running. Stop.\n"
        "2) Continue chatting about other topics — the task runs independently.\n"
        "3) coda_inbox — ONLY when user asks. Shows all tasks from last 24h.\n"
        "4) coda_get_result — for completed tasks, get full structured output.\n\n"
        "CHAINING: pass previous_session_id from a completed task's session_id "
        "to give the new task context of what was done before.\n\n"
        "INFO_NEEDED HANDOFF: When coda_inbox shows a task with status='info_needed', "
        "the agent could not proceed because of missing context. Call coda_get_result "
        "to read the 'feedback' field — it tells you exactly what the agent needs (a "
        "table name, a decision, a clarification). Add that context to the prompt and "
        "resubmit via coda_run with previous_session_id set to the original task's "
        "session_id so the agent has the prior attempt's context. 'needs_approval' is "
        "similar but means the agent has a destructive plan and is waiting for the "
        "caller's explicit go/no-go.\n\n"
        "SHARE THE REPLAY URL: When coda_run returns a viewer_url field (non-null), "
        "mention it to the user in plain text (e.g. \"you can view the session replay "
        "at <url>\"). The URL is a read-only static replay showing the prompt, the "
        "agent's work, and the final output. It reflects the task's progress while "
        "running, then the full transcript once it completes — and remains valid "
        "indefinitely after that. It is safe to share: it points to the same "
        "Databricks App the user is already authenticated against. Do this on the "
        "first mention of the task and any time the user asks where the task is or "
        "how to see what it did.\n\n"
        "INTERACTIVE HANDOFF (coda_interactive): When the user wants a human to "
        "drive a coding agent in CoDA — not autonomous execution — call "
        "coda_interactive instead of coda_run. The tool reads files from a "
        "directory that already exists in the Databricks Workspace (a Git "
        "Folder or a plain Workspace folder — either works). If your working "
        "files are not yet in the Workspace, upload them first (workspace.import "
        "via the Databricks SDK, REST, or CLI — any of these) into a folder "
        "the user can read, then pass that folder as workspace_path. The tool "
        "does NOT accept inline file payloads. If the directory is a Git "
        "Folder, ensure the desired branch is checked out and pushed first — "
        "the export is a server-side snapshot. The tool exports the directory "
        "into a Coda-local working directory, launches the chosen agent "
        "(claude default; also hermes, codex, gemini, opencode), and types "
        "the prompt as the first user input. The return shape includes a "
        "viewer_url the user opens to attach — share it immediately in plain "
        "text; it is the only handle to the session, and the user drives it "
        "until they exit. Interactive sessions do NOT appear in coda_inbox, "
        "and coda_get_result returns nothing for them — do not try to poll "
        "or fetch results. Note that git history is NOT available inside the "
        "session (files-only export); if the user needs history context, "
        "include a git log summary in the prompt string."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

# ── App hooks (PTY integration) ─────────────────────────────────────

_app_create_session = None
_app_send_input = None
_app_close_session = None


def set_app_hooks(
    create_session_fn,
    send_input_fn,
    close_session_fn,
):
    """Wire up Flask app callbacks for PTY operations.

    Registers the create/send/close hooks that ``coda_run`` and ``_watch_task``
    use to drive the underlying PTY session.
    """
    global _app_create_session, _app_send_input, _app_close_session
    _app_create_session = create_session_fn
    _app_send_input = send_input_fn
    _app_close_session = close_session_fn


# ── Background watcher ──────────────────────────────────────────────


def _watch_task(session_id: str, task_id: str, timeout_s: int) -> None:
    """Poll for result.json in a daemon thread.

    - Checks every 5 seconds for ``result.json`` in the task directory.
    - If found, calls ``task_manager.complete_task()`` (which auto-closes session).
    - Tracks last activity from ``status.jsonl`` mtime.
    - Timeout: if wall clock exceeds *timeout_s* AND no status update
      in the last 5 minutes, writes a timeout result and completes.
    - On completion, closes the PTY if hooks are wired.
    """
    tdir = task_manager._task_dir(session_id, task_id)
    status_path = os.path.join(tdir, "status.jsonl")
    start = time.time()
    stale_threshold = 300  # 5 minutes

    while True:
        time.sleep(5)

        # Check for result.json (may be at root or in results/ subdir)
        result_path = task_manager._find_result_json(tdir)
        if result_path:
            try:
                task_manager.complete_task(session_id, task_id)
                _close_pty_immediately(session_id)
                logger.info("Watcher: task %s completed (result found)", task_id)
            except Exception:
                logger.exception("Watcher: error completing task %s", task_id)
            return

        # Check timeout
        elapsed = time.time() - start
        if elapsed > timeout_s:
            # Check last activity
            try:
                last_activity = os.path.getmtime(status_path)
            except OSError:
                last_activity = start

            if (time.time() - last_activity) > stale_threshold:
                # Write timeout result and complete
                try:
                    timeout_result_path = os.path.join(tdir, "result.json")
                    task_manager._write_json(timeout_result_path, {
                        "status": "timeout",
                        "summary": "Task timed out",
                        "files_changed": [],
                        "artifacts": [],
                        "errors": [f"Timeout after {timeout_s}s with no activity for 5 min"],
                    })
                    task_manager.complete_task(session_id, task_id)
                    _close_pty_immediately(session_id)
                    logger.warning("Watcher: task %s timed out", task_id)
                except Exception:
                    logger.exception("Watcher: error timing out task %s", task_id)
                return


def _close_pty_immediately(session_id: str) -> None:
    """Close the PTY session associated with this task session immediately.

    Called by ``_watch_task`` as soon as the task transitions to completed
    or failed. Reads ``pty_session_id`` from the task-manager's session.json
    and calls the ``_app_close_session`` hook (i.e. ``mcp_close_pty_session``
    in production).
    """
    if _app_close_session is None:
        return
    try:
        session = task_manager._read_session(session_id)
        pty_session_id = session.get("pty_session_id")
        if pty_session_id:
            _app_close_session(pty_session_id)
    except Exception:
        logger.debug("Could not close PTY for session %s", session_id, exc_info=True)


# ── Tool definitions ────────────────────────────────────────────────


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    ),
)
async def coda_run(
    prompt: str,
    email: str,
    context: str = "{}",
    previous_session_id: str = "",
    permissions: str = "smart",
    timeout_s: int = 3600,
    workflow_protocol: bool = True,
) -> str:
    """Submit a coding task — FIRE AND FORGET.

    Returns IMMEDIATELY with a task_id. The task runs autonomously in the
    background. After receiving the response, tell the user the task was
    submitted and move on. Do NOT follow up with coda_inbox or coda_get_result
    unless the user explicitly asks to check status later.

    ``context`` is a JSON string with Unity Catalog metadata (tables, schemas).
    ``previous_session_id`` chains to a prior task's session for context continuity.
    ``permissions`` can be ``"smart"`` (default, safe) or ``"yolo"`` (auto-approve all).

    ``workflow_protocol`` defaults to True, which injects a Databricks
    orientation block and a 3-phase workflow protocol (PLAN/EXECUTE/SYNTHESIZE
    with critique at each phase) into the agent's prompt. The protocol also
    defines the ``info_needed`` terminal status for clean handoff when the
    agent is blocked. Set False to skip — useful for non-Databricks tasks.

    Returns JSON with ``task_id``, ``session_id``, and ``status: "running"``.
    """
    try:
        # Check concurrency limit
        running = task_manager.count_running_tasks()
        if running >= task_manager.MAX_CONCURRENT_TASKS:
            return json.dumps({
                "status": "error",
                "error": f"Concurrency limit reached ({task_manager.MAX_CONCURRENT_TASKS} "
                         f"tasks running). Try again when a task completes.",
            })

        # Parse context JSON
        try:
            ctx = json.loads(context) if context else None
        except json.JSONDecodeError:
            return json.dumps({
                "status": "error",
                "error": f"Invalid JSON in context parameter: {context!r}",
            })

        # Auto-create ephemeral session
        session_result = task_manager.create_session(email, "", label="hermes-mcp")
        session_id = session_result["session_id"]

        # Create task first (we need task_id to compute transcript_path).
        result = task_manager.create_task(
            session_id=session_id,
            prompt=prompt,
            email=email,
            context=ctx,
            timeout_s=timeout_s,
            permissions=permissions,
            previous_session_id=previous_session_id or None,
            workflow_protocol=workflow_protocol,
        )
        task_id = result["task_id"]

        pty_session_id = None
        if _app_create_session is not None:
            transcript_path = os.path.join(
                task_manager._task_dir(session_id, task_id),
                "transcript.log",
            )
            pty_session_id = _app_create_session(
                label="hermes-mcp",
                transcript_path=transcript_path,
                replay_only=True,   # coda_run URLs are post-hoc review only
            )
            task_manager._update_session_field(
                session_id, "pty_session_id", pty_session_id
            )

        # Send to PTY if hooks are wired
        if _app_send_input is not None and pty_session_id is not None:
            tdir = task_manager._task_dir(session_id, task_id)
            prompt_path = os.path.join(tdir, "prompt.txt")
            cmd = f'hermes -z "{prompt_path}"'
            if permissions == "yolo":
                cmd += " --yolo"
            cmd += "\n"
            _app_send_input(pty_session_id, cmd)

            # Start background watcher
            t = threading.Thread(
                target=_watch_task,
                args=(session_id, task_id, timeout_s),
                daemon=True,
            )
            t.start()

        return json.dumps({
            "task_id": task_id,
            "session_id": session_id,
            "status": "running",
            "viewer_url": url_builder.build_viewer_url(pty_session_id) if pty_session_id else None,
        })

    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


_ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}

# Wait for the agent's TUI to settle by polling the PTY output buffer. Returns
# as soon as the buffer length stays constant for _PROMPT_SEED_STABILITY_S, or
# _PROMPT_SEED_MAX_WAIT_S elapses (whichever first). Replaces a brittle
# hardcoded sleep that didn't adapt to slow agent cold-starts.
_PROMPT_SEED_MAX_WAIT_S = 5.0
_PROMPT_SEED_STABILITY_S = 1.0


async def _wait_for_agent_ready(pty_session_id: str) -> None:
    """Poll the PTY output buffer; return when the buffer stabilizes or max-wait elapses.

    Stability = buffer length unchanged for ``_PROMPT_SEED_STABILITY_S`` seconds,
    after at least one byte has appeared. If the session disappears mid-wait
    (PTY died), return immediately.
    """
    from app import sessions
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROMPT_SEED_MAX_WAIT_S
    last_len = -1
    stable_since: float | None = None
    poll_interval = 0.1

    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        sess = sessions.get(pty_session_id)
        if sess is None:
            return
        current_len = sum(len(chunk) for chunk in sess.get("output_buffer", []))
        if current_len > 0 and current_len == last_len:
            if stable_since is None:
                stable_since = loop.time()
            elif (loop.time() - stable_since) >= _PROMPT_SEED_STABILITY_S:
                return
        else:
            stable_since = None
            last_len = current_len


_AGENT_LAUNCH_CMDS = {
    "claude": "claude",
    "hermes": "hermes chat",
    "codex": "codex",
    "gemini": "gemini",
    "opencode": "opencode",
}


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
            "error": f"Workspace path not found ({workspace_path}): {e}",
        })

    if not _is_directory(status):
        return json.dumps({
            "status": "error",
            "error": f"Workspace path is not a directory: {workspace_path}",
        })

    # Create PTY FIRST so we have its session_id for the project_dir name.
    if _app_create_session is None or _app_send_input is None:
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    ),
)
async def coda_inbox(
    email: str = "",
    status: str = "",
) -> str:
    """Check status of all background tasks — your inbox.

    Call this instead of polling — it returns ALL tasks at once.
    No need to track individual task_ids; the inbox shows everything
    from the last 24 hours: running, completed, and failed tasks.

    By default returns all tasks. Filter by ``status`` to narrow:
    ``"running"`` for in-progress only, ``"completed"`` for finished,
    ``"failed"`` for errors, or ``""`` (default) for everything.

    Each task includes: ``task_id``, ``session_id``, ``status``,
    ``elapsed_s``, ``prompt_summary`` (first 100 chars of what was asked),
    ``previous_session_id`` (if chained from prior work).
    Completed tasks also include ``summary`` (what was done).
    Running tasks also include ``progress`` (latest agent step).

    Returns JSON with ``tasks`` (list sorted most recent first)
    and ``counts`` (e.g. ``{"running": 1, "completed": 2, "failed": 0}``).
    """
    try:
        tasks = task_manager.list_all_tasks(email=email, status_filter=status)
        # Decorate each task with its viewer URL (if available).
        for t in tasks:
            sess = task_manager._read_session_safe(t["session_id"])
            pty = sess.get("pty_session_id") if sess else None
            if pty:
                vu = url_builder.build_viewer_url(pty)
                if vu:
                    t["viewer_url"] = vu

        counts = {
            "running": 0,
            "completed": 0,
            "failed": 0,
            "info_needed": 0,
            "needs_approval": 0,
        }
        for t in tasks:
            s = t.get("status", "")
            if s in counts:
                counts[s] += 1
            elif s == "done":
                counts["completed"] += 1
            elif s == "timeout":
                counts["failed"] += 1

        return json.dumps({"tasks": tasks, "counts": counts})
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    ),
)
async def coda_get_result(
    task_id: str,
    session_id: str,
) -> str:
    """Retrieve the structured result of a completed task.

    Call this AFTER coda_inbox shows a task as "completed", "failed",
    "info_needed", or "needs_approval".

    Returns JSON with ``task_id``, ``session_id``, ``status``, ``summary``
    (what was done or why the agent stopped), ``files_changed`` (list of
    modified files), ``artifacts`` (job IDs, commit hashes, etc.),
    ``errors`` (if any), and — when status is "info_needed" — ``feedback``
    (a precise description of what context the caller must add before
    resubmitting).
    """
    try:
        result = task_manager.get_task_result(task_id, session_id)
        if result is None:
            # No result yet — return current status
            status = task_manager.get_task_status(task_id, session_id)
            return json.dumps({
                "task_id": task_id,
                "session_id": session_id,
                "status": status.get("status", "unknown"),
                "message": "Result not yet available — task is still in progress.",
            })

        result["task_id"] = task_id
        result["session_id"] = session_id
        # Ensure standard fields exist
        result.setdefault("status", "done")
        result.setdefault("summary", "")
        result.setdefault("files_changed", [])
        result.setdefault("artifacts", [])
        result.setdefault("errors", [])
        # Decorate with viewer_url if known
        sess = task_manager._read_session_safe(session_id)
        pty = sess.get("pty_session_id") if sess else None
        if pty:
            vu = url_builder.build_viewer_url(pty)
            if vu:
                result["viewer_url"] = vu
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"status": "error", "task_id": task_id, "error": str(exc)})


# ── Standalone entry point ──────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
