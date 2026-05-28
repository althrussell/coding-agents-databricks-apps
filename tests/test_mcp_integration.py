"""End-to-end MCP integration tests — v2 background execution + inbox API.

Exercises the full flow: coda_run -> coda_inbox -> coda_get_result.
No real PTY — app hooks are mocked.
"""

import json
import os
import time
from unittest.mock import MagicMock

import pytest


# ── helpers ──────────────────────────────────────────────────────────


def _parse(result: str) -> dict:
    """Parse JSON string returned by MCP tools."""
    return json.loads(result)


# ── fixture ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_env(tmp_path):
    """Redirect state to tmp and mock PTY hooks."""
    from coda_mcp import task_manager as tm
    from coda_mcp import mcp_server as ms

    original_dir = tm.SESSIONS_DIR
    tm.SESSIONS_DIR = str(tmp_path / "sessions")

    mock_send = MagicMock()
    mock_close = MagicMock()
    ms.set_app_hooks(
        create_session_fn=lambda label, **kwargs: f"pty-mock-{label}",
        send_input_fn=mock_send,
        close_session_fn=mock_close,
    )

    yield {"tmp": tmp_path, "mock_send": mock_send, "mock_close": mock_close}

    tm.SESSIONS_DIR = original_dir
    ms.set_app_hooks(None, None, None)


# ── 1. Happy-path: fire-and-forget → inbox → result ─────────────────


class TestFullMcpFlow:
    @pytest.mark.asyncio
    async def test_full_background_flow(self, isolated_env):
        """Happy path: run (fire-and-forget) → inbox → result."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm

        # Step 1: submit task (returns immediately)
        with MagicMock() as mock_thread:
            from coda_mcp import mcp_server
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("coda_mcp.mcp_server.threading", mock_thread)
                raw = await ms.coda_run(
                    prompt="create a sales pipeline",
                    email="alice@test.com",
                    context='{"tables": ["sales.transactions"]}',
                )

        task = _parse(raw)
        assert task["status"] == "running"
        task_id = task["task_id"]
        session_id = task["session_id"]
        assert task_id.startswith("task-")
        assert session_id.startswith("sess-")

        # Step 2: inbox shows running task
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        assert len(inbox["tasks"]) == 1
        assert inbox["tasks"][0]["task_id"] == task_id
        assert inbox["tasks"][0]["status"] == "running"
        assert inbox["counts"]["running"] == 1

        # Step 3: simulate agent writing result.json
        tdir = tm._task_dir(session_id, task_id)
        result_path = os.path.join(tdir, "result.json")
        with open(result_path, "w") as f:
            json.dump({
                "status": "completed",
                "summary": "Created sales pipeline with 3 stages",
                "files_changed": ["pipeline.py", "config.yaml"],
                "artifacts": ["/workspace/pipeline.py"],
                "errors": [],
            }, f)

        # Step 4: complete_task (simulating what _watch_task does)
        tm.complete_task(session_id, task_id)

        # Step 5: inbox shows completed
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        assert len(inbox["tasks"]) == 1
        assert inbox["tasks"][0]["status"] == "completed"
        assert inbox["tasks"][0]["summary"] == "Created sales pipeline with 3 stages"
        assert inbox["counts"]["completed"] == 1

        # Step 6: get full result
        raw = await ms.coda_get_result(task_id=task_id, session_id=session_id)
        result = _parse(raw)
        assert result["task_id"] == task_id
        assert result["summary"] == "Created sales pipeline with 3 stages"
        assert result["files_changed"] == ["pipeline.py", "config.yaml"]

        # Step 7: session was auto-closed
        session = tm._read_session(session_id)
        assert session["status"] == "closed"


# ── 2. Task chaining with previous_session_id ───────────────────────


class TestTaskChaining:
    @pytest.mark.asyncio
    async def test_chained_task_references_prior_session(self, isolated_env):
        """A chained task includes prior session context in prompt."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm

        # First task
        raw = await ms.coda_run(
            prompt="build pipeline",
            email="bob@test.com",
        )
        first = _parse(raw)
        first_sid = first["session_id"]
        first_tid = first["task_id"]

        # Complete first task
        tdir = tm._task_dir(first_sid, first_tid)
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({
                "status": "completed",
                "summary": "Built pipeline.py",
                "files_changed": ["pipeline.py"],
            }, f)
        tm.complete_task(first_sid, first_tid)

        # Second task chained to first
        raw = await ms.coda_run(
            prompt="add tests for the pipeline",
            email="bob@test.com",
            previous_session_id=first_sid,
        )
        second = _parse(raw)
        second_sid = second["session_id"]
        second_tid = second["task_id"]

        # Verify prompt references prior session
        prompt_path = os.path.join(
            tm._task_dir(second_sid, second_tid), "prompt.txt"
        )
        with open(prompt_path) as f:
            prompt_text = f.read()
        assert f"PRIOR SESSION: {first_sid}" in prompt_text

        # Verify meta.json has previous_session_id
        meta_path = os.path.join(
            tm._task_dir(second_sid, second_tid), "meta.json"
        )
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta["previous_session_id"] == first_sid

        # Verify inbox shows chaining
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        running_tasks = [t for t in inbox["tasks"] if t["status"] == "running"]
        assert len(running_tasks) == 1
        assert running_tasks[0]["previous_session_id"] == first_sid


# ── 3. Concurrency limit ────────────────────────────────────────────


class TestConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_exceeding_limit_returns_error(self, isolated_env):
        """Exceeding MAX_CONCURRENT_TASKS returns a clear error."""
        from coda_mcp import mcp_server as ms
        from unittest.mock import patch

        with patch("coda_mcp.task_manager.MAX_CONCURRENT_TASKS", 1):
            r1 = await ms.coda_run(prompt="task1", email="a@b.com")
            assert _parse(r1)["status"] == "running"

            r2 = await ms.coda_run(prompt="task2", email="a@b.com")
            d2 = _parse(r2)
            assert d2["status"] == "error"
            assert "concurrency" in d2["error"].lower()


# ── 4. Yolo permissions → --yolo flag ───────────────────────────────


class TestYoloPermissions:
    @pytest.mark.asyncio
    async def test_yolo_permissions(self, isolated_env):
        """permissions='yolo' causes the PTY command to include --yolo."""
        from coda_mcp import mcp_server as ms

        mock_send = isolated_env["mock_send"]

        with MagicMock() as mock_thread:
            from coda_mcp import mcp_server
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("coda_mcp.mcp_server.threading", mock_thread)
                await ms.coda_run(
                    prompt="deploy everything",
                    email="dave@test.com",
                    permissions="yolo",
                )

        mock_send.assert_called_once()
        cmd = mock_send.call_args[0][1]
        assert "--yolo" in cmd


# ── 5. Session auto-close on completion ──────────────────────────────


class TestAutoClose:
    @pytest.mark.asyncio
    async def test_session_auto_closes(self, isolated_env):
        """Session is auto-closed when task completes."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm

        raw = await ms.coda_run(prompt="quick job", email="a@b.com")
        d = _parse(raw)

        # Session should be busy
        session = tm._read_session(d["session_id"])
        assert session["status"] == "busy"

        # Complete the task
        tdir = tm._task_dir(d["session_id"], d["task_id"])
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({"status": "completed", "summary": "done"}, f)
        tm.complete_task(d["session_id"], d["task_id"])

        # Session should now be closed
        session = tm._read_session(d["session_id"])
        assert session["status"] == "closed"
        assert "closed_at" in session


# ── 6. Cleanup expired tasks ────────────────────────────────────────


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_expired(self, isolated_env):
        """cleanup_expired_tasks removes old closed sessions."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm
        from unittest.mock import patch

        raw = await ms.coda_run(prompt="old task", email="a@b.com")
        d = _parse(raw)

        # Complete and close
        tdir = tm._task_dir(d["session_id"], d["task_id"])
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({"status": "completed", "summary": "done"}, f)
        tm.complete_task(d["session_id"], d["task_id"])

        # Backdate closed_at to expire it
        session = tm._read_session(d["session_id"])
        session["closed_at"] = time.time() - 90000  # 25 hours ago
        tm._write_json(tm._session_file(d["session_id"]), session)

        # Cleanup should remove it
        removed = tm.cleanup_expired_tasks()
        assert removed == 1

        # Inbox should be empty now
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        assert len(inbox["tasks"]) == 0


# ── 7. E2E: grace period + transcript replay ────────────────────────
# Import the PTY-availability guard from test_transcript.
# The test below requires a real PTY to be usable.


def _pty_is_usable() -> bool:
    import os
    if not hasattr(os, "openpty"):
        return False
    try:
        master, slave = os.openpty()
        os.close(master)
        os.close(slave)
        return True
    except OSError:
        return False


_pty_skip = pytest.mark.skipif(not _pty_is_usable(), reason="pty.openpty() not available")


@_pty_skip
def test_end_to_end_grace_and_replay(tmp_path, monkeypatch):
    """Stub hermes via direct file I/O, then exercise the full coda_run flow.

    Wires up real Flask PTY hooks (not mocks) to verify the complete pipeline:
    create PTY → send input → result.json written → grace period → PTY closed
    → transcript persists → find_task_dir_by_pty_session resolves correctly.
    """
    import asyncio
    import json
    import time
    from pathlib import Path

    from coda_mcp import mcp_server, task_manager, url_builder

    # Override the autouse isolated_env fixture's SESSIONS_DIR patch with our own.
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")
    # Shrink grace period so the test runs fast.
    monkeypatch.setattr(mcp_server, "GRACE_PERIOD_S", 2)

    from app import (
        mcp_create_pty_session,
        mcp_send_input,
        mcp_close_pty_session,
        _mark_grace_for_session,
        _bump_session_last_poll,
        sessions,
    )

    # Disable the watcher thread so the test's manual orchestration
    # (write result.json + call _schedule_deferred_close) is the sole driver.
    # The watcher otherwise races with the manual orchestration on a
    # 5-second poll cycle and produces SessionNotFoundError.
    monkeypatch.setattr(mcp_server, "_watch_task", lambda *a, **kw: None)

    mcp_server.set_app_hooks(
        mcp_create_pty_session,
        mcp_send_input,
        mcp_close_pty_session,
        _mark_grace_for_session,
        _bump_session_last_poll,
    )

    # Initialize cleanup-referenced names BEFORE the try so an early failure
    # (e.g., coda_run or _read_session raising) doesn't shadow the original
    # exception with an UnboundLocalError in the finally block.
    pty_id = None
    sess_id = None
    task_id = None

    try:
        # --- Step 1: Submit a fake task ------------------------------------------
        result_json = asyncio.run(mcp_server.coda_run(
            prompt="test",
            email="u@x",
            timeout_s=5,
        ))
        result = json.loads(result_json)
        assert result["status"] == "running", f"Unexpected status: {result}"
        sess_id = result["session_id"]
        task_id = result["task_id"]
        pty_id = task_manager._read_session(sess_id)["pty_session_id"]

        # --- Step 2: viewer_url contains the pty_id ------------------------------
        assert pty_id in result["viewer_url"]

        # --- Step 3: Simulate hermes writing to the PTY --------------------------
        mcp_send_input(pty_id, "echo HELLO_FROM_HERMES\n")
        time.sleep(0.5)

        # --- Step 4: Simulate hermes completion by writing result.json -----------
        tdir = task_manager._task_dir(sess_id, task_id)
        Path(tdir).joinpath("result.json").write_text(json.dumps({
            "status": "completed",
            "summary": "stub",
            "files_changed": [],
            "artifacts": {},
            "errors": [],
        }))

        # --- Step 5: Trigger deferred close (watcher normally does this) ---------
        # complete_task first (watcher calls this before _schedule_deferred_close)
        task_manager.complete_task(sess_id, task_id)
        mcp_server._schedule_deferred_close(sess_id)

        # --- Step 6: PTY still alive immediately after grace scheduling ----------
        assert pty_id in sessions, "PTY should still be in sessions during grace"
        assert sessions[pty_id]["grace"] is True, "PTY should be marked grace"

        # --- Step 7: Wait past GRACE_PERIOD_S (2 s) + small margin --------------
        time.sleep(2.5)

        # --- Step 8: PTY now gone ------------------------------------------------
        assert pty_id not in sessions, "PTY should have been closed after grace"

        # --- Step 9: Transcript file exists and contains echoed output -----------
        transcript = Path(tdir) / "transcript.log"
        assert transcript.exists(), f"transcript.log missing at {transcript}"
        assert b"HELLO_FROM_HERMES" in transcript.read_bytes(), \
            "Echoed string not found in transcript"

        # --- Step 10: find_task_dir_by_pty_session resolves to the right dir -----
        found = task_manager.find_task_dir_by_pty_session(pty_id)
        assert found == str(tdir), f"Expected {tdir!r}, got {found!r}"

    finally:
        # Re-install mock hooks so the autouse fixture's teardown is consistent.
        from unittest.mock import MagicMock
        mcp_server.set_app_hooks(
            create_session_fn=lambda label, **kwargs: f"pty-mock-{label}",
            send_input_fn=MagicMock(),
            close_session_fn=MagicMock(),
        )
        # Best-effort PTY cleanup if the test failed before the Timer fired.
        if pty_id and pty_id in sessions:
            try:
                mcp_close_pty_session(pty_id)
            except Exception:
                pass
