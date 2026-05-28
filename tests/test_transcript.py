"""Unit tests for the transcript tee in read_pty_output.

These tests exercise the tee logic directly by simulating output dispatch into
a synthesized session dict and a real on-disk transcript file. The full PTY
read loop is not exercised here — see test_mcp_integration.py for E2E.
"""
import os
import stat
import threading
from pathlib import Path

import pytest

# The three tests that hit mcp_create_pty_session call pty.openpty(), which
# fails in headless CI containers without TTY allocators. Mark those tests
# explicitly so existing fixture-based tests (test_tee_*) keep running.
def _pty_is_usable() -> bool:
    if not hasattr(os, "openpty"):
        return False
    try:
        master, slave = os.openpty()
        os.close(master)
        os.close(slave)
        return True
    except OSError:
        return False


_pty_available = _pty_is_usable()
_pty_skip = pytest.mark.skipif(not _pty_available, reason="pty.openpty() not available")


@pytest.fixture
def session_dict(tmp_path):
    """Build a minimally valid sessions[pty_id] entry with a real transcript handle."""
    transcript = tmp_path / "transcript.log"
    fh = open(transcript, "ab", buffering=0)
    os.fchmod(fh.fileno(), 0o600)
    return {
        "transcript_path": str(transcript),
        "transcript_fh": fh,
        "transcript_bytes": 0,
        "lock": threading.Lock(),
    }


def _write_chunk(session, output: bytes, cap: int = 10 * 1024 * 1024) -> None:
    """Mirror the tee logic from read_pty_output for unit testing."""
    from app import _tee_transcript_chunk
    _tee_transcript_chunk(session, output, cap=cap)


def test_tee_writes_bytes_and_flushes(session_dict):
    _write_chunk(session_dict, b"hello world\n")
    assert session_dict["transcript_bytes"] == 12
    assert Path(session_dict["transcript_path"]).read_bytes() == b"hello world\n"


def test_tee_chmod_is_0600(session_dict):
    mode = stat.S_IMODE(os.stat(session_dict["transcript_path"]).st_mode)
    assert mode == 0o600


def test_tee_truncation_at_cap(session_dict):
    cap = 16
    _write_chunk(session_dict, b"AAAAAAAAAA", cap=cap)
    _write_chunk(session_dict, b"BBBBBBBBBBBBBBBBBBBB", cap=cap)
    body = Path(session_dict["transcript_path"]).read_bytes()
    # 10 A's, then 6 B's, then truncation marker.
    assert body.startswith(b"AAAAAAAAAABBBBBB")
    assert b"[transcript truncated at" in body
    # Handle is closed after marker
    assert session_dict["transcript_fh"] is None


def test_tee_no_op_when_fh_is_none(session_dict):
    session_dict["transcript_fh"] = None
    _write_chunk(session_dict, b"should not write")
    assert Path(session_dict["transcript_path"]).read_bytes() == b""


def test_tee_handles_write_error(session_dict, monkeypatch):
    # Close the handle out from under the tee — write() will ValueError.
    session_dict["transcript_fh"].close()
    _write_chunk(session_dict, b"this will fail")
    # Handle replaced with None; no crash.
    assert session_dict["transcript_fh"] is None


@_pty_skip
def test_mcp_create_pty_session_opens_transcript_when_path_given(tmp_path, monkeypatch):
    monkeypatch.setattr("app.MAX_CONCURRENT_SESSIONS", 5)
    transcript = tmp_path / "transcript.log"
    from app import mcp_create_pty_session, sessions, mcp_close_pty_session
    sid = mcp_create_pty_session(label="test", transcript_path=str(transcript))
    try:
        assert transcript.exists()
        mode = stat.S_IMODE(os.stat(transcript).st_mode)
        assert mode == 0o600
        sess = sessions[sid]
        assert sess["transcript_path"] == str(transcript)
        assert sess["transcript_fh"] is not None
        assert sess["transcript_bytes"] == 0
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_mcp_create_pty_session_no_transcript_when_path_none(monkeypatch):
    monkeypatch.setattr("app.MAX_CONCURRENT_SESSIONS", 5)
    from app import mcp_create_pty_session, sessions, mcp_close_pty_session
    sid = mcp_create_pty_session(label="test")
    try:
        sess = sessions[sid]
        assert sess.get("transcript_fh") is None
        assert sess.get("transcript_path") is None
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_terminate_session_closes_transcript_handle(tmp_path, monkeypatch):
    monkeypatch.setattr("app.MAX_CONCURRENT_SESSIONS", 5)
    transcript = tmp_path / "transcript.log"
    from app import mcp_create_pty_session, sessions, mcp_close_pty_session
    sid = mcp_create_pty_session(label="test", transcript_path=str(transcript))
    fh = sessions[sid]["transcript_fh"]
    mcp_close_pty_session(sid)
    assert fh.closed
    # Session removed from dict
    assert sid not in sessions


@_pty_skip
def test_grace_period_pty_does_not_count_toward_max(monkeypatch):
    monkeypatch.setattr("app.MAX_CONCURRENT_SESSIONS", 2)
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions, _mark_grace_for_session

    sid1 = mcp_create_pty_session(label="t1")
    sid2 = mcp_create_pty_session(label="t2")
    try:
        # At cap. A third creation should raise.
        with pytest.raises(RuntimeError, match="Maximum"):
            mcp_create_pty_session(label="t3")
        # Mark one as grace; now we should have headroom.
        _mark_grace_for_session(sid1)
        assert sessions[sid1]["grace"] is True
        sid3 = mcp_create_pty_session(label="t3")
        mcp_close_pty_session(sid3)
    finally:
        for s in [sid1, sid2]:
            try: mcp_close_pty_session(s)
            except Exception: pass


@_pty_skip
def test_bump_session_last_poll_advances_clock(monkeypatch):
    monkeypatch.setattr("app.MAX_CONCURRENT_SESSIONS", 5)
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions, _bump_session_last_poll
    sid = mcp_create_pty_session(label="t")
    try:
        baseline = sessions[sid]["last_poll_time"]
        _bump_session_last_poll(sid, 300)
        assert sessions[sid]["last_poll_time"] >= baseline + 299
    finally:
        mcp_close_pty_session(sid)


def test_mark_grace_on_missing_session_is_noop():
    from app import _mark_grace_for_session
    _mark_grace_for_session("nonexistent-pty-id")  # must not raise


def test_bump_session_last_poll_missing_is_noop():
    from app import _bump_session_last_poll
    _bump_session_last_poll("nonexistent-pty-id", 100)  # must not raise
