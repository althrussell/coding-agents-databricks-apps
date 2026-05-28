"""Tests for /api/session/attach replay fallback."""
import json
import os
from pathlib import Path

import pytest

from coda_mcp import task_manager


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_CONCURRENT_SESSIONS", "5")
    import app as app_module
    # Set app_owner so check_authorization returns (True, None) for requests
    # with no user header (same pattern used by test_session_detach.py)
    app_module.app_owner = "test@example.com"
    with app_module.app.test_client() as c:
        yield c, tmp_path


def _seed_transcript(sessions_root: Path, pty_id: str, content: bytes) -> None:
    sess_id = "sess-test"
    task_id = "task-test"
    sdir = sessions_root / sess_id
    tdir = sdir / "tasks" / task_id
    tdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps({
        "session_id": sess_id,
        "pty_session_id": pty_id,
        "current_task": None,
        "completed_tasks": [task_id],
        "status": "closed",
    }))
    (tdir / "transcript.log").write_bytes(content)


def test_attach_returns_replay_when_pty_gone_and_transcript_exists(client):
    c, root = client
    _seed_transcript(root, "pty-gone", b"hello\r\nworld\r\n")
    resp = c.post("/api/session/attach", json={"session_id": "pty-gone"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["replay"] is True
    assert data["output"] == ["hello\r\nworld\r\n"]
    assert data["label"] == "hermes-mcp (replay)"


def test_attach_404_when_pty_gone_and_no_transcript(client):
    c, root = client
    resp = c.post("/api/session/attach", json={"session_id": "pty-nope"})
    assert resp.status_code == 404
