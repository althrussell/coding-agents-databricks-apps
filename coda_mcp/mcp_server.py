"""MCP server exposing CoDA session/task tools via FastMCP.

v2: Background execution + inbox pattern.
- ``coda_run`` — fire-and-forget task submission (auto-creates ephemeral session)
- ``coda_inbox`` — dashboard of all background tasks
- ``coda_get_result`` — pull full structured result for a completed task

Delegates all disk state to ``task_manager.py``.  PTY operations are
handled through optional app hooks set via ``set_app_hooks()``.

Run standalone for testing::

    python mcp_server.py          # stdio transport
"""

import json
import logging
import os
import threading
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from mcp.types import ToolAnnotations

from coda_mcp import task_manager
from coda_mcp import url_builder

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
        "SHARE THE LIVE URL: When coda_run returns a viewer_url field (non-null), "
        "mention it to the user in plain text (e.g. \"you can watch progress at "
        "<url>\"). The URL is safe to share — it points to the same Databricks App "
        "the user is already authenticated against. Do this on the first mention "
        "of the task and any time the user asks where the task is or how to see it."
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
_app_mark_grace = None
_app_bump_poll = None

GRACE_PERIOD_S = 300  # 5 minutes


def set_app_hooks(
    create_session_fn,
    send_input_fn,
    close_session_fn,
    mark_grace_fn=None,
    bump_poll_fn=None,
):
    """Wire up Flask app callbacks for PTY operations.

    The two new optional hooks (mark_grace, bump_poll) are used by ``_watch_task``
    to defer PTY close by ``GRACE_PERIOD_S`` after task completion so live viewers
    can keep watching for a few minutes.
    """
    global _app_create_session, _app_send_input, _app_close_session
    global _app_mark_grace, _app_bump_poll
    _app_create_session = create_session_fn
    _app_send_input = send_input_fn
    _app_close_session = close_session_fn
    _app_mark_grace = mark_grace_fn
    _app_bump_poll = bump_poll_fn


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
                _schedule_deferred_close(session_id)
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
                    _schedule_deferred_close(session_id)
                    logger.warning("Watcher: task %s timed out", task_id)
                except Exception:
                    logger.exception("Watcher: error timing out task %s", task_id)
                return


def _close_pty_for_session(session_id: str) -> None:
    """Close the PTY associated with a session, if hooks are wired."""
    if _app_close_session is None:
        return
    try:
        session = task_manager._read_session(session_id)
        pty_session_id = session.get("pty_session_id")
        if pty_session_id:
            _app_close_session(pty_session_id)
    except Exception:
        logger.debug("Could not close PTY for session %s", session_id, exc_info=True)


def _schedule_deferred_close(session_id: str) -> None:
    """Mark the PTY as in-grace and schedule a delayed close.

    Both completion and timeout paths call this in place of the immediate
    ``_close_pty_for_session``. The Timer is a daemon thread so it doesn't
    block uvicorn shutdown.
    """
    if _app_close_session is None:
        return
    try:
        session = task_manager._read_session(session_id)
    except task_manager.SessionNotFoundError:
        return
    pty_session_id = session.get("pty_session_id")
    if not pty_session_id:
        return

    if _app_mark_grace is not None:
        _app_mark_grace(pty_session_id)
    if _app_bump_poll is not None:
        _app_bump_poll(pty_session_id, GRACE_PERIOD_S)

    t = threading.Timer(GRACE_PERIOD_S, _app_close_session, args=(pty_session_id,))
    t.daemon = True
    t.start()
    logger.info(
        "Watcher: scheduled deferred close for pty %s in %ds",
        pty_session_id, GRACE_PERIOD_S,
    )


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
) -> str:
    """Submit a coding task — FIRE AND FORGET.

    Returns IMMEDIATELY with a task_id. The task runs autonomously in the
    background. After receiving the response, tell the user the task was
    submitted and move on. Do NOT follow up with coda_inbox or coda_get_result
    unless the user explicitly asks to check status later.

    ``context`` is a JSON string with Unity Catalog metadata (tables, schemas).
    ``previous_session_id`` chains to a prior task's session for context continuity.
    ``permissions`` can be ``"smart"`` (default, safe) or ``"yolo"`` (auto-approve all).

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

        counts = {"running": 0, "completed": 0, "failed": 0}
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

    Call this AFTER coda_inbox shows a task as "completed" or "failed".

    Returns JSON with ``task_id``, ``session_id``, ``status``, ``summary``
    (what was done), ``files_changed`` (list of modified files),
    ``artifacts`` (job IDs, commit hashes, etc.), and ``errors`` (if any).
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
