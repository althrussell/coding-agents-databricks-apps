"""Tests for the replay_only flag on PTY sessions."""
import pytest

# Reuse the PTY-availability guard pattern from the suite.
import os
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
def test_mcp_create_pty_session_stores_replay_only_flag():
    """Creating a PTY with replay_only=True stores the flag in the session dict."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = mcp_create_pty_session(label="t1", replay_only=True)
    try:
        assert sessions[sid].get("replay_only") is True
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_mcp_create_pty_session_defaults_replay_only_false():
    """Default for replay_only is False (backward compat)."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = mcp_create_pty_session(label="t2")
    try:
        assert sessions[sid].get("replay_only") is False
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_attach_session_replay_only_alive_pty_returns_replay(tmp_path, monkeypatch):
    """A replay_only=True PTY that is still alive serves the transcript, not the live buffer."""
    from app import app as flask_app, mcp_create_pty_session, mcp_close_pty_session, sessions
    from coda_mcp import task_manager

    # Point task_manager at a tmp sessions root so find_task_dir_by_pty_session resolves.
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))

    # Create a fake task dir keyed by the PTY id we'll mint shortly.
    sid = mcp_create_pty_session(label="t-replay-alive", replay_only=True)
    try:
        # Plant a session.json that links task → this pty_session_id, plus a transcript.
        sess_id = "sess-fake"
        task_id = "task-fake"
        sdir = tmp_path / sess_id
        tdir = sdir / "tasks" / task_id
        tdir.mkdir(parents=True)
        (sdir / "session.json").write_text(
            '{"session_id": "%s", "pty_session_id": "%s", "current_task": "%s"}'
            % (sess_id, sid, task_id)
        )
        (tdir / "transcript.log").write_bytes(b"HELLO TRANSCRIPT")

        # Bust the lookup cache so find_task_dir_by_pty_session sees the new files.
        task_manager._pty_lookup_cache.clear()

        client = flask_app.test_client()
        resp = client.post("/api/session/attach", json={"session_id": sid})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["replay"] is True
        assert body["output"] == ["HELLO TRANSCRIPT"]
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_attach_session_replay_only_false_alive_pty_returns_live_buffer():
    """A replay_only=False PTY that is still alive returns the live output_buffer (unchanged behavior)."""
    from app import app as flask_app, mcp_create_pty_session, mcp_close_pty_session

    sid = mcp_create_pty_session(label="t-live", replay_only=False)
    try:
        client = flask_app.test_client()
        resp = client.post("/api/session/attach", json={"session_id": sid})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("replay") in (False, None)  # live path doesn't set replay key
        assert "output" in body
    finally:
        mcp_close_pty_session(sid)
