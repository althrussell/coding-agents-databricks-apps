# CoDA MCP Live Session URL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `viewer_url` to CoDA MCP tool responses so the calling user can watch hermes execute live in a browser, with a 5-minute grace period after task completion and indefinite static replay from an on-disk PTY transcript.

**Architecture:** Tee PTY bytes to `~/.coda/sessions/{sess}/tasks/{task}/transcript.log` from `read_pty_output`. Replace the immediate post-completion close in `_watch_task` with a `threading.Timer(300, close)`. Mark grace-period PTYs to exempt them from `MAX_CONCURRENT_SESSIONS`. Build `viewer_url` by capturing `X-Forwarded-Host` from inbound requests in an ASGI middleware. The Flask `/api/session/attach` endpoint adds a replay fallback that returns transcript bytes when the live PTY is gone. The SPA reads `?session=<pty_id>` on boot and routes to either the existing `_doAttach` (live) or a new `_doReplay` (static, chunked).

**Tech Stack:** Python 3 (Flask + FastMCP + python-socketio AsyncServer + Starlette + uvicorn), xterm.js, pytest, `uv` for runs.

**Spec:** `docs/superpowers/specs/2026-05-27-coda-mcp-live-session-url-design.md` at commit `02431c8` on `feat/coda-mcp-server`.

---

## Conventions used in this plan

- Worktree: `/Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp/`
- All `git commit` commands use `-c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty"` (per repo convention). No `Co-authored-by` line.
- All pytest invocations use `uv run pytest ...` (per repo convention).
- All file paths are relative to the worktree root.

---

## Task 1: `coda_mcp/url_builder.py` — base URL resolution module

**Files:**
- Create: `coda_mcp/url_builder.py`
- Test: `tests/test_url_builder.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_url_builder.py`:

```python
"""Tests for url_builder module — base URL resolution for viewer_url."""
import os
import importlib
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_module():
    """Re-import url_builder fresh for each test (module-level cache)."""
    from coda_mcp import url_builder
    importlib.reload(url_builder)
    yield


def test_returns_none_when_neither_env_nor_cache():
    from coda_mcp import url_builder
    assert url_builder.build_viewer_url("pty-1") is None


def test_env_override_wins():
    from coda_mcp import url_builder
    with mock.patch.dict(os.environ, {"CODA_APP_URL": "https://override.example.com"}):
        assert url_builder.build_viewer_url("pty-1") == \
            "https://override.example.com/?session=pty-1"


def test_env_override_strips_trailing_slash():
    from coda_mcp import url_builder
    with mock.patch.dict(os.environ, {"CODA_APP_URL": "https://override.example.com/"}):
        assert url_builder.build_viewer_url("pty-1") == \
            "https://override.example.com/?session=pty-1"


def test_header_capture_used_when_no_env():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("app.databricksapps.com")
    assert url_builder.build_viewer_url("pty-1") == \
        "https://app.databricksapps.com/?session=pty-1"


def test_env_overrides_header_capture():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("captured.example.com")
    with mock.patch.dict(os.environ, {"CODA_APP_URL": "https://override.example.com"}):
        assert url_builder.build_viewer_url("pty-1") == \
            "https://override.example.com/?session=pty-1"


def test_header_capture_overwrites_previous():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("first.example.com")
    url_builder.capture_from_headers("second.example.com")
    assert "second.example.com" in url_builder.build_viewer_url("pty-1")


def test_capture_empty_string_does_not_overwrite():
    from coda_mcp import url_builder
    url_builder.capture_from_headers("good.example.com")
    url_builder.capture_from_headers("")
    assert "good.example.com" in url_builder.build_viewer_url("pty-1")


def test_capture_none_does_not_crash():
    from coda_mcp import url_builder
    url_builder.capture_from_headers(None)
    assert url_builder.build_viewer_url("pty-1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_url_builder.py -v`
Expected: ImportError on `from coda_mcp import url_builder` — module does not exist yet.

- [ ] **Step 3: Implement `coda_mcp/url_builder.py`**

Create `coda_mcp/url_builder.py`:

```python
"""Builds the viewer_url returned by CoDA MCP tools.

Resolution order:
1. ``CODA_APP_URL`` env var (explicit override for local dev / power users).
2. Module-level cache populated by ``AppUrlCaptureMiddleware`` from the
   ``X-Forwarded-Host`` header (officially provided by Databricks Apps).
3. ``None`` — caller omits the field entirely.

The cache is process-global (single uvicorn worker per app) and refreshed
on every inbound HTTP request.
"""
from __future__ import annotations

import os
from typing import Optional

_app_url_cache: Optional[str] = None


def capture_from_headers(host: Optional[str]) -> None:
    """Called by the ASGI middleware on every inbound HTTP request.

    No-op when ``host`` is falsy (None or empty) to avoid wiping a good
    cache value with a missing header on a probe/CORS preflight.
    """
    global _app_url_cache
    if host:
        _app_url_cache = host


def build_viewer_url(pty_session_id: str) -> Optional[str]:
    """Return the full viewer URL for a PTY session, or None if no base is known."""
    override = os.environ.get("CODA_APP_URL", "").strip()
    if override:
        base = override.rstrip("/")
    elif _app_url_cache:
        base = f"https://{_app_url_cache}"
    else:
        return None
    return f"{base}/?session={pty_session_id}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_url_builder.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/url_builder.py tests/test_url_builder.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat(coda-mcp): url_builder module for viewer_url resolution"
```

---

## Task 2: `task_manager.find_task_dir_by_pty_session` — reverse lookup with TTL cache

**Files:**
- Modify: `coda_mcp/task_manager.py` (add new function at end, before `cleanup_expired_tasks`)
- Test: `tests/test_task_manager.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_task_manager.py` (locate existing test file; this assumes pytest fixtures `tmp_path` and patching of `SESSIONS_DIR` already exist in the file — confirm pattern, otherwise use the snippet below as a self-contained module):

```python
import json
import os
import time
from unittest import mock

import pytest

from coda_mcp import task_manager


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    # Reset the lookup cache between tests
    task_manager._pty_lookup_cache.clear()
    return tmp_path


def _make_session_dir(root, sess_id, pty_id, current_task=None, completed=None):
    sdir = root / sess_id
    (sdir / "tasks").mkdir(parents=True)
    data = {
        "session_id": sess_id,
        "pty_session_id": pty_id,
        "current_task": current_task,
        "completed_tasks": completed or [],
        "status": "ready",
    }
    (sdir / "session.json").write_text(json.dumps(data))
    return sdir


def test_find_task_dir_hits_current_task(sessions_root):
    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    result = task_manager.find_task_dir_by_pty_session("pty-1")
    assert result == str(sessions_root / "sess-A" / "tasks" / "task-X")


def test_find_task_dir_falls_back_to_last_completed(sessions_root):
    _make_session_dir(
        sessions_root, "sess-A", "pty-1",
        current_task=None,
        completed=["task-old", "task-recent"],
    )
    result = task_manager.find_task_dir_by_pty_session("pty-1")
    assert result == str(sessions_root / "sess-A" / "tasks" / "task-recent")


def test_find_task_dir_returns_none_when_no_match(sessions_root):
    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    assert task_manager.find_task_dir_by_pty_session("pty-NONEXIST") is None


def test_find_task_dir_ignores_corrupt_session_json(sessions_root):
    sdir = sessions_root / "sess-bad"
    sdir.mkdir()
    (sdir / "session.json").write_text("not json {{{")
    _make_session_dir(sessions_root, "sess-good", "pty-1", current_task="task-X")
    assert task_manager.find_task_dir_by_pty_session("pty-1") == \
        str(sessions_root / "sess-good" / "tasks" / "task-X")


def test_find_task_dir_cache_hits_within_ttl(sessions_root):
    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    task_manager.find_task_dir_by_pty_session("pty-1")
    # Remove session.json — cache should still return the hit
    (sessions_root / "sess-A" / "session.json").unlink()
    assert task_manager.find_task_dir_by_pty_session("pty-1") == \
        str(sessions_root / "sess-A" / "tasks" / "task-X")


def test_find_task_dir_cache_expires(sessions_root, monkeypatch):
    monkeypatch.setattr(task_manager, "_PTY_LOOKUP_TTL", 0.01)
    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    task_manager.find_task_dir_by_pty_session("pty-1")
    (sessions_root / "sess-A" / "session.json").unlink()
    time.sleep(0.02)
    assert task_manager.find_task_dir_by_pty_session("pty-1") is None


def test_find_task_dir_no_sessions_dir(sessions_root, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", "/nonexistent/path/that/does/not/exist")
    assert task_manager.find_task_dir_by_pty_session("pty-1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_task_manager.py -v -k find_task_dir`
Expected: 7 failures with `AttributeError: module 'coda_mcp.task_manager' has no attribute 'find_task_dir_by_pty_session'`.

- [ ] **Step 3: Add module-level cache and function**

Edit `coda_mcp/task_manager.py`. Near the top, after the existing module constants (after `TASK_TTL_S = ...`):

```python
# ── PTY → task-dir reverse lookup (used by attach_session replay fallback) ──

_pty_lookup_cache: dict[str, tuple[str, float]] = {}  # pty_id -> (task_dir, ts)
_PTY_LOOKUP_TTL = 60.0  # seconds
```

Then before `def cleanup_expired_tasks()`, add:

```python
def find_task_dir_by_pty_session(pty_session_id: str) -> str | None:
    """Find the task dir whose session.json carries this pty_session_id.

    Returns the path to the active task dir, or — if the session has completed —
    the most recently completed task dir. Returns None on no match.

    Cached for ``_PTY_LOOKUP_TTL`` seconds to avoid disk scans on every browser
    refresh.

    Invariant: CoDA MCP sessions are ephemeral — one task per session. If the
    lifecycle ever changes to allow multiple tasks per session, this function
    must be revisited to pick the active or grace-period task rather than
    ``completed_tasks[-1]``.
    """
    now = time.time()
    cached = _pty_lookup_cache.get(pty_session_id)
    if cached and (now - cached[1]) < _PTY_LOOKUP_TTL:
        return cached[0]

    if not os.path.isdir(SESSIONS_DIR):
        return None

    for sess_name in os.listdir(SESSIONS_DIR):
        sess_file = os.path.join(SESSIONS_DIR, sess_name, "session.json")
        try:
            with open(sess_file) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        if data.get("pty_session_id") != pty_session_id:
            continue

        candidate = data.get("current_task") or (
            data["completed_tasks"][-1] if data.get("completed_tasks") else None
        )
        if candidate:
            tdir = os.path.join(SESSIONS_DIR, sess_name, "tasks", candidate)
            _pty_lookup_cache[pty_session_id] = (tdir, now)
            return tdir

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_task_manager.py -v -k find_task_dir`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/task_manager.py tests/test_task_manager.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat(coda-mcp): find_task_dir_by_pty_session lookup with TTL cache"
```

---

## Task 3: `app.py::read_pty_output` — tee PTY bytes to transcript with lock-guarded writes

**Files:**
- Modify: `app.py` (top: new constant; `read_pty_output` function lines 861-910)
- Test: `tests/test_transcript.py` (new — standalone unit tests for the tee logic; integration tested later)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcript.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transcript.py -v`
Expected: ImportError on `from app import _tee_transcript_chunk`.

- [ ] **Step 3: Add the helper and the constant in `app.py`**

Near the top of `app.py` (after the existing constants block around line 46-50), add:

```python
TRANSCRIPT_CAP_BYTES = 10 * 1024 * 1024  # 10 MB soft cap per transcript
```

Then add the helper (place it near `read_pty_output`, e.g., immediately above it):

```python
def _tee_transcript_chunk(session, output: bytes, cap: int = TRANSCRIPT_CAP_BYTES) -> None:
    """Append PTY output to the transcript file. Single-writer (read_pty_output).

    All file-handle access is under ``session["lock"]`` so we never race the
    Timer-driven close path in ``terminate_session``. The ``ValueError`` catch
    is belt-and-suspenders for the tiny window where the handle is closed
    between the ``is not None`` check and the actual ``write`` call (the lock
    prevents this, but be defensive).
    """
    with session["lock"]:
        fh = session.get("transcript_fh")
        written = session.get("transcript_bytes", 0)
        if fh is None:
            return
        remaining = cap - written
        if remaining <= 0:
            return
        chunk = output[:remaining]
        try:
            fh.write(chunk)
            fh.flush()
            session["transcript_bytes"] = written + len(chunk)
            if len(chunk) < len(output):
                fh.write(b"\n[transcript truncated at %d bytes]\n" % cap)
                fh.flush()
                fh.close()
                session["transcript_fh"] = None
        except (OSError, ValueError) as exc:
            logger.warning("transcript write failed: %s", exc)
            try:
                fh.close()
            except Exception:
                pass
            session["transcript_fh"] = None
```

- [ ] **Step 4: Wire the tee into `read_pty_output`**

In `app.py::read_pty_output`, locate the block (currently around line 880-888):

```python
                decoded = output.decode(errors="replace")
                with session_lock:
                    # Buffer for HTTP polling fallback (AC-15)
                    session["output_buffer"].append(decoded)
                    session["last_poll_time"] = time.time()  # Keep session alive during WS output
                # Push via WebSocket to the session room (AC-8)
                _emit_from_thread('terminal_output',
                                  {'session_id': session_id, 'output': decoded},
                                  room=session_id)
```

Immediately after the `_emit_from_thread` call (and before the `else:` branch), add:

```python
                # Tee to transcript file if enabled for this session
                _tee_transcript_chunk(session, output)
```

- [ ] **Step 5: Run unit tests to verify they pass**

Run: `uv run pytest tests/test_transcript.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run existing terminal tests to verify no regression**

Run: `uv run pytest tests/test_terminal_env_strip.py tests/test_session_linger.py tests/test_session_detach.py -v`
Expected: existing pass count unchanged (no failures introduced).

- [ ] **Step 7: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add app.py tests/test_transcript.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: tee PTY output to transcript.log with lock-guarded writes"
```

---

## Task 4: `app.py` — open transcript handle in `mcp_create_pty_session` + close in `terminate_session`

**Files:**
- Modify: `app.py::mcp_create_pty_session` (lines ~1324-1387)
- Modify: `app.py::terminate_session` (lines ~912-936)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transcript.py -v -k "create_pty or terminate"`
Expected: 3 failures — `mcp_create_pty_session` does not yet accept `transcript_path`.

- [ ] **Step 3: Modify `mcp_create_pty_session` signature**

In `app.py`, change the signature (line ~1324):

```python
def mcp_create_pty_session(label: str = "hermes-mcp", transcript_path: str | None = None) -> str:
```

After the `os.close(slave_fd)` line (around line 1358) and before `session_id = str(uuid.uuid4())`, add the transcript open. Place it inside the existing flow so the file handle is constructed before being stored:

```python
    # Open transcript file (if requested) before locking the session dict.
    transcript_fh = None
    if transcript_path:
        try:
            os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
            transcript_fh = open(transcript_path, "ab", buffering=0)
            os.fchmod(transcript_fh.fileno(), 0o600)
        except OSError as exc:
            logger.warning("Could not open transcript at %s: %s", transcript_path, exc)
            transcript_fh = None
```

Modify the `sessions[session_id] = { ... }` block to include the new fields:

```python
        sessions[session_id] = {
            "master_fd": master_fd,
            "pid": pid,
            "output_buffer": deque(maxlen=1000),
            "lock": threading.Lock(),
            "last_poll_time": time.time(),
            "created_at": time.time(),
            "label": label,
            "transcript_path": transcript_path if transcript_fh else None,
            "transcript_fh": transcript_fh,
            "transcript_bytes": 0,
            "grace": False,
        }
```

- [ ] **Step 4: Modify `terminate_session` to close the transcript handle**

In `app.py::terminate_session` (line ~912), at the top of the function (right after the `logger.info` and the `_emit_from_thread('session_closed', ...)` call), add:

```python
    # Close transcript handle (if any) under per-session lock; swap-then-close
    # outside the lock to avoid blocking on slow filesystems.
    with sessions_lock:
        sess = sessions.get(session_id)
    if sess is not None:
        with sess["lock"]:
            transcript_fh = sess.get("transcript_fh")
            sess["transcript_fh"] = None
        if transcript_fh is not None:
            try:
                transcript_fh.close()
            except Exception:
                pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_transcript.py -v -k "create_pty or terminate"`
Expected: 3 passed.

- [ ] **Step 6: Run full transcript test suite**

Run: `uv run pytest tests/test_transcript.py -v`
Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add app.py tests/test_transcript.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: open transcript handle in mcp_create_pty_session; close in terminate_session"
```

---

## Task 5: `app.py` — grace-period exemption from `MAX_CONCURRENT_SESSIONS` + helper hooks

**Files:**
- Modify: `app.py` (the two `MAX_CONCURRENT_SESSIONS` check sites + add two new helpers near the bottom near other MCP hook functions)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transcript.py -v -k "grace or bump_session"`
Expected: failures — `_mark_grace_for_session` / `_bump_session_last_poll` don't exist; the cap check still uses raw `len`.

- [ ] **Step 3: Replace the `MAX_CONCURRENT_SESSIONS` checks**

There are two checkpoints in `app.py`:

**Site 1 — `create_session()` (around line 1252):**

```python
    with sessions_lock:
        if len(sessions) >= MAX_CONCURRENT_SESSIONS:
            return jsonify({"error": f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Close an existing session first."}), 429
```

Replace with:

```python
    with sessions_lock:
        active = sum(1 for s in sessions.values() if not s.get("grace"))
        if active >= MAX_CONCURRENT_SESSIONS:
            return jsonify({"error": f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Close an existing session first."}), 429
```

**Site 2 — `mcp_create_pty_session()` (around lines 1326-1330 and again 1362-1371):**

Both `len(sessions) >= MAX_CONCURRENT_SESSIONS` checks become:

```python
        active = sum(1 for s in sessions.values() if not s.get("grace"))
        if active >= MAX_CONCURRENT_SESSIONS:
            raise RuntimeError(
                f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached."
            )
```

(Apply at both pre-spawn and post-spawn check sites.)

- [ ] **Step 4: Add the two helper functions**

Place near `mcp_close_pty_session` (around line 1399):

```python
def _mark_grace_for_session(session_id: str) -> None:
    """Mark a PTY session as 'in grace period' so it doesn't count toward
    MAX_CONCURRENT_SESSIONS. Called by ``_watch_task`` immediately before
    scheduling the deferred close Timer.

    No-op if the session does not exist (e.g., already torn down).
    """
    with sessions_lock:
        sess = sessions.get(session_id)
    if sess is None:
        return
    with sess["lock"]:
        sess["grace"] = True


def _bump_session_last_poll(session_id: str, delta_s: float) -> None:
    """Advance ``last_poll_time`` by ``delta_s`` so the idle reaper can't
    preempt the Timer's deferred close. Defensive: at the current 24h
    SESSION_TIMEOUT_SECONDS the reaper would never win anyway, but a future
    tuning shouldn't break the grace window.

    No-op if the session does not exist.
    """
    with sessions_lock:
        sess = sessions.get(session_id)
    if sess is None:
        return
    with sess["lock"]:
        sess["last_poll_time"] = time.time() + delta_s
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_transcript.py -v -k "grace or bump_session"`
Expected: 4 passed.

- [ ] **Step 6: Run full transcript suite + session limit test for regression**

Run: `uv run pytest tests/test_transcript.py tests/test_session_limit.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add app.py tests/test_transcript.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: exempt grace-period PTYs from MAX_CONCURRENT_SESSIONS"
```

---

## Task 6: `mcp_server.py` — wire deferred close via `Timer`; update `set_app_hooks`

**Files:**
- Modify: `coda_mcp/mcp_server.py` (lines 70-90 hook plumbing; lines 94-148 `_watch_task` + helpers)
- Test: `tests/test_mcp_server.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
import threading
from unittest import mock

from coda_mcp import mcp_server, task_manager


def test_set_app_hooks_accepts_grace_and_bump_hooks():
    create = mock.MagicMock()
    send = mock.MagicMock()
    close = mock.MagicMock()
    mark_grace = mock.MagicMock()
    bump_poll = mock.MagicMock()
    mcp_server.set_app_hooks(create, send, close, mark_grace, bump_poll)
    assert mcp_server._app_mark_grace is mark_grace
    assert mcp_server._app_bump_poll is bump_poll


def test_watch_task_schedules_timer_on_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    # Create a session + task with a faked result.json
    s = task_manager.create_session("u@x", "uid", label="t")
    sid = s["session_id"]
    task_manager._update_session_field(sid, "pty_session_id", "pty-abc")
    t = task_manager.create_task(sid, "do thing", "u@x")
    tid = t["task_id"]
    tdir = task_manager._task_dir(sid, tid)
    task_manager._write_json(tdir + "/result.json", {"status": "completed"})

    mark = mock.MagicMock()
    bump = mock.MagicMock()
    closer = mock.MagicMock()
    mcp_server.set_app_hooks(mock.MagicMock(), mock.MagicMock(), closer, mark, bump)

    timer_created = []
    real_timer = threading.Timer

    def fake_timer(seconds, fn, args=None, kwargs=None):
        timer_created.append((seconds, fn, args))
        t = real_timer(seconds, fn, args=args, kwargs=kwargs)
        return t

    monkeypatch.setattr(mcp_server.threading, "Timer", fake_timer)

    # Use a very short watch interval and ensure no real Timer fires
    monkeypatch.setattr(mcp_server, "GRACE_PERIOD_S", 0.05)

    # Run one iteration manually
    mcp_server._watch_task(sid, tid, timeout_s=10)

    # Timer should be scheduled for GRACE_PERIOD_S seconds with closer + pty_session_id
    assert len(timer_created) == 1
    delay, fn, args = timer_created[0]
    assert delay == 0.05
    assert fn is closer
    assert args == ("pty-abc",)

    # _mark_grace and _bump_session_last_poll should have been called
    mark.assert_called_once_with("pty-abc")
    bump.assert_called_once_with("pty-abc", 0.05)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -v -k "set_app_hooks_accepts or watch_task_schedules"`
Expected: failures — extra params on `set_app_hooks` not accepted; `_watch_task` calls close synchronously.

- [ ] **Step 3: Extend `set_app_hooks` and module state**

In `coda_mcp/mcp_server.py`, at the top of the "App hooks" block (around line 70), expand:

```python
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
```

- [ ] **Step 4: Replace the immediate close inside `_watch_task`**

Replace the existing `_close_pty_for_session(session_id)` calls in `_watch_task` (one in the completion branch around line 117, one in the timeout branch around line 144) with the deferred-Timer helper. Add a new helper at the bottom of the existing helper section (right after `_close_pty_for_session` around line 161):

```python
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
```

Then in `_watch_task`, replace both occurrences of `_close_pty_for_session(session_id)` with `_schedule_deferred_close(session_id)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py -v -k "set_app_hooks_accepts or watch_task_schedules"`
Expected: 2 passed.

- [ ] **Step 6: Run full mcp_server test suite for regression**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: all pass (existing tests should be unaffected since hooks default to None).

- [ ] **Step 7: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_server.py tests/test_mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat(coda-mcp): defer PTY close by GRACE_PERIOD_S via threading.Timer"
```

---

## Task 7: `mcp_server.py` — return `viewer_url` from all three tools + pass `transcript_path` to PTY creation + update instructions

**Files:**
- Modify: `coda_mcp/mcp_server.py` (`coda_run` body, `coda_inbox` body, `coda_get_result` body, `instructions` block)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
import asyncio
import json
import os
from unittest import mock

from coda_mcp import mcp_server, task_manager, url_builder


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.run(coro)


def test_coda_run_includes_viewer_url_when_builder_returns_one(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")

    create = mock.MagicMock(return_value="pty-abc")
    send = mock.MagicMock()
    closer = mock.MagicMock()
    mcp_server.set_app_hooks(create, send, closer, mock.MagicMock(), mock.MagicMock())

    result_json = asyncio.run(mcp_server.coda_run(prompt="do it", email="u@x"))
    result = json.loads(result_json)
    assert result["status"] == "running"
    assert "?session=pty-abc" in result["viewer_url"]
    assert result["viewer_url"].startswith("https://app.example.com")


def test_coda_run_omits_viewer_url_when_builder_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", None)
    monkeypatch.delenv("CODA_APP_URL", raising=False)

    create = mock.MagicMock(return_value="pty-abc")
    mcp_server.set_app_hooks(create, mock.MagicMock(), mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    result_json = asyncio.run(mcp_server.coda_run(prompt="do it", email="u@x"))
    result = json.loads(result_json)
    # viewer_url present but None when builder returns None
    assert result.get("viewer_url") is None


def test_coda_run_passes_transcript_path_to_create_session(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    create = mock.MagicMock(return_value="pty-abc")
    mcp_server.set_app_hooks(create, mock.MagicMock(), mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

    asyncio.run(mcp_server.coda_run(prompt="do it", email="u@x"))
    # create_session was called with transcript_path=... pointing into ~/.coda/sessions/<sess>/tasks/<task>/transcript.log
    kwargs = create.call_args.kwargs
    assert "transcript_path" in kwargs
    assert kwargs["transcript_path"].endswith("transcript.log")
    assert "tasks" in kwargs["transcript_path"]


def test_coda_inbox_decorates_each_task_with_viewer_url(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")

    # Seed one session with one task and a pty_session_id
    s = task_manager.create_session("u@x", "uid", label="t")
    sid = s["session_id"]
    task_manager._update_session_field(sid, "pty_session_id", "pty-xyz")
    task_manager.create_task(sid, "prompt", "u@x")

    result_json = asyncio.run(mcp_server.coda_inbox())
    result = json.loads(result_json)
    assert len(result["tasks"]) == 1
    assert "viewer_url" in result["tasks"][0]
    assert "?session=pty-xyz" in result["tasks"][0]["viewer_url"]


def test_coda_get_result_includes_viewer_url(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")

    s = task_manager.create_session("u@x", "uid", label="t")
    sid = s["session_id"]
    task_manager._update_session_field(sid, "pty_session_id", "pty-xyz")
    t = task_manager.create_task(sid, "prompt", "u@x")
    tid = t["task_id"]
    tdir = task_manager._task_dir(sid, tid)
    task_manager._write_json(tdir + "/result.json", {
        "status": "completed", "summary": "ok",
    })

    result_json = asyncio.run(mcp_server.coda_get_result(tid, sid))
    result = json.loads(result_json)
    assert "viewer_url" in result
    assert "?session=pty-xyz" in result["viewer_url"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -v -k "viewer_url or transcript_path"`
Expected: failures — fields not present, `transcript_path` not passed.

- [ ] **Step 3: Modify `coda_run`**

In `coda_mcp/mcp_server.py`, at the top of the file add the import:

```python
from coda_mcp import url_builder
```

In the body of `coda_run` (around line 219), modify the PTY creation block to compute and pass the transcript path:

```python
        # Create PTY if hooks are wired
        if _app_create_session is not None:
            transcript_path = os.path.join(
                task_manager._task_dir(session_id, _new_task_id_preview := task_manager._new_task_id()),
                "transcript.log",
            )
```

Wait — `task_id` isn't known until after `task_manager.create_task`. Restructure: create the task FIRST (so we have task_id), then create the PTY with transcript path, then send the input. The existing order is: create_session → create_pty → update session with pty_id → create_task → send_input. We need: create_session → create_task → create_pty(transcript_path) → update session with pty_id → send_input.

Replace the existing PTY-create + create_task block (lines ~218-258) with this restructured version:

```python
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
```

- [ ] **Step 4: Add `viewer_url` to `coda_inbox` entries**

In `coda_inbox` (around line 300), after the `list_all_tasks` call, decorate each entry. Replace:

```python
        tasks = task_manager.list_all_tasks(email=email, status_filter=status)
```

with:

```python
        tasks = task_manager.list_all_tasks(email=email, status_filter=status)
        # Decorate each task with its viewer URL (if available).
        for t in tasks:
            sess = task_manager._read_session_safe(t["session_id"])
            pty = sess.get("pty_session_id") if sess else None
            if pty:
                vu = url_builder.build_viewer_url(pty)
                if vu:
                    t["viewer_url"] = vu
```

This requires adding `_read_session_safe` to `task_manager.py` — a wrapper that returns `None` instead of raising. Add it now in `coda_mcp/task_manager.py` next to `_read_session`:

```python
def _read_session_safe(session_id: str) -> dict | None:
    """Read session.json, returning None on missing/corrupt instead of raising."""
    try:
        return _read_session(session_id)
    except SessionNotFoundError:
        return None
```

- [ ] **Step 5: Add `viewer_url` to `coda_get_result`**

In `coda_get_result` (around line 327), after the existing field-setting block, add:

```python
        # Decorate with viewer_url if known
        sess = task_manager._read_session_safe(session_id)
        pty = sess.get("pty_session_id") if sess else None
        if pty:
            vu = url_builder.build_viewer_url(pty)
            if vu:
                result["viewer_url"] = vu
```

Place this immediately before `return json.dumps(result)`.

- [ ] **Step 6: Update FastMCP `instructions`**

In `coda_mcp/mcp_server.py`, modify the `instructions=` argument to FastMCP (around line 42) by appending a paragraph at the end of the existing instructions string:

```python
        "CHAINING: pass previous_session_id from a completed task's session_id "
        "to give the new task context of what was done before.\n\n"
        "SHARE THE LIVE URL: When coda_run returns a viewer_url field (non-null), "
        "mention it to the user in plain text (e.g. \"you can watch progress at "
        "<url>\"). The URL is safe to share — it points to the same Databricks App "
        "the user is already authenticated against. Do this on the first mention "
        "of the task and any time the user asks where the task is or how to see it."
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py -v -k "viewer_url or transcript_path"`
Expected: 5 passed.

- [ ] **Step 8: Run full mcp test suite for regression**

Run: `uv run pytest tests/test_mcp_server.py tests/test_mcp_integration.py -v`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_server.py coda_mcp/task_manager.py tests/test_mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat(coda-mcp): return viewer_url from coda_run/inbox/get_result + transcript wiring"
```

---

## Task 8: `mcp_asgi.py` — capture `X-Forwarded-Host` via ASGI middleware

**Files:**
- Modify: `coda_mcp/mcp_asgi.py` (add middleware class + register it on `mcp_starlette`)
- Test: `tests/test_app_url_middleware.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_app_url_middleware.py`:

```python
"""Tests for AppUrlCaptureMiddleware — populates url_builder._app_url_cache."""
import asyncio
import importlib

import pytest

from coda_mcp import url_builder


@pytest.fixture(autouse=True)
def _reset_cache():
    importlib.reload(url_builder)
    yield


async def _fake_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _make_scope(headers: list[tuple[bytes, bytes]]):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }


async def _drive(middleware, scope):
    sent = []
    async def send(msg): sent.append(msg)
    async def receive(): return {"type": "http.request", "body": b"", "more_body": False}
    await middleware(scope, receive, send)


def test_middleware_captures_x_forwarded_host():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = _make_scope([(b"x-forwarded-host", b"app.databricksapps.com")])
    asyncio.run(_drive(mw, scope))
    assert url_builder._app_url_cache == "app.databricksapps.com"


def test_middleware_falls_back_to_host_when_no_xforwarded():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = _make_scope([(b"host", b"localhost:8000")])
    asyncio.run(_drive(mw, scope))
    assert url_builder._app_url_cache == "localhost:8000"


def test_middleware_skips_non_http_scope():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = {"type": "lifespan"}
    async def receive(): return {"type": "lifespan.startup"}
    sent = []
    async def send(msg): sent.append(msg)
    # Must not crash. Cache stays None.
    asyncio.run(mw(scope, receive, send))
    assert url_builder._app_url_cache is None


def test_middleware_no_op_when_no_host_header():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = _make_scope([])
    asyncio.run(_drive(mw, scope))
    assert url_builder._app_url_cache is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app_url_middleware.py -v`
Expected: ImportError on `AppUrlCaptureMiddleware`.

- [ ] **Step 3: Add the middleware class to `mcp_asgi.py`**

At the top of `coda_mcp/mcp_asgi.py` (after imports, around line 28), add:

```python
from coda_mcp import url_builder


class AppUrlCaptureMiddleware:
    """Capture X-Forwarded-Host (or Host) from every inbound HTTP request and
    populate url_builder._app_url_cache. Used so MCP tools can return a
    working viewer_url without manual configuration.

    Caveat: /socket.io/ traffic is intercepted by socketio.ASGIApp *before*
    reaching mcp_starlette, so WebSocket connect requests never hit this
    middleware. This is fine in practice — every HTTP request to /mcp and to
    Flask routes does hit it, which is enough to keep the cache hot.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            host_bytes = headers.get(b"x-forwarded-host") or headers.get(b"host")
            if host_bytes:
                try:
                    url_builder.capture_from_headers(host_bytes.decode("latin-1"))
                except Exception:
                    pass
        await self.app(scope, receive, send)
```

- [ ] **Step 4: Register the middleware on `mcp_starlette`**

In the existing block that adds CORS (around lines 80-86):

```python
# CORS for MCP and Flask routes
mcp_starlette.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Add a second `add_middleware` call immediately after:

```python
# Capture X-Forwarded-Host into url_builder cache (for MCP viewer_url).
# Added AFTER CORS so it wraps the CORS-handled request.
mcp_starlette.add_middleware(AppUrlCaptureMiddleware)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_app_url_middleware.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_asgi.py tests/test_app_url_middleware.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat(coda-mcp): AppUrlCaptureMiddleware seeds url_builder from X-Forwarded-Host"
```

---

## Task 9: `app.py::attach_session` — replay fallback when PTY is gone

**Files:**
- Modify: `app.py::attach_session` (lines ~1104-1123)
- Test: `tests/test_replay_attach.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_replay_attach.py`:

```python
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
    from app import app
    # Bypass authorization (single-user app pattern used by other tests)
    monkeypatch.setattr("app.check_authorization", lambda: True)
    with app.test_client() as c:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_replay_attach.py -v`
Expected: replay test fails (no fallback); 404 test passes already.

- [ ] **Step 3: Modify `attach_session`**

In `app.py::attach_session` (around line 1104), replace the body with:

```python
@app.route("/api/session/attach", methods=["POST"])
def attach_session():
    """Reattach to an existing session — returns buffered output for replay.

    If the live PTY is gone but an on-disk transcript exists for this
    pty_session_id, return the transcript as ``output`` with ``replay: True``.
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")

    sess = _get_session(session_id)
    if not sess or sess.get("exited"):
        # Replay fallback: look up transcript.log by pty_session_id
        from coda_mcp import task_manager as _tm
        tdir = _tm.find_task_dir_by_pty_session(session_id)
        if tdir:
            transcript = os.path.join(tdir, "transcript.log")
            if os.path.isfile(transcript):
                try:
                    with open(transcript, "rb") as f:
                        content = f.read()
                    return jsonify({
                        "session_id": session_id,
                        "label": "hermes-mcp (replay)",
                        "output": [content.decode("utf-8", errors="replace")],
                        "replay": True,
                        "process": None,
                        "created_at": None,
                    })
                except OSError:
                    pass
        return jsonify({"error": "Session not found or exited"}), 404

    # Existing live-attach path
    sess["last_poll_time"] = time.time()
    return jsonify({
        "session_id": session_id,
        "label": sess.get("label", ""),
        "output": list(sess["output_buffer"]),
        "process": _get_session_process(sess["pid"]),
        "created_at": sess.get("created_at"),
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_replay_attach.py -v`
Expected: 2 passed.

- [ ] **Step 5: Run regression for the existing session-attach tests**

Run: `uv run pytest tests/test_session_detach.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add app.py tests/test_replay_attach.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: attach_session replay fallback reads transcript.log when PTY is gone"
```

---

## Task 10: `static/index.html` — boot URL parse + `_doReplay` + history hygiene

**Files:**
- Modify: `static/index.html`

> **Note**: This is the most "real" change. We add ~50-70 LoC of JS. Tested manually (Playwright not configured in this repo).

- [ ] **Step 1: Locate the SPA boot path**

Read `static/index.html` lines 990-1030 (the existing session-picker boot logic) to confirm where pane creation happens after the picker. The new URL-driven branch must run before the picker.

- [ ] **Step 2: Add boot-time URL parse**

Find the existing function that runs on `DOMContentLoaded` or the IIFE that initializes the app. Just before it would invoke the session picker, insert:

```javascript
    // ── Deep-link to a CoDA MCP session via ?session=<pty_id> ──
    async function _initFromQueryString() {
      const params = new URLSearchParams(location.search);
      const sessionId = params.get('session');
      if (!sessionId) return false;

      try {
        const resp = await fetch('/api/session/attach', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId })
        });

        if (resp.status === 404) {
          _renderExpiredPage(sessionId);
          return true;  // handled, skip picker
        }

        const data = await resp.json();
        const term = createTerminalPane({ sessionId, label: data.label || sessionId });

        if (data.replay) {
          const content = (data.output || []).join('');
          await _doReplay(term, sessionId, content);
        } else {
          await _doAttach(term, sessionId);
          if (typeof socket !== 'undefined' && socket) {
            socket.emit('join_session', { session_id: sessionId });
          }
        }

        return true;  // handled, skip picker
      } catch (err) {
        console.error('deep-link attach failed:', err);
        return false;
      }
    }
```

`createTerminalPane({ sessionId, label })` is the name commonly used in this repo for pane creation; if the actual name differs, substitute the local helper. Read the existing pane creation site to confirm and adjust the call site accordingly.

- [ ] **Step 3: Add `_doReplay`**

Place near `_doAttach` (around line 1339):

```javascript
    async function _doReplay(term, sessionId, content) {
      // Chunk the write to avoid main-thread jank on multi-MB transcripts.
      const CHUNK = 64 * 1024;
      for (let i = 0; i < content.length; i += CHUNK) {
        term.write(content.slice(i, i + CHUNK));
        await new Promise(r => requestAnimationFrame(r));
      }
      // Mount a static banner above the pane.
      _showReplayBanner(term, sessionId);
      // NOTE: do NOT wire term.onData → terminal_input; do NOT include in heartbeat
      // session_ids list; do NOT emit join_session.
      return sessionId;
    }

    function _showReplayBanner(term, sessionId) {
      const pane = getAllPanes().find(p => p.sessionId === sessionId);
      if (!pane || !pane.element) return;
      const banner = document.createElement('div');
      banner.className = 'replay-banner';
      banner.textContent = 'Task completed — viewing replay';
      banner.style.cssText = 'padding:4px 8px;background:#333;color:#aaa;font-size:12px;text-align:center;';
      pane.element.insertBefore(banner, pane.element.firstChild);
    }
```

- [ ] **Step 4: Add `_renderExpiredPage`**

Place near `_doReplay`:

```javascript
    function _renderExpiredPage(sessionId) {
      const root = document.body;
      root.innerHTML = `
        <div style="font-family:monospace;padding:40px;text-align:center;color:#ccc;">
          <h2>Session expired</h2>
          <p>Session <code>${sessionId.replace(/[<>]/g, '')}</code> is gone, and no replay is available.</p>
          <p>The transcript may have aged out after the 24-hour retention window.</p>
          <p><a href="/" style="color:#6cf;">← Back to terminal</a></p>
        </div>
      `;
    }
```

- [ ] **Step 5: Wire `_initFromQueryString` into the boot path**

Find where the existing session-picker is shown after `DOMContentLoaded`. Wrap it:

```javascript
    document.addEventListener('DOMContentLoaded', async () => {
      // existing init code (sockets, themes, etc.)

      const handled = await _initFromQueryString();
      if (handled) return;

      // existing flow (show session picker, etc.)
    });
```

The exact insertion site depends on the existing boot structure — read lines 990-1050 of `static/index.html` to find the right place.

- [ ] **Step 6: Add history hygiene on pane close**

Locate the existing pane-close handler. Inside, after the pane is removed, add:

```javascript
        // If this pane was opened via ?session=<id>, drop the query param so a
        // refresh doesn't re-attach to a stale id.
        const params = new URLSearchParams(location.search);
        if (params.get('session') === pane.sessionId) {
          history.replaceState({}, '', '/');
        }
```

- [ ] **Step 7: Manual smoke test**

Local dev:

```bash
uv run uvicorn coda_mcp.mcp_asgi:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000/?session=fake-id` in a browser. Expected: "Session expired" page (404 since no transcript exists).

Create a fake live session via the regular UI, note its session_id from the picker, then navigate to `http://localhost:8000/?session=<that_id>` — expected: terminal opens directly attached to that session.

- [ ] **Step 8: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add static/index.html
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat(spa): deep-link ?session=<pty_id> with live attach + replay rendering"
```

---

## Task 11: Integration test — E2E grace period + transcript replay

**Files:**
- Modify: `tests/test_mcp_integration.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_integration.py`:

```python
import asyncio
import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from coda_mcp import mcp_server, task_manager, url_builder


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")
    # Shrink grace for the test
    monkeypatch.setattr(mcp_server, "GRACE_PERIOD_S", 2)
    return tmp_path


def test_end_to_end_grace_and_replay(mcp_env, monkeypatch):
    """Stub hermes via direct file I/O, then exercise the full coda_run flow."""
    from app import mcp_create_pty_session, mcp_send_input, mcp_close_pty_session
    from app import _mark_grace_for_session, _bump_session_last_poll, sessions

    mcp_server.set_app_hooks(
        mcp_create_pty_session, mcp_send_input, mcp_close_pty_session,
        _mark_grace_for_session, _bump_session_last_poll,
    )

    # Submit a fake task
    result_json = asyncio.run(mcp_server.coda_run(
        prompt="test", email="u@x", timeout_s=5,
    ))
    result = json.loads(result_json)
    assert result["status"] == "running"
    sess_id = result["session_id"]
    task_id = result["task_id"]
    pty_id = task_manager._read_session(sess_id)["pty_session_id"]

    # viewer_url returned
    assert pty_id in result["viewer_url"]

    # Simulate hermes writing to the PTY by sending input that echoes to bash
    mcp_send_input(pty_id, "echo HELLO_FROM_HERMES\n")
    time.sleep(0.5)

    # Now simulate hermes completion by writing result.json
    tdir = task_manager._task_dir(sess_id, task_id)
    Path(tdir).joinpath("result.json").write_text(json.dumps({
        "status": "completed", "summary": "stub", "files_changed": [],
        "artifacts": {}, "errors": [],
    }))

    # Wait for watcher to pick it up (polls every 5s — shorten via patch below if slow)
    # In practice, the test patches the poll interval. For now, manually invoke:
    mcp_server._schedule_deferred_close(sess_id)

    # PTY still alive immediately after grace scheduling
    assert pty_id in sessions
    assert sessions[pty_id]["grace"] is True

    # Wait past GRACE_PERIOD_S
    time.sleep(2.5)

    # PTY now gone
    assert pty_id not in sessions

    # Transcript file exists and contains the echoed line
    transcript = Path(tdir) / "transcript.log"
    assert transcript.exists()
    assert b"HELLO_FROM_HERMES" in transcript.read_bytes()

    # find_task_dir_by_pty_session now returns the task dir from the on-disk record
    found = task_manager.find_task_dir_by_pty_session(pty_id)
    assert found == str(tdir)
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_mcp_integration.py -v -k end_to_end_grace_and_replay`
Expected: pass.

- [ ] **Step 3: Run the full test suite for regression**

Run: `uv run pytest tests/ -v --timeout=60`
Expected: prior pass count + the new tests. No failures.

- [ ] **Step 4: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add tests/test_mcp_integration.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "test: E2E coverage for grace period + transcript replay"
```

---

## Task 12: Manual smoke + deployment verification

**Files:** none (verification only)

- [ ] **Step 1: Deploy the worktree to the test app**

From the worktree root:

```bash
databricks bundle deploy --target test-coda
```

(Adjust target name to whatever the existing deployment uses — check `databricks.yml` or `app.yaml` notes.)

- [ ] **Step 2: Verify in Genie Code**

In the Databricks workspace, open Genie Code, ensure the Custom MCP server `mcp-test-coda` is connected. Submit a simple task: `"List the files in /tmp"`.

Expected:
- Genie Code's response mentions a `viewer_url` like `https://mcp-test-coda-<workspace_id>.aws.databricksapps.com/?session=<pty_id>`.
- Clicking the URL opens the terminal pre-attached to that session.
- Hermes output streams in real time.

- [ ] **Step 3: Verify replay**

After the task completes, wait 6+ minutes (grace period + buffer), then reload the same URL.

Expected:
- Page loads showing the static transcript of what hermes did.
- "Task completed — viewing replay" banner.
- No input is sent when you type.

- [ ] **Step 4: Verify chmod on transcript**

From a shell in the deployed app (workspace terminal or `databricks workspace files` API):

```bash
ls -la ~/.coda/sessions/*/tasks/*/transcript.log
```

Expected: files have mode `-rw-------` (0o600).

- [ ] **Step 5: Verify `viewer_url` absence locally without env**

```bash
unset CODA_APP_URL
uv run uvicorn coda_mcp.mcp_asgi:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

# Submit a coda_run via curl-formatted JSON-RPC
curl -s http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"coda_run","arguments":{"prompt":"test","email":"local@dev"}}}'

kill $SERVER_PID
```

Expected: the JSON response contains `"viewer_url": "http://localhost:8000/?session=..."` (because the inbound `Host: localhost:8000` was captured).

- [ ] **Step 6: Final commit (if any verification turned up a fix)**

If smoke tests revealed issues, fix them as separate commits, then update this checklist.

---

## Self-review notes

- All eight spec decisions covered: §1 viewer mode → Task 10 `_doReplay`; §2 transcript tee → Tasks 3-4; §3 deferred Timer → Task 6; §4 grace exemption → Task 5; §5 URL form → Tasks 1, 7; §6 ASGI middleware → Task 8; §7 attach replay fallback → Task 9; §8 SPA → Task 10.
- No "TODO" / "TBD" / "implement later" / placeholder text — every step has concrete code, exact paths, exact commands.
- Type/method consistency:
  - `set_app_hooks` signature in Task 6 matches the call site updated in Task 11 (`mcp_server.set_app_hooks(create, send, close, mark_grace, bump_poll)` with optional defaults).
  - `_mark_grace_for_session` / `_bump_session_last_poll` defined in Task 5 used by Task 6 and Task 11.
  - `transcript_path` kwarg added to `mcp_create_pty_session` in Task 4 used by `coda_run` in Task 7.
  - `find_task_dir_by_pty_session` defined in Task 2 used by `attach_session` in Task 9.
  - `url_builder.build_viewer_url` defined in Task 1 used by `coda_run`/`coda_inbox`/`coda_get_result` in Task 7.
- Spec §3 "Architecture" diagram preserved as the mental model; data flows §5.1-5.4 map to Tasks 7, 9, 6, 9 respectively.
- Risks §9 (secrets, grace race, multi-tab) accepted in the spec; surface in the test plan via the chmod-600 verification in Task 12 step 4.
