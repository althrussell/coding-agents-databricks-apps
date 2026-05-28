# Spec: `coda_run` Returns Replay-Only URL

**Status:** Draft, pre-critique-gate
**Date:** 2026-05-28
**Branch:** `coda-mcp`
**Related:** PR #66 (introduced the live-attach `viewer_url` we are now narrowing) ; `docs/superpowers/specs/2026-05-27-coda-mcp-live-session-url-design.md` (predecessor design)

## Goal

Make `coda_run`'s returned `viewer_url` resolve to a **read-only static replay** of the agent's transcript, never to a live PTY attach. As a consequence, drop the 5-minute "grace period" machinery from the `coda_run` execution path entirely — the PTY session can be torn down immediately when `hermes -z` exits.

## Why

PR #66 introduced a dual-purpose `viewer_url` on `coda_run`: live attach during a 5-minute grace window, then static replay after that. The dual mode was sized for "human watches hermes run live, then post-mortem replays the same URL".

That use case is being split out into a **separate** MCP tool, `coda_interactive` (designed in a follow-up spec). `coda_run` is now exclusively the fire-and-forget batch surface — autonomous execution, post-hoc inspection. The live-attach affordance on its returned URL is no longer useful: by the time most callers' humans click the URL, hermes has already exited; what they get is a dead bash shell, not a live agent.

## The Three-Mode Framework

This spec settles the contract by enumerating the three ways CoDA sessions get created:

The existing PTY lifecycle in `app.py` (`SESSION_TIMEOUT_SECONDS = 86400`, `CLEANUP_INTERVAL_SECONDS = 900`) **already gives sessions a 24h idle TTL** with WS-heartbeat extension. Mode 2 inherits this directly; only Mode 3 needs to deviate (faster teardown).

| Mode | How invoked | PTY tag | Pre-attach lifecycle | Post-attach lifecycle | Teardown trigger | URL semantics |
|---|---|---|---|---|---|---|
| **1. Direct launch** | User opens web UI, creates a tab | (none) | n/a — user starts attached | 24h idle cleanup; WS heartbeat extends indefinitely | Tab close / disconnect + 24h idle | No external URL |
| **2. `coda_interactive`** (Todo 2, not in this spec) | MCP client fires the tool, passes URL to a human | `replay_only=False` | Same 24h idle cleanup as Mode 1 | Same — WS heartbeat extends | Agent process exit (`exit` / `/quit` / Ctrl-D), 24h idle, or user closes tab + 24h idle | Live attach; fallback to replay if PTY gone |
| **3. `coda_run`** *(this spec)* | MCP client fires the tool, URL is for post-hoc review only | `replay_only=True` | n/a — no live attach exists | n/a | Hermes -z process exit → `result.json` appears → immediate teardown (bypasses 24h idle) | Replay only, always |

This spec finalizes Mode 3 and embeds Mode 2 as a forward-reference so the critique gate can sanity-check both together. Mode 1 is the existing direct-launch path — no changes; Mode 2 inherits its lifecycle wholesale.

## Design

### 1. Add `replay_only` flag to PTY sessions

In `app.py`'s `mcp_create_pty_session(label, transcript_path=None)`, add a third parameter:

```python
def mcp_create_pty_session(
    label: str = "hermes-mcp",
    transcript_path: str | None = None,
    replay_only: bool = False,
) -> str:
    ...
    sessions[session_id] = {
        ...
        "replay_only": replay_only,
        ...
    }
```

Default is `False` so existing callers (direct-launch via `create_session`, future `coda_interactive`) keep their current behavior.

### 2. Enforce replay-only in the attach endpoint

In `app.py`'s `attach_session()` route, **before** the live-attach branch runs, check the flag. If `sess.get("replay_only")` is true, serve the transcript regardless of whether the PTY is still alive:

```python
def attach_session():
    ...
    sess = _get_session(session_id)

    # NEW: replay-only sessions always serve transcript, never live buffer
    if sess and sess.get("replay_only"):
        return _serve_transcript_replay(session_id)

    # Existing: PTY gone → transcript fallback
    if not sess or sess.get("exited"):
        return _serve_transcript_replay(session_id)
    
    # Existing: live attach
    ...
```

Where `_serve_transcript_replay()` is a helper extracted from the existing transcript-lookup block at `app.py:1170-1188`. The helper takes only the PTY `session_id` — it does not need any fields from the live session dict (`output_buffer`, `pid`, `label`, `created_at`), since the replay path uses `task_manager.find_task_dir_by_pty_session(session_id)` + file I/O on the transcript. Clean extraction, no field synthesis.

If no transcript file exists for the session (rare — e.g., PTY died before any output flushed), the helper returns the existing 404 page.

### 3. Wire `coda_run` to pass `replay_only=True`

In `coda_mcp/mcp_server.py` `coda_run()`:

```python
pty_session_id = _app_create_session(
    label="hermes-mcp",
    transcript_path=transcript_path,
    replay_only=True,   # NEW
)
```

### 4. Rip out the grace-period machinery from the `coda_run` path

**Pre-existing reality check (informational, per critique):** The `mark_grace_fn` and `bump_poll_fn` hooks were *never wired* in production — neither `app.py:1770-1774`'s `set_app_hooks(...)` call nor `mcp_asgi.py:80-84`'s equivalent passes them. At runtime `_app_mark_grace` and `_app_bump_poll` are both `None`, so `_schedule_deferred_close` no-ops through its `if _app_mark_grace is not None:` guard at `mcp_server.py:203`. The Timer fires and the close happens, but the `grace` flag is never set, the `MAX_CONCURRENT_SESSIONS` exclusion never activates. So the rip-out is removing partially dead code — the spec executor should not waste time reproducing or regression-testing grace-period state that never existed in prod.

The following code added in PR #66 is now dead weight for `coda_run` sessions and should be removed:

- `coda_mcp/mcp_server.py`:
  - `GRACE_PERIOD_S = 300` constant
  - `_app_mark_grace` / `_app_bump_poll` hook slots (and the `set_app_hooks` parameters that accept them)
  - `_schedule_deferred_close(session_id)` function
  - The `threading.Timer(GRACE_PERIOD_S, ...)` call in `_watch_task`
- `app.py`:
  - `_mark_grace_for_session(session_id)` function (line ~1515)
  - `_bump_session_last_poll(session_id, delta_s)` function (line ~1530)
  - `grace` key written to the session dict in `mcp_create_pty_session` (line ~1477)
  - The `sum(1 for s in sessions.values() if not s.get("grace"))` exclusion in all 4 `MAX_CONCURRENT_SESSIONS` check sites at `app.py:1329`, `1369`, `1405`, `1456` (revert to simple `len(sessions)` count)
- Docstrings to update:
  - `_close_pty_immediately` at `mcp_server.py:167` currently says "only use from emergency teardown or tests" — rewrite to say it is the normal teardown path for `coda_run`.
  - MCP `instructions` string at `mcp_server.py:61-66` says "SHARE THE LIVE URL" / "watch progress" — rewrite to say "replay URL" / "review what was done."
- Tests:
  - `tests/test_transcript.py`: drop 4 grace-related tests (lines 135, 157, 169, 174)
  - `tests/test_replay_attach.py`: rewrite to assert *immediate* replay regardless of PTY state, not "replay-after-grace"
  - `tests/test_mcp_server.py`: drop 2 grace tests (lines 361, 372 — hooks test + timer-scheduling test)
  - `tests/test_mcp_integration.py`: drop 1 grace test (line 315); the E2E test at `:396` already calls `complete_task` + close directly, keep that pattern

### 5. Watcher teardown on completion

In `_watch_task` (currently spawned by `coda_run`), when the watcher detects `result.json` and marks the task complete, replace the deferred-close path with the immediate one:

```python
# Old:
_schedule_deferred_close(session_id)
# New:
_close_pty_immediately(session_id)
```

`_close_pty_immediately` already exists at `mcp_server.py:167`. It's a thin wrapper that reads `pty_session_id` from task_manager's `session.json` and calls the `_app_close_session(pty_session_id)` hook (`app.py`'s `mcp_close_pty_session`). After the rip-out it becomes the sole teardown path for `coda_run` — update its docstring to reflect that it's now the normal path, not "emergency teardown."

## What does NOT change

- `coda_run`'s **return shape** is unchanged: `{task_id, session_id, status, viewer_url}`. The `viewer_url` string itself is the same format (`{base}/?session={pty}`). The change is purely in what that URL does when followed.
- Transcript writing (the tee in `read_pty_output`) is unchanged.
- The 404-when-no-transcript-found page (`_renderExpiredPage`) is unchanged.
- The frontend (`static/index.html`) `_initFromQueryString`, `_doReplay`, `_doAttach` flow is unchanged. The replay code path already exists and is the one the server will steer all `coda_run` traffic into.
- Direct-launch PTY sessions are unchanged — they keep their existing 24h-idle cleanup (`SESSION_TIMEOUT_SECONDS = 86400`) and WS-heartbeat-extends lifecycle.

## Architecture

```
                     ┌─────────────────────────────────┐
                     │ MCP client calls coda_run       │
                     └────────────────┬────────────────┘
                                      ▼
              ┌──────────────────────────────────────────────┐
              │ task_manager.create_task → write prompt.txt  │
              │ mcp_create_pty_session(replay_only=True)     │
              │ send "hermes -z prompt.txt\n" to PTY         │
              │ spawn _watch_task daemon thread              │
              │ return {viewer_url: ".../?session=..."}      │
              └────────────────┬─────────────────────────────┘
                               ▼
                      (hermes runs in PTY)
                               ▼
              ┌──────────────────────────────────────────────┐
              │ hermes writes result.json → exits            │
              │ _watch_task detects result.json              │
              │ _watch_task calls _close_pty_immediately     │
              │ PTY torn down, slot freed                    │
              └────────────────┬─────────────────────────────┘
                               ▼
   (Human clicks the URL at any time — before/during/after task)
                               ▼
              ┌──────────────────────────────────────────────┐
              │ Frontend POSTs /api/session/attach           │
              │ attach_session() sees sess["replay_only"]    │
              │   OR sess is gone (post-teardown)            │
              │ Returns {replay: true, output: [transcript]} │
              │ Frontend calls _doReplay() — read-only view  │
              └──────────────────────────────────────────────┘
```

## Data flow under different timings

The replay-only contract makes timing irrelevant. Three cases, all converge on the same UX:

1. **Human clicks URL while hermes is still running:**
   PTY exists, `replay_only=True` → server serves the in-progress transcript. Read-only view of partial output.

2. **Human clicks URL right after hermes exits (no grace):**
   `_watch_task` has just called `_close_pty_immediately`. PTY may or may not still be in `sessions`. Either way, `replay_only` is true OR PTY is gone → server serves the final transcript from disk.

3. **Human clicks URL hours / days later:**
   PTY is long gone. Transcript file still on disk. Existing transcript-fallback path serves it.

In none of these cases does the user need a live PTY attached. The transcript file is always sufficient.

## Error handling

- **Transcript file missing / unreadable** (rare — PTY died before flush): existing 404 + `_renderExpiredPage` UI applies. No behavior change.
- **`replay_only` flag on a session that has no `transcript_path`**: should not happen for `coda_run` (we always set transcript_path). If it does, the attach endpoint falls through to the existing 404 path. Defensive — no special handling needed.
- **Race: human clicks URL exactly as `_close_pty_immediately` runs**: both old (PTY still in `sessions`) and new (PTY gone) outcomes resolve to "serve transcript". No race-condition bug.

## Testing

### Modified tests
- `tests/test_replay_attach.py`: rewrite the two existing tests to assert immediate replay on a `replay_only=True` session, regardless of `exited` status. Drop the grace-window scenario.
- `tests/test_transcript.py`: drop the tests that exercised grace-period transitions (~6 of 12).
- `tests/test_mcp_server.py`: drop tests for `_schedule_deferred_close`, `_app_mark_grace`, `_app_bump_poll`. Keep tests for `viewer_url` generation and `find_task_dir_by_pty_session`.
- `tests/test_mcp_integration.py`: replace the manual `_schedule_deferred_close` call in the E2E test with assertions that the PTY is torn down within ~100ms of `result.json` appearing.

### New tests
- `tests/test_replay_only_flag.py` (new):
  1. `attach_session` on a `replay_only=True` PTY that is still alive returns `{replay: true, output: [transcript]}`, not the live buffer.
  2. `attach_session` on a `replay_only=False` PTY that is still alive returns the live buffer (unchanged behavior).
  3. `mcp_create_pty_session(replay_only=True)` stores the flag in the session dict.
  4. `coda_run` end-to-end (using the existing `test_mcp_integration.py:396` pattern — call `complete_task` + close path directly, do NOT wait for the 5s watcher poll cycle): after the close call, slot count returns to baseline immediately. **No timing-based assertion** — call ordering is the contract.
  5. **Regression guard**: assert that a session dict created via `coda_run`'s path contains NO `grace` key, and that `mcp_create_pty_session` does not accept a `grace` keyword argument. Prevents future drift that accidentally re-introduces grace on the `coda_run` path.

### Test count expectation
- Removals: 4 (`test_transcript.py`) + 2 (`test_mcp_server.py`) + 1 (`test_mcp_integration.py`) = 7 grace-only tests dropped. `test_replay_attach.py` has 2 tests that get rewritten, not removed.
- Additions: 5 new tests in `test_replay_only_flag.py`.
- **Net: -2 tests overall.**
- Total: targets ~525 passing + ~10 PTY-gated skipped

## Out of scope (for Todo 1)

- **`coda_interactive` tool** (Mode 2): designed in a separate spec / Todo 2.
- Changes to Mode 1 direct-launch lifecycle: untouched. The 24h-idle / WS-heartbeat-extends behavior stays as-is for tabs.
- Backporting the `replay_only` concept to historical `coda_run`-created sessions on disk: not necessary. Old transcripts on disk are served via the same path; the flag matters only at attach-time for alive PTYs.

## Migration / Rollout

- Single commit (or small commit chain) to the `coda-mcp` branch, on top of PR #66's merge.
- No data migration: `replay_only` defaults to `False`, so existing sessions in any in-flight worker process behave unchanged. Future `coda_run` invocations get `replay_only=True`.
- No config flag needed — the behavior change is unconditional.
- No deployment ordering constraint: app restart picks up the new behavior cleanly.

## Open questions

None blocking. The design is concrete enough for planning.

## Critique gate

**Cleared** (2026-05-28). Critic verdict: APPROVE WITH CHANGES. All flagged issues incorporated above:
- Pre-existing hooks-never-wired reality documented in Section 4 (informational — simplifies rip-out)
- Step 5 corrected: `_close_pty_immediately(session_id)` exists at `mcp_server.py:167`, not `app.py`
- `_bump_session_last_poll(session_id, delta_s)` added to `app.py` rip-out inventory
- Test count corrected to -2 (was -6); assertion #4 rewritten to be deterministic (call-ordering, not 100ms timing)
- MCP `instructions` string at `mcp_server.py:61-66` added to "docstrings to update" list
- 5th compensating regression test added to prevent future grace re-introduction
- `_serve_transcript_replay()` extraction note expanded with data-source clarification

Original five critique questions, all answered in the critique pass:
1. **Rip-out scope** — mostly complete; missed `_bump_session_last_poll` (added) and the hooks-never-wired note (added)
2. **Flag placement** — `replay_only` on session dict is correct; disk-based alternative would add latency
3. **Mode 2 forward-compat** — verified clean; 24h idle clock starts from session creation, behaves correctly whether human attaches or not
4. **Replay-only edge cases** — no admin override needed (admins use Mode 1 directly); partial-transcript-during-live behavior is intentional
5. **100ms assertion** — confirmed flake-bait (watcher polls every 5s); replaced with `test_mcp_integration.py:396`-style direct-call assertion

Plus five additional critic-eye questions, all resolved:
6. **Concurrency/race** — verified safe under GIL + `sessions_lock`; both interleavings serve transcript correctly
7. **Grace was load-bearing** — confirmed obsolete for Mode 3; live-watch case shifts to Mode 2 as designed
8. **Refactor coupling** — `_serve_transcript_replay` extraction is clean, no field synthesis needed
9. **Documentation drift** — `docs/mcp-v2-background-execution.md` predates PR #66 (no drift); only the MCP `instructions` string needs updating
10. **Test budget** — confirmed -2 net with the regression-guard test added
