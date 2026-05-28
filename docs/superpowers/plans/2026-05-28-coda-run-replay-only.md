# `coda_run` Replay-Only URL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `coda_run`'s returned `viewer_url` resolve to a read-only static transcript replay (never a live PTY attach), and rip out the unwired 5-minute grace-period machinery from PR #66 as a consequence.

**Architecture:** Mode 3 in the three-mode framework (see spec `docs/superpowers/specs/2026-05-28-coda-run-replay-only-design.md`). A new `replay_only` boolean on the PTY session dict steers the existing `/api/session/attach` endpoint into the transcript-from-disk path unconditionally for `coda_run`-created sessions. The watcher closes the PTY immediately on task completion — no deferred timer.

**Tech Stack:** Python 3.11 + Flask + FastMCP + uvicorn (ASGI) + pytest. No new deps. All changes localized to `app.py`, `coda_mcp/mcp_server.py`, and the test suite.

---

## Pre-flight check (do before Task 1)

- [ ] **P1: Verify baseline tests pass.**

```bash
cd /Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp
.venv/bin/python -m pytest tests/ -x --ignore=tests/test_e2e.py -q 2>&1 | tail -20
```

Expected: All pass (~527 passed + ~11 PTY-gated skipped). If anything fails on `main` for unrelated reasons, stop and report.

- [ ] **P2: Confirm worktree is on the `feat/coda-mcp-live-session-url` branch.**

```bash
git branch --show-current
```

Expected: `feat/coda-mcp-live-session-url`

---

## Task 1: Add `replay_only` parameter to `mcp_create_pty_session`

Backward-compatible default (`False`) so existing callers (direct-launch via `create_session`, future `coda_interactive`) keep their behavior unchanged.

**Files:**
- Modify: `app.py` (function `mcp_create_pty_session`, line ~1402, and the session-dict insert at ~1469)
- Create: `tests/test_replay_only_flag.py`

- [ ] **Step 1: Write the failing test.**

Create `tests/test_replay_only_flag.py`:

```python
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
```

- [ ] **Step 2: Run the test and verify it fails.**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py -v 2>&1 | tail -15
```

Expected: 2 failures. First test fails with `TypeError: mcp_create_pty_session() got an unexpected keyword argument 'replay_only'`. Second test fails with `assert None is False` (the key doesn't exist yet so `.get` returns None, which is not `False`).

- [ ] **Step 3: Add the parameter and storage.**

In `app.py`, change the `mcp_create_pty_session` signature (search for `def mcp_create_pty_session`):

```python
# Before:
def mcp_create_pty_session(label: str = "hermes-mcp", transcript_path: str | None = None) -> str:

# After:
def mcp_create_pty_session(
    label: str = "hermes-mcp",
    transcript_path: str | None = None,
    replay_only: bool = False,
) -> str:
```

In the same function, add the `replay_only` key to the session dict that's being built (find the dict literal that contains `"grace": False,` — that's the one). Add right after the existing `"grace": False,` line:

```python
                "grace": False,
                "replay_only": replay_only,   # NEW
```

(The `"grace": False,` line gets removed entirely in Task 8 — leave it alone here.)

- [ ] **Step 4: Run the test and verify it passes.**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py -v 2>&1 | tail -10
```

Expected: 2 passed.

- [ ] **Step 5: Commit.**

```bash
git add app.py tests/test_replay_only_flag.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: add replay_only param to mcp_create_pty_session

Backward-compatible default (False). Stored in session dict for later
attach-time enforcement."
```

---

## Task 2: Extract `_serve_transcript_replay` helper from `attach_session`

Pure refactor. Extracts the transcript-from-disk lookup currently inlined in `attach_session` at `app.py:1170-1188` into a reusable helper. Existing tests (`tests/test_replay_attach.py`) act as the safety net.

**Files:**
- Modify: `app.py` (`attach_session` at ~1158, plus new helper above it)

- [ ] **Step 1: Verify existing replay tests pass (the safety net).**

```bash
.venv/bin/python -m pytest tests/test_replay_attach.py -v 2>&1 | tail -10
```

Expected: 2 passed (the two tests that already exist for transcript-after-PTY-exit replay).

- [ ] **Step 2: Add the helper just above `attach_session`.**

In `app.py`, find `@app.route("/api/session/attach"` (around line 1157). Just **above** the `@app.route` decorator, add this helper:

```python
def _serve_transcript_replay(session_id: str):
    """Serve the on-disk transcript for a PTY session as a replay response.

    Used by attach_session() in two cases:
      1. The PTY is gone (existing transcript-fallback path).
      2. The PTY exists but is replay_only=True (new in Task 3).

    Returns either a Flask JSON response with replay=True, or a 404 if no
    transcript exists for this pty_session_id.
    """
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
```

- [ ] **Step 3: Replace the inlined block in `attach_session` with a helper call.**

Inside `attach_session`, find the block:

```python
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
```

Replace it with:

```python
    sess = _get_session(session_id)
    if not sess or sess.get("exited"):
        return _serve_transcript_replay(session_id)
```

- [ ] **Step 4: Run replay tests to verify behavior is preserved.**

```bash
.venv/bin/python -m pytest tests/test_replay_attach.py tests/test_transcript.py -v 2>&1 | tail -20
```

Expected: All pass (refactor is behavior-preserving).

- [ ] **Step 5: Commit.**

```bash
git add app.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "refactor: extract _serve_transcript_replay helper from attach_session

Pure refactor — no behavior change. Helper is also used by the new
replay_only short-circuit in the next commit."
```

---

## Task 3: Enforce `replay_only=True` in `attach_session`

New early-return: if the live session has `replay_only=True`, serve the transcript regardless of whether the PTY is still alive.

**Files:**
- Modify: `app.py` (`attach_session`)
- Modify: `tests/test_replay_only_flag.py`

- [ ] **Step 1: Add two failing tests.**

Append to `tests/test_replay_only_flag.py`:

```python
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
            '{"session_id": "%s", "pty_session_id": "%s"}' % (sess_id, sid)
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
```

- [ ] **Step 2: Run the new tests and verify they fail (first one only — second should pass already).**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py -v 2>&1 | tail -20
```

Expected: `test_attach_session_replay_only_alive_pty_returns_replay` FAILS (because the alive PTY currently returns the live buffer, not the transcript). `test_attach_session_replay_only_false_alive_pty_returns_live_buffer` PASSES (existing behavior is correct). The two Task 1 tests still pass.

- [ ] **Step 3: Add the early-return in `attach_session`.**

In `app.py`, modify the body of `attach_session`. Find:

```python
    sess = _get_session(session_id)
    if not sess or sess.get("exited"):
        return _serve_transcript_replay(session_id)
```

Insert the new replay-only check **between** the `_get_session` call and the `if not sess` check:

```python
    sess = _get_session(session_id)

    # Replay-only sessions (e.g. those created by coda_run) always serve the
    # transcript-from-disk, even when the PTY is still alive.
    if sess and sess.get("replay_only"):
        return _serve_transcript_replay(session_id)

    if not sess or sess.get("exited"):
        return _serve_transcript_replay(session_id)
```

- [ ] **Step 4: Run the new tests and verify they pass.**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py -v 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 5: Commit.**

```bash
git add app.py tests/test_replay_only_flag.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: replay_only PTY sessions short-circuit to transcript in attach_session

Replay-only sessions always serve the on-disk transcript regardless of
whether the PTY is still alive. Used by coda_run (wired in the next commit)."
```

---

## Task 4: Wire `coda_run` to pass `replay_only=True`

One-line change in the call to `_app_create_session` (the hook that points to `mcp_create_pty_session`).

**Files:**
- Modify: `coda_mcp/mcp_server.py` (around line 289 — the `_app_create_session(...)` call inside `coda_run`)
- Modify: `tests/test_replay_only_flag.py`

- [ ] **Step 1: Add a failing test.**

Append to `tests/test_replay_only_flag.py`:

```python
@_pty_skip
def test_coda_run_creates_pty_with_replay_only_true(tmp_path, monkeypatch):
    """coda_run must create its PTY with replay_only=True."""
    import asyncio
    import json
    from app import sessions
    from coda_mcp import mcp_server, task_manager

    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    # Stop the watcher from racing the test — we only care about creation here.
    monkeypatch.setattr(mcp_server, "_watch_task", lambda *a, **kw: None)

    result_str = asyncio.run(mcp_server.coda_run(prompt="ignored", email="t@example.com"))
    result = json.loads(result_str)
    pty_id = task_manager._read_session(result["session_id"])["pty_session_id"]
    try:
        assert sessions[pty_id].get("replay_only") is True
    finally:
        from app import mcp_close_pty_session
        mcp_close_pty_session(pty_id)
```

- [ ] **Step 2: Run and verify failure.**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py::test_coda_run_creates_pty_with_replay_only_true -v 2>&1 | tail -10
```

Expected: FAIL — `assert None is True` (or `assert False is True`) because `coda_run` is not yet passing the flag.

- [ ] **Step 3: Modify `coda_run` in `coda_mcp/mcp_server.py`.**

Find the `_app_create_session(...)` call inside `coda_run` (search for `pty_session_id = _app_create_session(`). Currently:

```python
            pty_session_id = _app_create_session(
                label="hermes-mcp",
                transcript_path=transcript_path,
            )
```

Add the new kwarg:

```python
            pty_session_id = _app_create_session(
                label="hermes-mcp",
                transcript_path=transcript_path,
                replay_only=True,   # NEW: coda_run URLs are post-hoc review only
            )
```

- [ ] **Step 4: Run and verify pass.**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 5: Commit.**

```bash
git add coda_mcp/mcp_server.py tests/test_replay_only_flag.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "feat: coda_run creates PTY sessions with replay_only=True

Mode 3 in the three-mode framework. The viewer_url returned by coda_run
now always resolves to a transcript-from-disk replay."
```

---

## Task 5: Switch `_watch_task` to immediate PTY close (pure refactor)

Replace `_schedule_deferred_close(session_id)` with `_close_pty_immediately(session_id)` in `_watch_task`. Both functions already exist — this is a one-name-for-another swap. **Not a TDD task** — existing tests (specifically `tests/test_mcp_integration.py`, which already calls `_close_pty_immediately`-equivalent paths directly) act as the safety net. The "no timer" behavior is hard to test as a red-green cycle without instrumenting the watcher's polling loop, which isn't worth the complexity here.

**Files:**
- Modify: `coda_mcp/mcp_server.py` (`_watch_task`, around lines 133 and 160)

- [ ] **Step 1: Confirm existing safety-net tests pass.**

```bash
.venv/bin/python -m pytest tests/test_mcp_integration.py tests/test_mcp_server.py -v 2>&1 | tail -10
```

Expected: All pass. These tests cover `_watch_task`'s completion path and `_close_pty_immediately`'s teardown.

- [ ] **Step 2: Locate the call sites.**

```bash
grep -n "_schedule_deferred_close" coda_mcp/mcp_server.py
```

Expected: 3 matches — one at the function definition (~line 186), two call sites inside `_watch_task` (~lines 133 and 160). You're swapping the two call sites; the definition gets deleted in Task 7.

- [ ] **Step 3: Swap the calls in `_watch_task`.**

In `coda_mcp/mcp_server.py`, at each of the **two** call sites inside `_watch_task` (the success branch and the timeout branch), replace:

```python
# Before:
_schedule_deferred_close(session_id)

# After:
_close_pty_immediately(session_id)
```

Leave the `_schedule_deferred_close` function definition alone for now — it becomes dead code that Task 7 deletes.

- [ ] **Step 4: Re-run the safety-net tests.**

```bash
.venv/bin/python -m pytest tests/test_mcp_integration.py tests/test_mcp_server.py -v 2>&1 | tail -10
```

Expected: All pass. Behavior is preserved at the test-observable level (the watcher still drives a teardown after completion); only the timing changes (immediate vs. 5-min deferred), and no current test asserts the 5-min delay (the grace-timing tests use `monkeypatch` to shrink it to milliseconds).

- [ ] **Step 5: Confirm via grep that `_watch_task` no longer calls `_schedule_deferred_close`.**

```bash
grep -n "_schedule_deferred_close" coda_mcp/mcp_server.py
```

Expected: 1 match (only the function definition itself, which Task 7 will delete).

- [ ] **Step 6: Commit.**

```bash
git add coda_mcp/mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "refactor: _watch_task uses _close_pty_immediately instead of deferred close

Pure call-site swap. Behavior change: PTY teardown is immediate rather
than 5-minute-deferred. _schedule_deferred_close becomes dead code,
ripped out in a follow-up commit."
```

---

## Task 6: Drop dead grace tests

Now that no production code path calls grace machinery, the tests that exercise it can go. Doing this BEFORE the code rip-out keeps the suite green at every commit.

**Files:**
- Modify: `tests/test_transcript.py` (delete 4 tests)
- Modify: `tests/test_mcp_server.py` (delete 2 tests + setup/teardown grace lines)
- Modify: `tests/test_mcp_integration.py` (delete 1 test)

- [ ] **Step 1: Delete grace tests from `tests/test_transcript.py`.**

Open `tests/test_transcript.py`. Delete these **4 test functions in full** (each is one block from `def` line through to the next blank line / next `def`):

| Test | Approx line |
|---|---|
| `def test_grace_period_pty_does_not_count_toward_max(monkeypatch):` | 135 |
| `def test_bump_session_last_poll_advances_clock(monkeypatch):` | 157 |
| `def test_mark_grace_on_missing_session_is_noop():` | 169 |
| `def test_bump_session_last_poll_missing_is_noop():` | 174 |

Re-verify after deletion:

```bash
grep -n "grace\|_mark_grace\|_bump_session\|GRACE" tests/test_transcript.py
```

Expected: no matches.

- [ ] **Step 2: Delete grace tests from `tests/test_mcp_server.py`.**

Delete:
- `def test_set_app_hooks_accepts_grace_and_bump_hooks():` (around line 361)
- The function that starts at line ~399 (the `monkeypatch.setattr(mcp_server, "GRACE_PERIOD_S", 0.05)` one — search for `GRACE_PERIOD_S` to find it).

Also in the setup/teardown fixtures at the top of the file (lines 21-22 and 27-28), remove the lines:

```python
    mcp_server._app_mark_grace = None
    mcp_server._app_bump_poll = None
```

Verify:

```bash
grep -n "grace\|mark_grace\|bump_poll\|GRACE" tests/test_mcp_server.py
```

Expected: no matches.

- [ ] **Step 3: Delete the grace E2E test from `tests/test_mcp_integration.py`.**

Delete the entire `# ── 7. E2E: grace period + transcript replay ────────────────────────` section. Specifically:
- The section header comment at line ~293
- The full `def test_end_to_end_grace_and_replay(tmp_path, monkeypatch):` function (starts line 315, ends after line ~408)

Verify:

```bash
grep -n "grace\|GRACE\|_mark_grace" tests/test_mcp_integration.py
```

Expected: no matches.

- [ ] **Step 4: Run the full suite — must still pass.**

```bash
.venv/bin/python -m pytest tests/ -x --ignore=tests/test_e2e.py -q 2>&1 | tail -20
```

Expected: All remaining tests pass. The grace tests are gone; nothing imports `_mark_grace_for_session` or `GRACE_PERIOD_S` from test code anymore.

- [ ] **Step 5: Commit.**

```bash
git add tests/test_transcript.py tests/test_mcp_server.py tests/test_mcp_integration.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "test: drop dead grace-period tests

Prep for grace-machinery rip-out in follow-up commits. Removes 7 tests
that exercised code paths now superseded by replay_only + immediate close."
```

---

## Task 7: Rip out grace machinery from `coda_mcp/mcp_server.py`

Delete `_schedule_deferred_close`, the grace hook slots, and the `GRACE_PERIOD_S` constant. Also clean up `set_app_hooks` and `_close_pty_immediately`'s docstring.

**Files:**
- Modify: `coda_mcp/mcp_server.py`

- [ ] **Step 1: Verify nothing in the suite imports the symbols you're about to delete.**

```bash
grep -rn "_schedule_deferred_close\|_app_mark_grace\|_app_bump_poll\|GRACE_PERIOD_S" coda_mcp/ tests/ app.py
```

Expected: Only matches inside `coda_mcp/mcp_server.py`. If any tests still import these, return to Task 6.

- [ ] **Step 2: Remove the dead module-level state and the function.**

In `coda_mcp/mcp_server.py`:

- Delete lines 79-80: `_app_mark_grace = None` and `_app_bump_poll = None`
- Delete line 82: `GRACE_PERIOD_S = 300  # 5 minutes`
- Delete the entire `_schedule_deferred_close` function (lines ~186-213). Search for `def _schedule_deferred_close` and delete from that line through the function's closing line.

- [ ] **Step 3: Update `set_app_hooks` signature.**

Find `def set_app_hooks(` (around line 85). Currently it accepts `mark_grace_fn` and `bump_poll_fn` parameters. Remove those parameters from the signature, and remove the lines inside the function body that assign them to the module-level slots (`_app_mark_grace = mark_grace_fn`, `_app_bump_poll = bump_poll_fn`).

Also update the function's docstring — search for the line that mentions "defer PTY close by ``GRACE_PERIOD_S``" and rewrite the docstring to remove grace references entirely.

- [ ] **Step 4: Update `_close_pty_immediately` docstring.**

Find `def _close_pty_immediately(` (around line 167). Its docstring currently says it's for "emergency teardown or tests". Rewrite to reflect that it's the normal close path:

```python
def _close_pty_immediately(session_id: str) -> None:
    """Close the PTY session associated with this task session immediately.

    Called by ``_watch_task`` as soon as the task transitions to completed
    or failed. Reads ``pty_session_id`` from the task-manager's session.json
    and calls the ``_app_close_session`` hook (i.e. ``mcp_close_pty_session``
    in production).
    """
```

- [ ] **Step 5: Update the module-level docstring.**

At the top of `coda_mcp/mcp_server.py`, find the line that mentions hooks (around line 9: "handled through optional app hooks set via ``set_app_hooks()``."). Make sure it doesn't claim grace functionality. Search for any other comment block referencing grace and remove.

- [ ] **Step 6: Run the suite.**

```bash
.venv/bin/python -m pytest tests/ -x --ignore=tests/test_e2e.py -q 2>&1 | tail -15
```

Expected: All pass.

- [ ] **Step 7: Commit.**

```bash
git add coda_mcp/mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "refactor: rip out grace-period machinery from coda_mcp/mcp_server.py

Removes _schedule_deferred_close, GRACE_PERIOD_S, the unused grace hook
slots, and the corresponding set_app_hooks parameters. The grace hooks
were never wired in production — this is dead code removal, not a
behavior change."
```

---

## Task 8: Rip out grace machinery from `app.py`

Delete `_mark_grace_for_session`, `_bump_session_last_poll`, the `grace` key from the session dict creation, and the `MAX_CONCURRENT_SESSIONS` exclusion at all 4 sites.

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Remove the `"grace": False,` key from session dict creation in `mcp_create_pty_session`.**

In `app.py`, find the dict literal in `mcp_create_pty_session` that contains `"grace": False,` (around line 1477). Delete that single line. The `replay_only` line you added in Task 1 stays.

There may be ANOTHER similar `"grace": False,` line in the other session-creation path inside `create_session` (search the file for `"grace": False,` — there may be 2 occurrences). Delete both.

```bash
grep -n '"grace"' app.py
```

Expected after deletion: no matches.

- [ ] **Step 2: Revert the `MAX_CONCURRENT_SESSIONS` exclusion at 4 sites.**

Search for `sum(1 for s in sessions.values() if not s.get("grace"))`:

```bash
grep -n "if not s.get(\"grace\")" app.py
```

Expected: 4 matches at lines around 1329, 1369, 1405, 1456.

**CRITICAL — locking note:** All 4 sites are **already** inside a `with sessions_lock:` block (the lock is acquired by the surrounding session-creation code immediately before the check). `sessions_lock` is `threading.Lock()` (not `RLock`), so **do NOT** wrap the replacement in another `with sessions_lock:` — that will deadlock. Just use `len(sessions)` directly.

At each of the 4 sites, replace:

```python
# Before (inside an existing `with sessions_lock:` block):
active = sum(1 for s in sessions.values() if not s.get("grace"))
if active >= MAX_CONCURRENT_SESSIONS:
    ...
```

With:

```python
# After (still inside the same `with sessions_lock:` block — no new lock):
active = len(sessions)
if active >= MAX_CONCURRENT_SESSIONS:
    ...
```

To verify each site really is inside a lock block, read the ~5 lines preceding each `sum(...)` call. You should see `with sessions_lock:` at lines 1328, 1366 (for site 1369), 1404 (for site 1405), and 1455 (for site 1456). If any site is somehow NOT already locked, stop and ask before proceeding — the original code may have a latent bug worth investigating.

- [ ] **Step 3: Delete `_mark_grace_for_session` and `_bump_session_last_poll`.**

Find both functions (around lines 1515 and 1530). Delete each function definition in full.

- [ ] **Step 4: Verify no stale references.**

```bash
grep -n "grace\|_mark_grace\|_bump_session_last_poll" app.py
```

Expected: no matches (or only comment lines that reference history — delete those too).

- [ ] **Step 5: Run the suite.**

```bash
.venv/bin/python -m pytest tests/ -x --ignore=tests/test_e2e.py -q 2>&1 | tail -15
```

Expected: All pass.

- [ ] **Step 6: Commit.**

```bash
git add app.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "refactor: rip out grace-period machinery from app.py

Removes _mark_grace_for_session, _bump_session_last_poll, the 'grace'
key on session dicts, and the MAX_CONCURRENT_SESSIONS exclusion at all
4 check sites. Grace was never wired through set_app_hooks in prod, so
this removes dead code."
```

---

## Task 9: Update MCP `instructions` string + check `mcp_asgi.py` cleanup

The FastMCP `instructions` string at `mcp_server.py:61-66` currently tells callers to "SHARE THE LIVE URL" and "watch progress". With replay-only semantics, that text is wrong.

**Files:**
- Modify: `coda_mcp/mcp_server.py`
- Spot-check: `coda_mcp/mcp_asgi.py`

- [ ] **Step 1: Locate the instructions string.**

```bash
grep -n "SHARE THE LIVE URL\|watch progress\|live URL" coda_mcp/mcp_server.py
```

Expected: matches near the `FastMCP(...)` instantiation block (around lines 61-66).

- [ ] **Step 2: Rewrite the relevant paragraph.**

In `coda_mcp/mcp_server.py`, find the paragraph that starts "SHARE THE LIVE URL" (or whatever the exact phrasing is at lines 61-66). Replace it with:

```
SHARE THE REPLAY URL: After calling coda_run, you receive a ``viewer_url``
in the response. Pass this URL to your user so they can open it in a browser
to review the agent's transcript — what was prompted, what was reasoned, what
was produced. The URL is read-only and serves a static replay of the session,
so it remains valid indefinitely after the task completes.
```

(Exact wording may need adjustment to match the surrounding paragraph style — read the surrounding text first.)

- [ ] **Step 3: Spot-check `mcp_asgi.py`.**

```bash
grep -n "set_app_hooks\|grace\|mark_grace\|bump_poll" coda_mcp/mcp_asgi.py
```

Expected: a `set_app_hooks(...)` call exists but does **not** pass grace-related kwargs (per critic's finding). No changes needed. If grace kwargs ARE passed (shouldn't be, but verify), remove them.

- [ ] **Step 4: Verify nothing relies on the old text.**

```bash
grep -rn "watch progress\|live URL\|LIVE URL" docs/ tests/ static/
```

Expected: matches only in historical documents (specs/plans from prior PRs). No live code depends on the old phrasing.

- [ ] **Step 5: Run the suite.**

```bash
.venv/bin/python -m pytest tests/ -x --ignore=tests/test_e2e.py -q 2>&1 | tail -15
```

Expected: All pass.

- [ ] **Step 6: Commit.**

```bash
git add coda_mcp/mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "docs: update MCP instructions string for replay-only viewer_url semantics

The viewer_url returned by coda_run is no longer a live attach — it is
a static replay. Update the FastMCP instructions text accordingly so
MCP clients describe it correctly to end users."
```

---

## Task 10: Update / rewrite `test_replay_attach.py` for the new contract

After the rip-out, `test_replay_attach.py` may pass without changes (the helper extraction and replay-only flag don't break its existing assertions). But the two tests in it should now make the stronger assertion: replay works regardless of PTY state, not just after the PTY has exited.

**Files:**
- Modify: `tests/test_replay_attach.py`

- [ ] **Step 1: Read the current contents.**

```bash
cat tests/test_replay_attach.py
```

- [ ] **Step 2: Run the file as-is to confirm green starting point.**

```bash
.venv/bin/python -m pytest tests/test_replay_attach.py -v 2>&1 | tail -10
```

Expected: 2 passed.

- [ ] **Step 3: Strengthen the assertions.**

The existing tests likely create a transcript file and an exited PTY, then assert that attach returns replay. Add a third test that uses a `replay_only=True` PTY which is STILL ALIVE and asserts the same — confirming the new short-circuit.

**Important:** This test allocates a real PTY (via `mcp_create_pty_session`), so it needs the same `_pty_skip` guard pattern used in `tests/test_replay_only_flag.py`. Add the guard at the top of the file if it isn't there already (next to the existing imports).

At the top of `tests/test_replay_attach.py`, if not already present, add:

```python
import os as _os
import pytest as _pytest

try:
    import pty as _pty
    _master, _slave = _pty.openpty()
    _os.close(_master)
    _os.close(_slave)
    _PTY_AVAILABLE = True
except Exception:
    _PTY_AVAILABLE = False

_pty_skip = _pytest.mark.skipif(
    not _PTY_AVAILABLE,
    reason="PTY not allocatable in this environment",
)
```

Then add to the end of `tests/test_replay_attach.py`:

```python
@_pty_skip
def test_attach_session_returns_replay_for_alive_replay_only_pty(tmp_path, monkeypatch):
    """A coda_run-style PTY (replay_only=True) that is still alive serves the transcript.

    This is the new contract introduced by the replay-only flag — historically
    a live PTY would serve its output_buffer.
    """
    import os
    from app import app as flask_app, mcp_create_pty_session, mcp_close_pty_session
    from coda_mcp import task_manager

    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))

    sid = mcp_create_pty_session(label="replay-alive", replay_only=True)
    try:
        sess_id = "sess-x"
        task_id = "task-x"
        sdir = tmp_path / sess_id
        tdir = sdir / "tasks" / task_id
        tdir.mkdir(parents=True)
        (sdir / "session.json").write_text(
            '{"session_id": "%s", "pty_session_id": "%s"}' % (sess_id, sid)
        )
        (tdir / "transcript.log").write_bytes(b"FROM DISK")
        # Cache may have stale entries from earlier tests — clear before the lookup.
        task_manager._pty_lookup_cache.clear()

        client = flask_app.test_client()
        resp = client.post("/api/session/attach", json={"session_id": sid})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["replay"] is True
        assert body["output"] == ["FROM DISK"]
    finally:
        mcp_close_pty_session(sid)
```

- [ ] **Step 4: Run.**

```bash
.venv/bin/python -m pytest tests/test_replay_attach.py -v 2>&1 | tail -10
```

Expected: 3 passed.

- [ ] **Step 5: Commit.**

```bash
git add tests/test_replay_attach.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "test: extend test_replay_attach.py for alive-PTY replay_only case

Confirms the new contract: replay-only sessions always serve the
transcript-from-disk, even when the PTY is still alive."
```

---

## Task 11: Add regression-guard test

Prevent future drift that accidentally re-introduces `grace` on the `coda_run` path.

**Files:**
- Modify: `tests/test_replay_only_flag.py`

- [ ] **Step 1: Append the regression test.**

Append to `tests/test_replay_only_flag.py`:

```python
@_pty_skip
def test_no_grace_key_in_coda_run_session_dict():
    """Regression guard: coda_run-created PTYs must not have a 'grace' key,
    and mcp_create_pty_session must not accept a 'grace' kwarg.

    Protects against accidental re-introduction of grace-period machinery
    in future changes.
    """
    import inspect
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    # The function signature must not include 'grace'.
    sig = inspect.signature(mcp_create_pty_session)
    assert "grace" not in sig.parameters, (
        f"mcp_create_pty_session should not accept a 'grace' parameter "
        f"(found in signature: {list(sig.parameters)})"
    )

    # And the session dict must not contain a 'grace' key.
    sid = mcp_create_pty_session(label="t-no-grace", replay_only=True)
    try:
        assert "grace" not in sessions[sid], (
            f"session dict should not contain a 'grace' key "
            f"(found: {list(sessions[sid].keys())})"
        )
    finally:
        mcp_close_pty_session(sid)
```

- [ ] **Step 2: Run.**

```bash
.venv/bin/python -m pytest tests/test_replay_only_flag.py -v 2>&1 | tail -15
```

Expected: 7 passed (the previous 6 + this regression-guard).

- [ ] **Step 3: Run the full suite one final time.**

```bash
.venv/bin/python -m pytest tests/ -x --ignore=tests/test_e2e.py -q 2>&1 | tail -15
```

Expected: Around 525 passed + ~11 skipped (PTY-gated). Net change from baseline: -2 tests.

- [ ] **Step 4: Commit.**

```bash
git add tests/test_replay_only_flag.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" commit -m "test: regression guard against re-introduction of grace key

Asserts mcp_create_pty_session does not accept a 'grace' kwarg and that
coda_run-created session dicts contain no 'grace' key. Catches drift
if a future change tries to bring the grace machinery back."
```

---

## Final verification (post-task)

- [ ] **F1: Full suite green.**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_e2e.py -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **F2: `grep` confirms no stale references.**

```bash
grep -rn "grace\|GRACE_PERIOD\|_mark_grace\|_bump_session_last_poll\|_schedule_deferred_close" coda_mcp/ app.py 2>&1 | grep -v ".pyc\|.git"
```

Expected: no matches (or only matches in comments that document the removal — those are fine).

- [ ] **F3: Manual smoke (optional, requires deployed environment).**

1. Restart the app (`uvicorn coda_mcp.mcp_asgi:app`).
2. Trigger a `coda_run` from an MCP client. Capture the `viewer_url`.
3. Open the URL in a browser **while** hermes is still running. Confirm: read-only replay UI, no terminal input box.
4. Wait for hermes to complete (~30s). Confirm: PTY is gone from `/health` (`active_sessions` returns to baseline).
5. Re-open the URL. Confirm: same read-only replay, full final transcript.

---

## Self-review checklist (run on completed plan)

1. **Spec coverage** ✓
   - Section "Add replay_only flag" → Task 1
   - Section "Enforce replay-only" → Tasks 2 (extract) + 3 (enforce)
   - Section "Wire coda_run" → Task 4
   - Section "Rip out grace machinery" → Tasks 6 (tests) + 7 (mcp_server.py) + 8 (app.py)
   - Section "Watcher teardown on completion" → Task 5
   - "Docstrings to update" → Tasks 7 (docstring inside) + 9 (MCP instructions)
   - "Regression guard" → Task 11

2. **Placeholders** ✓ — every step has concrete code/commands. No TBDs.

3. **Type consistency** ✓
   - `replay_only: bool = False` used identically in signature, dict, and tests
   - `_close_pty_immediately(session_id: str) -> None` — task-manager session_id, not pty_session_id (the function takes the task session ID and looks up the PTY internally)
   - `_serve_transcript_replay(session_id)` — pty_session_id (passed straight through to `find_task_dir_by_pty_session`)

4. **Ordering safety** ✓
   - Tests dropped (Task 6) BEFORE code rip-out (Tasks 7, 8) → suite stays green
   - `_watch_task` swap (Task 5) BEFORE `_schedule_deferred_close` deletion (Task 7) → no orphan calls
   - `replay_only` storage (Task 1) BEFORE attach short-circuit (Task 3) → flag exists before being read

---

## Plan critique gate

**Cleared** (2026-05-28). Critic verdict: APPROVE WITH CHANGES. Issues found and resolved:

1. **CRITICAL — locking deadlock in Task 8 Step 2.** Original instruction wrapped the replacement code in `with sessions_lock:`, but all 4 MAX_CONCURRENT sites are already inside `with sessions_lock:` blocks. `sessions_lock` is a non-reentrant `threading.Lock()`, so the wrap would deadlock the server. Fixed: Task 8 Step 2 now explicitly says "do NOT wrap" and replaces the code with bare `active = len(sessions)`.

2. **MAJOR — TDD violation in Task 5.** Original task tried to wrap the `_watch_task` swap in a red-green cycle, but the test ended up passing on first run (it called `_close_pty_immediately` directly, not through `_watch_task`). Fixed: Task 5 relabeled as a non-TDD refactor with existing integration tests as the safety net, in the same style as Task 2.

3. **MAJOR — missing `_pty_skip` in Task 10 test.** New test in `test_replay_attach.py` allocates a real PTY but didn't carry the PTY-skip guard, so it would error on CI environments without `pty.openpty()`. Fixed: Task 10 now adds the guard pattern at the file top and decorates the new test with `@_pty_skip`.

4. **MINOR — vague test names in Task 6 Step 1.** Original named 2 of 4 grace tests to delete and said "plus two more". Fixed: all 4 tests now named explicitly in a table.

Per-dimension verdicts from the critic:
- **Spec coverage**: Complete (all spec sections map to ≥1 task)
- **Task atomicity & ordering**: Sound — green at every commit boundary
- **TDD discipline**: Clean after Task 5 relabel (Tasks 1, 3, 4, 11 do genuine red-green; Tasks 2, 5 are pure refactors with safety-net tests)
- **Line-number accuracy**: Verified exact at every reference (no drift)
- **Test-code correctness**: All fixtures/imports/decorators verified after fixes
- **Concurrency**: Safe after Task 8 lock-wrap fix
- **Commit messages**: Conventional-commits format with `-c user.email=datasciencemonkey@gmail.com` override — correct

Plan is ready for execution.
