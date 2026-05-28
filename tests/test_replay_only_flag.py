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
