# CoDA MCP Live Session URL — Design

**Date:** 2026-05-27
**Branch:** `feat/coda-mcp-server`
**Status:** Spec approved by user; ready for implementation plan
**Related PR:** databrickslabs/coding-agents-databricks-apps#64 (parent feature)

## 1. Problem

`coda_run` is fire-and-forget today: it returns `{task_id, session_id, status: "running"}` and the calling MCP client (Genie Code, Claude Desktop, Cursor) has no way to surface progress to the user. The user only sees a structured `result.json` after the task completes via `coda_inbox`/`coda_get_result`. Status messages from `status.jsonl` are coarse-grained. There is no way to watch hermes execute live, intervene mid-task, or reconstruct what happened after the fact.

The Flask app side already has a fully working real-time terminal UI (xterm.js + Socket.IO + HTTP polling fallback) that knows how to attach to any active PTY by id. The MCP server already spawns those PTYs to run hermes. **The two halves are not connected by a URL.**

## 2. Goal

Give every `coda_run` (and existing tasks listed via `coda_inbox` / fetched via `coda_get_result`) a `viewer_url` that:

- **During execution** — opens the existing terminal UI attached to that task's live PTY. The user can watch hermes work in real time and type into the session if they want to redirect or take over (single-user app; this is intentional).
- **For ~5 minutes after completion** — keeps the PTY alive so a viewer who joined mid-task isn't yanked the instant `result.json` is written. Heartbeats from an active viewer do not extend this window — the grace timer is fixed.
- **Indefinitely after PTY closes** (within the 24h `TASK_TTL_S`) — serves a static "replay" rendering of the captured terminal transcript so a user can scroll the full execution history from `coda_inbox`.

Out of scope (deferred to separate specs): configurable agent selection (hermes vs claude-code vs codex), multi-user attribution, asciinema-style timed replay.

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  MCP client                       Browser                       │
│  (Genie Code, Claude Desktop)     (single user, app URL)        │
└──────────┬──────────────────────────────────┬──────────────────┘
           │ tools/call coda_run              │ GET /?session=<id>
           ▼                                  ▼
   ┌───────────────┐               ┌─────────────────────┐
   │ coda_mcp /mcp │               │ Flask /static + WS  │
   │  +viewer_url  │               │  /api/session/attach│
   └───────┬───────┘               └──────────┬──────────┘
           │                                  │
           ▼                                  ▼
   ┌──────────────────────────────────────────────────────┐
   │  Flask app (single process)                          │
   │   sessions[<pty_id>] → {fd, buffer, transcript_fh,   │
   │                          grace: bool}                │
   │   read_pty_output thread:                            │
   │     fd → buffer  →  socketio emit (room=<pty_id>)    │
   │     fd → transcript.log  (NEW: tee, flush per write) │
   └──────────────────────────────────────────────────────┘
           │                                  │
           │ writes (chmod 600)               │ reads when PTY gone
           ▼                                  ▼
   ~/.coda/sessions/{sess}/tasks/{task}/transcript.log
```

Everything between the MCP server and the Flask app already exists. The feature is mostly plumbing:

1. **Tee PTY output** to `transcript.log` (on disk, per task, chmod 0600, 10 MB soft cap).
2. **Defer PTY close** on task completion by 5 minutes (`threading.Timer`) so live viewers can finish reading.
3. **Build `viewer_url`** in MCP tool responses by capturing `X-Forwarded-Host` from the inbound request.
4. **Teach the SPA** to read `?session=` on load and to render replay mode when the PTY is gone but a transcript exists.

## 4. Components

### 4.1 `app.py::sessions[pty_id]` dict (additive)

Four new keys, all optional/defaulting:

- `transcript_path: str | None` — absolute path to the tee target.
- `transcript_fh: BinaryIO | None` — open file handle owned by `read_pty_output`.
- `transcript_bytes: int` (default 0) — running count to enforce the 10 MB cap.
- `grace: bool` (default False) — set `True` when `_watch_task` schedules deferred close. Used by the concurrency check to exempt this slot.

No removals. No semantic changes to existing keys.

### 4.2 `app.py::mcp_create_pty_session(label, transcript_path=None)`

New optional kwarg. When provided:

- `os.makedirs(os.path.dirname(transcript_path), exist_ok=True)`
- Open file: `fh = open(transcript_path, "ab", buffering=0)` (binary append, unbuffered)
- `os.fchmod(fh.fileno(), 0o600)` immediately
- Store `transcript_path` and `transcript_fh` on the session dict
- If open fails: log error, set both to `None`, continue (live PTY still works)

### 4.3 `app.py::read_pty_output` (additive)

After the existing buffer append and Socket.IO emit, if a transcript handle is present, write under the per-session lock to prevent races against `terminate_session` (which may close the handle from the Timer thread):

```python
with session_lock:
    fh = session.get("transcript_fh")
    written = session.get("transcript_bytes", 0)
    if fh is not None:
        remaining = TRANSCRIPT_CAP_BYTES - written
        if remaining > 0:
            chunk = output[:remaining]
            try:
                fh.write(chunk)
                fh.flush()
                session["transcript_bytes"] = written + len(chunk)
                if len(chunk) < len(output):
                    fh.write(b"\n[transcript truncated at 10MB]\n")
                    fh.flush()
                    fh.close()
                    session["transcript_fh"] = None
            except (OSError, ValueError) as exc:
                logger.warning("transcript write failed for %s: %s", session_id, exc)
                try: fh.close()
                except Exception: pass
                session["transcript_fh"] = None
```

`TRANSCRIPT_CAP_BYTES = 10 * 1024 * 1024`.

**Invariants** (documented for future maintainers):

- `transcript_fh` is opened in `mcp_create_pty_session`, written exclusively by `read_pty_output`, and closed by either (a) `read_pty_output` on cap/error or (b) `terminate_session` on PTY teardown. All three sites operate under `session["lock"]`.
- `transcript_bytes` is incremented only by `read_pty_output`. Single-writer; reads from other threads must hold `session["lock"]`.
- `ValueError` is caught alongside `OSError` to defend against a tiny window where `terminate_session` closes the handle between the spec's `if fh is not None` check and the actual `fh.write` call — the lock prevents this, but the catch is belt-and-suspenders.

### 4.4 `app.py::terminate_session` (additive)

Close the transcript file handle under the per-session lock before the existing fd close. The swap-to-`None` is the synchronization point that lets `read_pty_output` notice the handle is gone on its next iteration:

```python
sess = sessions.get(session_id)
if sess is not None:
    with sess["lock"]:
        fh = sess.get("transcript_fh")
        sess["transcript_fh"] = None  # swap first, then close
    if fh is not None:
        try: fh.close()
        except Exception: pass
```

(The actual close happens outside the lock to avoid holding it across a potential blocking I/O on a slow filesystem.)

### 4.5 `app.py::MAX_CONCURRENT_SESSIONS` check (modified)

At the `if len(sessions) >= MAX_CONCURRENT_SESSIONS` checkpoints in `create_session()` and `mcp_create_pty_session()`, replace the raw length check with a filtered count that excludes grace-period PTYs:

```python
active = sum(1 for s in sessions.values() if not s.get("grace"))
if active >= MAX_CONCURRENT_SESSIONS: ...
```

`cleanup_stale_sessions` itself is **unchanged** — it still treats grace-period PTYs like any other session, but the 24h `SESSION_TIMEOUT_SECONDS` is so long the reaper never wins the race against the 5-min Timer.

`MAX_CONCURRENT_SESSIONS` default stays at 5.

### 4.6 `coda_mcp/mcp_server.py::_watch_task` (modified)

Both completion and timeout paths replace immediate `_close_pty_for_session(session_id)` with:

```python
session_data = task_manager._read_session(session_id)
pty_session_id = session_data.get("pty_session_id")
if pty_session_id and _app_close_session is not None:
    _mark_grace(pty_session_id)   # sets sessions[pty_id]["grace"] = True
    _bump_last_poll(pty_session_id, GRACE_PERIOD_S)  # defensive against reaper
    threading.Timer(
        GRACE_PERIOD_S,
        _app_close_session,
        args=(pty_session_id,),
    ).start()
```

`GRACE_PERIOD_S = 300` (5 minutes), defined as a module constant for testability. `_mark_grace` and `_bump_last_poll` are two new hook callbacks wired through `set_app_hooks()` alongside the existing three — consistent with the current pattern (no direct Flask imports from the MCP module).

The Timer must be a daemon so it doesn't block uvicorn shutdown: `t = threading.Timer(...); t.daemon = True; t.start()`.

### 4.7 `coda_mcp/mcp_server.py::coda_run` (additive)

After `mcp_create_pty_session`, compute the transcript path and pass it in:

```python
transcript_path = os.path.join(
    task_manager._task_dir(session_id, task_id),
    "transcript.log",
)
pty_session_id = _app_create_session(
    label="hermes-mcp",
    transcript_path=transcript_path,
)
```

(Note: `_app_create_session` signature gains the kwarg. The implementation in `app.py` already documented above.)

Then build the response with the new field:

```python
return json.dumps({
    "task_id": task_id,
    "session_id": session_id,
    "status": "running",
    "viewer_url": _build_viewer_url(pty_session_id),  # may be None
})
```

Tools serialize via `json.dumps` so `None` becomes `null`. Clients that don't recognize the field will ignore it.

### 4.8 `coda_mcp/url_builder.py` (new tiny module)

```python
import os
from typing import Optional

_app_url_cache: Optional[str] = None

def capture_from_headers(host: Optional[str]) -> None:
    """Called by middleware on every inbound request."""
    global _app_url_cache
    if host:
        _app_url_cache = host

def build_viewer_url(pty_session_id: str) -> Optional[str]:
    override = os.environ.get("CODA_APP_URL", "").strip()
    if override:
        base = override.rstrip("/")
    elif _app_url_cache:
        base = f"https://{_app_url_cache}"
    else:
        return None
    return f"{base}/?session={pty_session_id}"
```

### 4.9 `coda_mcp/mcp_asgi.py` (additive middleware)

Insert a small ASGI middleware on `mcp_starlette` (via `mcp_starlette.add_middleware(...)`) that extracts `X-Forwarded-Host` (fallback: `Host`) from every HTTP request and calls `url_builder.capture_from_headers(host)`. Both MCP requests AND inbound browser HTTP requests refresh the cache.

**Coverage caveat** (not a problem in practice): the top-level ASGI app is `socketio.ASGIApp(sio, other_asgi_app=mcp_starlette)`, so `/socket.io/` traffic is intercepted by socketio *before* it reaches `mcp_starlette` and therefore never hits this middleware. This is fine because (a) the user always loads the SPA via plain HTTP first (which refreshes the cache), and (b) every `coda_run` MCP call is a plain HTTP POST to `/mcp` (also through the middleware). The cache is hot by the time any tool needs the URL.

```python
class AppUrlCaptureMiddleware:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"x-forwarded-host") or headers.get(b"host")
            if host:
                url_builder.capture_from_headers(host.decode())
        await self.app(scope, receive, send)
```

### 4.10 `coda_mcp/task_manager.py::find_task_dir_by_pty_session` (new)

```python
_pty_lookup_cache: dict[str, tuple[str, float]] = {}  # pty_id -> (task_dir, ts)
_PTY_LOOKUP_TTL = 60.0  # seconds

def find_task_dir_by_pty_session(pty_session_id: str) -> str | None:
    """Find the task dir whose session.json carries this pty_session_id."""
    now = time.time()
    cached = _pty_lookup_cache.get(pty_session_id)
    if cached and (now - cached[1]) < _PTY_LOOKUP_TTL:
        return cached[0]
    # Scan SESSIONS_DIR
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
        # The session has a current_task or completed_tasks; pick the most recent.
        candidate = data.get("current_task") or (
            data["completed_tasks"][-1] if data.get("completed_tasks") else None
        )
        if candidate:
            tdir = os.path.join(SESSIONS_DIR, sess_name, "tasks", candidate)
            _pty_lookup_cache[pty_session_id] = (tdir, now)
            return tdir
    return None
```

TTL handles the rename/close case without manual invalidation.

**Invariant**: CoDA MCP sessions are ephemeral — one task per session (see `task_manager.create_session` then `complete_task` which sets `current_task=None` and appends to `completed_tasks`). This function therefore returns the right task dir for the lifetime of the URL. If the lifecycle ever changes to allow task reuse within a single session, this function must be revisited to pick the *active or grace-period* task rather than `completed_tasks[-1]`.

### 4.11 `app.py::attach_session` endpoint (additive)

After the existing `_get_session()` lookup, add a fallback:

```python
sess = _get_session(session_id)
if not sess or sess.get("exited"):
    # NEW: try transcript replay
    tdir = task_manager.find_task_dir_by_pty_session(session_id)
    if tdir:
        transcript = os.path.join(tdir, "transcript.log")
        if os.path.isfile(transcript):
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
    return jsonify({"error": "Session not found or exited"}), 404
```

The response shape (`output: [str]`, `replay: true|absent`, plus existing keys) is **NOT** consumed by the existing `_doAttach` — that function deliberately ignores `data.output` and forces a SIGWINCH redraw of the live application (`static/index.html:1339-1357`, comment at line 1347: "We skip buffer replay because it contains raw escape sequences that produce garbled output"). The replay-mode response is consumed by a new SPA function `_doReplay` described in §4.12, which writes the bytes directly into xterm.

### 4.12 `static/index.html` (~50-70 LoC)

Four additions:

1. **Boot-time URL parse** — before the existing session-picker fetch, check `new URLSearchParams(location.search).get("session")`. If absent → existing flow. If present → call `POST /api/session/attach` once and branch on the response:
    - 200 with `replay: true` → call **`_doReplay`** (new, described below). Skip `_doAttach`. Do NOT emit `join_session`. Do NOT wire `terminal_input` to the WS.
    - 200 without `replay` → call the existing `_doAttach(term, sessionId)` and the existing `socket.emit('join_session', { session_id })` path. (Reusing `_doAttach` is correct here because the *live* PTY is running an interactive app, and SIGWINCH-redraw is the right behavior.)
    - 404 → render a small in-page fallback: "session expired or never existed" + a button to navigate to `/`.

2. **`_doReplay(term, sessionId, bytes)` — new function** that handles static replay rendering. Cannot route through `_doAttach` because `_doAttach` discards `data.output` (it relies on a running app to redraw via SIGWINCH; replay mode has no running app). Implementation:

    ```js
    async function _doReplay(term, sessionId, content) {
      // Chunk the write to avoid main-thread jank on multi-MB transcripts.
      // xterm.js write() is internally batched, but a single 10MB call
      // still blocks until the parser drains. 64KB slices with rAF gives
      // the browser a chance to repaint between chunks.
      const CHUNK = 64 * 1024;
      for (let i = 0; i < content.length; i += CHUNK) {
        term.write(content.slice(i, i + CHUNK));
        await new Promise(r => requestAnimationFrame(r));
      }
      // Mount a small "Task completed — viewing replay" banner above the pane.
      // No input handler, no WS subscription, no heartbeat for this session id.
    }
    ```

3. **Replay-mode pane behavior** — the tab gets a "(replay)" badge. The xterm input handler is not wired. The session is NOT included in the heartbeat session_ids list (the PTY is dead; heartbeats would 404 the lookup).

4. **History/URL hygiene** — when the user closes a pane that was opened via `?session=`, call `history.replaceState({}, '', '/')` so a refresh doesn't re-attach.

**Estimate revised**: 50-70 LoC including the new `_doReplay` and the 404 fallback. Architecturally the most "real" change in the spec — the rest of the codebase shifts are mostly additive.

### 4.13 MCP tool `instructions` update (`coda_mcp/mcp_server.py`)

Append one paragraph to the existing `instructions` block on the FastMCP instance:

> SHARE THE LIVE URL: When `coda_run` returns a `viewer_url` field, mention it to the user in plain text (e.g. "you can watch progress at <url>"). The URL is safe to share — it points to the same Databricks App the user is already authenticated against. Do this on the FIRST mention of the task and any time the user asks where the task is or how to see it.

## 5. Data flow

### 5.1 Submit

`MCP client → /mcp coda_run → task_manager.create_session → mcp_create_pty_session(transcript_path) → task_manager.create_task → mcp_send_input("hermes -z ...") → _watch_task thread spawned → return {task_id, session_id, status: "running", viewer_url}`.

### 5.2 Live view

`Browser → GET /?session=<pty_id> → SPA reads ?session → POST /api/session/attach → live output buffer returned → WS join_session → live stream from read_pty_output → terminal_input writes to fd → heartbeat keeps the (already non-grace) PTY alive`.

### 5.3 Grace window

At T+0 hermes writes `result.json`. `_watch_task` calls `task_manager.complete_task` (disk status → closed), marks the PTY `grace=True`, bumps `last_poll_time`, schedules `Timer(300, _app_close_session)`. A viewer present at T+0 keeps streaming for up to 5 min. At T+300 the Timer SIGHUPs bash, `read_pty_output` sees EOF, flushes and closes the transcript handle, removes the session entry.

### 5.4 Replay

`Browser → GET /?session=<pty_id> → POST /api/session/attach → PTY not found → find_task_dir_by_pty_session → read transcript.log → return {output: [bytes], replay: true} → SPA renders bytes, no WS subscription`.

## 6. Error handling

| Failure | Behavior |
|---|---|
| `CODA_APP_URL` and `X-Forwarded-Host` both absent | `viewer_url: null`. One startup WARN. |
| Transcript open fails | `transcript_fh = None`. Live PTY works; replay disabled. |
| Transcript write fails mid-stream | Log once per session, close handle, set `transcript_fh = None`, keep reading PTY. |
| 10 MB cap hit | Write marker, close handle, set `transcript_fh = None`. PTY keeps streaming live (no further teeing). |
| Timer fires after manual close | `terminate_session` is re-entrant; `sessions.pop(_, None)` and `os.kill` wrapped in try/except. No-op. |
| uvicorn restart during grace | In-memory state lost; old `viewer_url` falls through to transcript replay (if file exists) or 404. Acceptable. |
| Browser opens URL mid-grace, grace expires while connected | `read_pty_output` emits `session_exited` to the room. SPA shows "session ended" banner. User reloads → replay mode. |
| Browser opens URL after grace AND transcript reaped | 404. SPA shows expired page. |
| `MAX_CONCURRENT_TASKS` reached | Unchanged "concurrency limit" error. Grace PTYs don't count toward this (disk status = closed). |
| `MAX_CONCURRENT_SESSIONS` reached among active (non-grace) | Existing 429. Grace PTYs don't count. |
| Hermes hangs (no `result.json`) | Existing `_watch_task` timeout path now also defers close via the same Timer mechanism. |

## 7. Testing

### 7.1 Unit

- `coda_mcp/url_builder.py`: env override beats header capture; `None` when both absent; trailing slash on override is stripped.
- `coda_run` returns `viewer_url` only when builder returns non-None; same for `coda_inbox` per-entry and `coda_get_result`.
- `find_task_dir_by_pty_session`: hit, miss, TTL expiry, ignores corrupt session.json.
- `_watch_task`: schedules `Timer` (mocked) with correct args on both completion and timeout paths; never calls `_app_close_session` synchronously.
- `_mark_grace` / `_bump_last_poll` set the session dict fields.

### 7.2 Integration (`tests/test_mcp_integration.py`)

- E2E with a stub hermes (`bash -c 'echo hello; touch results/result.json; echo done'`):
  - `transcript.log` contains "hello".
  - At T+1s, PTY still alive (grace).
  - At T+(GRACE+1)s (test uses a 2s grace via patched constant), PTY closed; transcript file persists.
  - `/api/session/attach` returns `replay: true` after close; live mode before.
- Concurrency: submit `MAX_CONCURRENT_TASKS` tasks, complete them all (grace begins), submit `MAX_CONCURRENT_TASKS` more — all succeed (grace PTYs don't block).
- 10 MB cap: feed a hermes stub that prints `>10MB` of output; transcript file is exactly `10MB + marker`; PTY keeps running.

### 7.3 SPA

- New `tests/test_frontend_deeplink.spec.js` (Playwright if available; else manual checklist):
  - `/?session=<live_id>` → live attach, WS room joined, terminal renders.
  - `/?session=<replay_id>` → replay rendered, no WS join, banner visible.
  - `/?session=<bogus_id>` → expired page.
  - Closing the pane drops `?session=` from `history`.

### 7.4 Manual smoke

- Deploy to `mcp-test-coda` app, connect Genie Code, run a `coda_run`, click `viewer_url` from the chat response, confirm live stream + grace + replay.
- `chmod 600` check: `ls -la ~/.coda/sessions/*/tasks/*/transcript.log` on deployed pod.
- Confirm `viewer_url` absent on a local uvicorn boot without `CODA_APP_URL` and no inbound request yet.

## 8. Open questions (resolved)

- ~~Read-only vs interactive viewer?~~ → Interactive (full terminal).
- ~~Grace period mechanism?~~ → `threading.Timer(300, _close)`.
- ~~Replay storage?~~ → Tee to `transcript.log`.
- ~~Configurable agent?~~ → Deferred to a separate spec.
- ~~Base URL resolution?~~ → `CODA_APP_URL` env override → `X-Forwarded-Host` capture (officially provided by Databricks Apps).
- ~~Concurrency under grace?~~ → Exempt grace PTYs from `MAX_CONCURRENT_SESSIONS`. Cap stays at 5.

## 9. Risks accepted

- **Transcript on disk contains secrets** if hermes prints them. Single-user app, file is mode 0600, cleaned with the rest of the session at 24h TTL. Documented in `docs/mcp-v2-background-execution.md`.
- **5 min grace + 0 second active task** means a viewer who opens the URL late may still race the close. Acceptable; replay mode covers them.
- **Browser tabs can interact with the same PTY simultaneously.** Already true for the existing terminal UI; no new exposure.

## 10. Surface summary

| Surface | LoC est | Risk |
|---|---|---|
| `app.py` (4 functions touched) | ~60 | Low — additive, no semantic shifts |
| `coda_mcp/mcp_server.py` (2 functions + instructions) | ~40 | Low |
| `coda_mcp/url_builder.py` (new) | ~25 | Low |
| `coda_mcp/mcp_asgi.py` (middleware) | ~15 | Low |
| `coda_mcp/task_manager.py` (new lookup) | ~30 | Low |
| `static/index.html` | ~50-70 | Medium — new boot branch + new `_doReplay` rendering path; live attach still reuses `_doAttach` |
| Tests | ~250 | — |

**Total**: ~235-255 LoC of production code + ~250 LoC of tests.

## 11. Next step

Hand to `writing-plans` skill to produce an executable implementation plan with task ordering, dependencies, and verification gates.
