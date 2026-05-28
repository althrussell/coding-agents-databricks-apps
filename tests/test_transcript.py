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
