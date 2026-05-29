# `coda_interactive` Terminal-Side Pull — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Replace `coda_interactive`'s broken server-side Workspace export (runs as the app SP, which can't read the user's folder) with a terminal-side `databricks workspace export-dir` pull (runs as the user), guarded by a split wait + a server-side filesystem post-check. Delete `workspace_export.py`.

**Architecture:** The MCP server types a chained `cd && databricks workspace export-dir <src> ./<name> && cd <name>` into the PTY (which is authenticated as the app owner), waits for the pull to settle, verifies on the local filesystem that files arrived, then launches the agent and seeds the prompt. No `WorkspaceClient` in the tool anymore.

**Tech stack:** Python 3.11, pytest, FastMCP. No new dependencies. Run tests with `uv run pytest`.

**Reference:** `docs/superpowers/specs/2026-05-28-coda-interactive-terminal-pull-design.md` (full design, error table, risks).

---

## Files

- **Modify:** `coda_mcp/mcp_server.py` — remove export import + `WorkspaceClient` usage; add `re` import; add `_safe_dirname`, `_normalize_workspace_path`; refactor `_wait_for_agent_ready` → `_wait_for_output_stable` + wrapper; add `_EXPORT_MAX_WAIT_S`/`_EXPORT_STABILITY_S`; rewrite `coda_interactive` body.
- **Delete:** `coda_mcp/workspace_export.py`, `tests/test_workspace_export.py`.
- **Modify:** `tests/test_replay_only_flag.py` — refresh stale comment (line ~166).
- **Rewrite:** `tests/test_coda_interactive.py`.
- **Modify:** `tests/test_mcp_server.py` — add helper + wrapper tests.

## Pre-flight

- Worktree: `/Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp`, branch `feat/coda-mcp-interactive-handoff` (already merged with main / deps bump, HEAD `2dd66aa`).
- Commit identity: `-c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty"`. No AI co-author.
- `databricks workspace export-dir SOURCE TARGET` is verified: creates TARGET, recursive, auto notebook extensions, `--overwrite` flag (not needed here).

---

## Task 1: Helpers + wait-helper refactor (TDD)

**Files:** Modify `coda_mcp/mcp_server.py`; add tests to `tests/test_mcp_server.py`.

- [ ] **Step 1: Write failing tests** — append to `tests/test_mcp_server.py`:

```python
class TestInteractiveHelpers:
    def test_safe_dirname_basename(self):
        from coda_mcp.mcp_server import _safe_dirname
        assert _safe_dirname("/Users/x@y.com/WAM") == "WAM"
        assert _safe_dirname("/Users/x@y.com/WAM/") == "WAM"

    def test_safe_dirname_sanitizes(self):
        from coda_mcp.mcp_server import _safe_dirname
        assert _safe_dirname("/Users/x/My Project!") == "My_Project_"

    def test_safe_dirname_empty_fallback(self):
        from coda_mcp.mcp_server import _safe_dirname
        assert _safe_dirname("/") == "workspace"
        assert _safe_dirname("") == "workspace"

    def test_normalize_strips_workspace_prefix(self):
        from coda_mcp.mcp_server import _normalize_workspace_path
        assert _normalize_workspace_path("/Workspace/Users/x/WAM") == "/Users/x/WAM"

    def test_normalize_leaves_plain_path(self):
        from coda_mcp.mcp_server import _normalize_workspace_path
        assert _normalize_workspace_path("/Users/x/WAM") == "/Users/x/WAM"
        assert _normalize_workspace_path("/Users/x/WAM/") == "/Users/x/WAM"

    @pytest.mark.asyncio
    async def test_wait_for_agent_ready_delegates(self, monkeypatch):
        """_wait_for_agent_ready calls _wait_for_output_stable with prompt-seed constants."""
        from coda_mcp import mcp_server
        seen = {}
        async def fake_stable(pty, max_wait, stability):
            seen["args"] = (pty, max_wait, stability)
        monkeypatch.setattr(mcp_server, "_wait_for_output_stable", fake_stable)
        await mcp_server._wait_for_agent_ready("pty-1")
        assert seen["args"] == ("pty-1", mcp_server._PROMPT_SEED_MAX_WAIT_S, mcp_server._PROMPT_SEED_STABILITY_S)
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_mcp_server.py::TestInteractiveHelpers -v` → all fail (symbols don't exist).

- [ ] **Step 3: Add `re` import** to `coda_mcp/mcp_server.py` (near `import os` at line 19, keep alphabetical-ish with the stdlib group):

```python
import re
```

- [ ] **Step 4: Add the two helpers** in `coda_mcp/mcp_server.py` just above `_ALLOWED_AGENTS` (line 336):

```python
def _safe_dirname(workspace_path: str) -> str:
    """Local directory name for the pulled folder = sanitized basename."""
    base = os.path.basename(workspace_path.rstrip("/"))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return safe or "workspace"


def _normalize_workspace_path(workspace_path: str) -> str:
    """Canonical Workspace API path: drop the /Workspace FUSE prefix if present."""
    p = workspace_path.rstrip("/")
    if p.startswith("/Workspace/"):
        p = p[len("/Workspace"):]
    return p
```

- [ ] **Step 5: Refactor the wait helper.** Replace the existing `_wait_for_agent_ready` definition (lines 346-380, the `async def _wait_for_agent_ready(...)` through the end of its `while` loop) with a generalized function plus a thin wrapper. Also add the two new constants next to the existing ones (after line 343):

Add constants (after `_PROMPT_SEED_STABILITY_S = 1.0`):

```python
_EXPORT_MAX_WAIT_S = 120.0   # generous; export-dir prints per-file so it won't prematurely stabilize mid-pull
_EXPORT_STABILITY_S = 1.5
```

Replace the function:

```python
async def _wait_for_output_stable(pty_session_id: str, max_wait: float, stability: float) -> None:
    """Poll the PTY output buffer; return when it stabilizes or max_wait elapses.

    Stability = buffer length unchanged for ``stability`` seconds, after at
    least one byte has appeared. If the session disappears mid-wait, return.
    """
    from app import sessions
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    last_len = -1
    stable_since: float | None = None
    poll_interval = 0.1

    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        sess = sessions.get(pty_session_id)
        if sess is None:
            return
        current_len = sum(len(chunk) for chunk in sess.get("output_buffer", []))
        if current_len > 0 and current_len == last_len:
            if stable_since is None:
                stable_since = loop.time()
            elif (loop.time() - stable_since) >= stability:
                return
        else:
            stable_since = None
            last_len = current_len


async def _wait_for_agent_ready(pty_session_id: str) -> None:
    """Wait for an agent TUI to settle (prompt-seed budget). Wrapper for back-compat."""
    await _wait_for_output_stable(
        pty_session_id, _PROMPT_SEED_MAX_WAIT_S, _PROMPT_SEED_STABILITY_S
    )
```

- [ ] **Step 6: Run, expect PASS** — `uv run pytest tests/test_mcp_server.py::TestInteractiveHelpers -v` → all pass. Then `uv run pytest tests/test_mcp_server.py -q` → no regressions (coda_run still uses `_wait_for_agent_ready`).

- [ ] **Step 7: Ruff** — `uv run ruff check coda_mcp/mcp_server.py tests/test_mcp_server.py` → clean.

- [ ] **Step 8: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_server.py tests/test_mcp_server.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: add _safe_dirname/_normalize_workspace_path + generalize wait helper

_wait_for_output_stable(pty, max_wait, stability) is the parametrized poller;
_wait_for_agent_ready becomes a thin wrapper preserving the 5.0/1.0 budget so
coda_run is unaffected. Adds _EXPORT_MAX_WAIT_S/_EXPORT_STABILITY_S for the
upcoming terminal-side pull wait."
```

---

## Task 2: Rewrite `coda_interactive` + delete export module (TDD)

**Files:** Modify `coda_mcp/mcp_server.py`; delete `coda_mcp/workspace_export.py` + `tests/test_workspace_export.py`; rewrite `tests/test_coda_interactive.py`; touch `tests/test_replay_only_flag.py` comment.

- [ ] **Step 1: Rewrite `tests/test_coda_interactive.py`** to the new contract. Replace the whole file with:

```python
"""Tests for coda_interactive — terminal-side workspace pull (no server-side export)."""
import json
import os

import pytest

from coda_mcp import mcp_server


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Wire PTY hooks with recording mocks; HOME -> tmp so project_dir is sandboxed.

    The _app_send_input mock simulates a SUCCESSFUL export-dir by creating the
    target dir + a file when it sees the pull command. Tests that want the
    failure path override `simulate_pull` to False.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs: list[str] = []
    state = {"pty_id": "pty-abc123", "simulate_pull": True, "closed": []}

    def fake_create(label, replay_only=False, **kw):
        return state["pty_id"]

    def fake_send(pty_id, text):
        inputs.append(text)
        # Simulate export-dir landing files on disk.
        if state["simulate_pull"] and "export-dir" in text:
            # project_dir = ~/.coda/projects/<pty_id>; name parsed from the command tail "cd <name>"
            project_dir = os.path.join(os.path.expanduser("~/.coda/projects"), state["pty_id"])
            # name is the final `cd <name>` token
            name = text.rstrip().rsplit("cd ", 1)[-1].strip().strip("'\"")
            target = os.path.join(project_dir, name)
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "README.md"), "w") as f:
                f.write("# hi")

    def fake_close(pty_id):
        state["closed"].append(pty_id)

    async def fake_wait(*a, **kw):
        return None

    monkeypatch.setattr(mcp_server, "_app_create_session", fake_create)
    monkeypatch.setattr(mcp_server, "_app_send_input", fake_send)
    monkeypatch.setattr(mcp_server, "_app_close_session", fake_close)
    monkeypatch.setattr(mcp_server, "_wait_for_output_stable", fake_wait)
    monkeypatch.setattr(mcp_server, "_wait_for_agent_ready", fake_wait)
    monkeypatch.setattr(mcp_server.url_builder, "build_viewer_url", lambda pid: f"https://viewer/{pid}")
    return inputs, state


@pytest.mark.asyncio
async def test_pull_command_is_sent_first(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="analyze", workspace_path="/Workspace/Users/x@y.com/WAM", agent="claude")
    first = inputs[0]
    assert "databricks workspace export-dir" in first
    assert "/Users/x@y.com/WAM" in first        # /Workspace prefix stripped
    assert "/Workspace/Users" not in first
    assert "./WAM" in first and first.rstrip().endswith("WAM")  # cd <name> tail


@pytest.mark.asyncio
async def test_agent_launches_after_successful_pull(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude")
    assert any(t.strip() == "claude" for t in inputs)


@pytest.mark.asyncio
async def test_prompt_seeded_with_context_line(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="DO THE THING", workspace_path="/Users/x/WAM", agent="claude")
    seeded = inputs[-1]
    assert "/Users/x/WAM" in seeded
    assert "DO THE THING" in seeded
    assert seeded.index("Workspace") < seeded.index("DO THE THING")  # context precedes prompt


@pytest.mark.asyncio
async def test_empty_pull_returns_error_and_no_launch(wired):
    inputs, state = wired
    state["simulate_pull"] = False  # export-dir produces nothing
    out = json.loads(await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"))
    assert out["status"] == "error"
    assert state["closed"] == [state["pty_id"]]          # PTY closed
    assert not any(t.strip() == "claude" for t in inputs)  # agent NOT launched


@pytest.mark.asyncio
async def test_happy_path_returns_launched(wired):
    out = json.loads(await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"))
    assert out["status"] == "launched"
    assert out["viewer_url"] == "https://viewer/pty-abc123"
    assert out["project_dir"].endswith(os.path.join("pty-abc123", "WAM"))


@pytest.mark.asyncio
async def test_unknown_agent_rejected(wired):
    out = json.loads(await mcp_server.coda_interactive(
        prompt="x", workspace_path="/Users/x/WAM", agent="bogus"))
    assert out["status"] == "error" and "Unknown agent" in out["error"]


@pytest.mark.asyncio
async def test_pty_hook_not_wired(monkeypatch):
    monkeypatch.setattr(mcp_server, "_app_create_session", None)
    monkeypatch.setattr(mcp_server, "_app_send_input", None)
    out = json.loads(await mcp_server.coda_interactive(
        prompt="x", workspace_path="/Users/x/WAM", agent="claude"))
    assert out["status"] == "error" and "PTY hook" in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,cmd", [
    ("claude", "claude"), ("hermes", "hermes chat"), ("codex", "codex"),
    ("gemini", "gemini"), ("opencode", "opencode"),
])
async def test_agent_matrix(wired, agent, cmd):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent=agent)
    assert any(t.strip() == cmd for t in inputs)


def test_no_blocking_sleep_in_source():
    import inspect
    src = inspect.getsource(mcp_server.coda_interactive)
    assert "time.sleep(" not in src


def test_no_workspaceclient_in_module():
    """The export-era WorkspaceClient import/use is gone from the module."""
    import inspect
    src = inspect.getsource(mcp_server)
    assert "export_workspace_tree" not in src
    assert "workspace.get_status(" not in src
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_coda_interactive.py -q` → fails (old behavior still in place; `export_workspace_tree`/`get_status` still present).

- [ ] **Step 3: Rewrite `coda_interactive`** in `coda_mcp/mcp_server.py`. Replace the entire function body (lines 416-523, from `if agent not in _ALLOWED_AGENTS:` through the catch-all `return`) with:

```python
    if agent not in _ALLOWED_AGENTS:
        return json.dumps({
            "status": "error",
            "error": f"Unknown agent: {agent!r}. Allowed: {sorted(_ALLOWED_AGENTS)}",
        })

    if _app_create_session is None or _app_send_input is None:
        return json.dumps({
            "status": "error",
            "error": "PTY hook not wired",
        })

    pty_session_id = None
    project_dir = None
    try:
        # Create PTY FIRST so we have its session_id for the project_dir name.
        pty_session_id = _app_create_session(
            label=f"{agent}-interactive",
            replay_only=False,
        )
        project_dir = os.path.join(
            os.path.expanduser("~/.coda/projects"),
            pty_session_id,
        )
        os.makedirs(project_dir, exist_ok=True)

        name = _safe_dirname(workspace_path)
        source_path = _normalize_workspace_path(workspace_path)

        # Pull the Workspace folder into ./<name> AS THE USER (terminal creds).
        # A failed export-dir short-circuits the && chain, leaving <name> absent;
        # the filesystem check below turns that into a real error.
        pull_cmd = (
            f"cd {shlex.quote(project_dir)} && "
            f"databricks workspace export-dir {shlex.quote(source_path)} {shlex.quote('./' + name)} && "
            f"cd {shlex.quote(name)}"
        )
        _app_send_input(pty_session_id, pull_cmd + "\n")

        # Wait for the pull to finish (shell goes idle), then verify on disk.
        await _wait_for_output_stable(
            pty_session_id, _EXPORT_MAX_WAIT_S, _EXPORT_STABILITY_S
        )

        target_dir = os.path.join(project_dir, name)
        if not os.path.isdir(target_dir) or not os.listdir(target_dir):
            if _app_close_session is not None:
                try:
                    _app_close_session(pty_session_id)
                except Exception:
                    pass
            if os.path.isdir(project_dir):
                shutil.rmtree(project_dir, ignore_errors=True)
            return json.dumps({
                "status": "error",
                "error": (
                    f"No files were pulled from {workspace_path}. Check the path "
                    f"exists in the Workspace and that you have read access."
                ),
            })

        # Launch the agent (fresh — same proven path as before).
        launch_cmd = _AGENT_LAUNCH_CMDS[agent]
        _app_send_input(pty_session_id, launch_cmd + "\n")

        # Wait for the agent TUI to settle, then paste the kickoff prompt with a
        # context line naming the source so the agent knows where the files came from.
        await _wait_for_agent_ready(pty_session_id)
        seeded_prompt = (
            f"Your working directory contains files exported from the Databricks "
            f"Workspace path {workspace_path}.\n\n{prompt}"
        )
        _app_send_input(pty_session_id, seeded_prompt + "\n")

        viewer_url = url_builder.build_viewer_url(pty_session_id)

        return json.dumps({
            "status": "launched",
            "viewer_url": viewer_url,
            "agent": agent,
            "project_dir": target_dir,
            "workspace_path": workspace_path,
            "instructions": (
                "Open viewer_url to attach. The agent is running in a directory "
                "holding the files pulled from your Workspace folder, with your "
                "kickoff prompt typed. Type the agent's quit command (e.g. /quit) "
                "then `exit` to end the session. Note: files are a snapshot pulled "
                "via 'databricks workspace export-dir' — git history is not included."
            ),
        })
    except Exception as e:
        if pty_session_id and _app_close_session is not None:
            try:
                _app_close_session(pty_session_id)
            except Exception:
                pass
        if project_dir and os.path.isdir(project_dir):
            shutil.rmtree(project_dir, ignore_errors=True)
        return json.dumps({
            "status": "error",
            "error": f"coda_interactive failed: {e}",
        })
```

- [ ] **Step 4: Update the `coda_interactive` docstring** (lines 398-414). Replace the body text so it no longer says "exports its file tree / server-side snapshot". New docstring:

```python
    """Launch an interactive agent session in CoDA, handed off via a viewer URL.

    The MCP caller passes a Databricks Workspace directory path. CoDA pulls that
    folder onto the session's disk IN THE TERMINAL (authenticated as you) via
    ``databricks workspace export-dir``, launches the chosen agent (claude
    default) in the pulled directory, auto-types ``prompt`` as the first user
    input, and returns a ``viewer_url`` the calling user opens to drive it.

    If the pull produces no files (bad path or no read access) the tool returns
    a ``status=error`` and does not launch the agent.

    Interactive sessions do NOT appear in ``coda_inbox`` and ``coda_get_result``
    will not return anything for them. The viewer URL is the only handle.

    ``email`` is accepted for forward-compatibility and is currently unused.

    Allowed agents: claude (default), hermes, codex, gemini, opencode.
    """
```

- [ ] **Step 5: Remove the dead export imports.** In `coda_mcp/mcp_server.py` line 31, delete:

```python
from coda_mcp.workspace_export import export_workspace_tree, _is_directory
```

And remove the `WorkspaceClient` import guard (lines ~33-36) IF nothing else in the file uses `WorkspaceClient`. Verify first:

```bash
grep -n "WorkspaceClient" coda_mcp/mcp_server.py
```

If the only hits are the import guard, delete the guard block:

```python
try:
    from databricks.sdk import WorkspaceClient
except Exception:
    WorkspaceClient = None  # type: ignore
```

If `WorkspaceClient` is used elsewhere, leave the guard and only remove `coda_interactive`'s usage (already done in Step 3).

- [ ] **Step 6: Delete the export module + its tests**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" rm coda_mcp/workspace_export.py tests/test_workspace_export.py
```

- [ ] **Step 7: Refresh the stale comment** in `tests/test_replay_only_flag.py` (~line 166). It currently references `export_workspace_tree`. Read the surrounding lines and reword so it describes the invariant generically (e.g. "must not create a project directory / pull workspace files") without naming the deleted symbol. Do NOT change the test's logic.

- [ ] **Step 8: Run the target tests, expect PASS**

```bash
uv run pytest tests/test_coda_interactive.py tests/test_mcp_server.py -v
```
Expect all green. If `test_pull_command_is_sent_first` fails on the `endswith("WAM")` assertion, inspect the actual `pull_cmd` string and adjust the test's tail assertion to match the real (shlex-quoted) form — the production string is the source of truth for *behavior*, but the command MUST contain `databricks workspace export-dir`, the normalized source, and a final `cd <name>`.

- [ ] **Step 9: Import sanity** — `uv run python -c "import coda_mcp.mcp_server; import app"` → no ImportError (confirms the deleted module isn't imported anywhere at load time).

- [ ] **Step 10: Ruff** — `uv run ruff check coda_mcp/mcp_server.py tests/test_coda_interactive.py tests/test_replay_only_flag.py` → clean (watch for now-unused imports like `shutil`/`shlex` — both are still used; confirm).

- [ ] **Step 11: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_server.py tests/test_coda_interactive.py tests/test_replay_only_flag.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: coda_interactive pulls workspace files in the terminal, not server-side

Root cause of the empty-session bug: the MCP server's WorkspaceClient runs as
the app service principal, which can't list/export the user's Workspace folder,
and the error was swallowed. Now the tool types 'databricks workspace export-dir'
into the PTY (authed as the user), waits for the pull to settle, verifies files
landed on disk, then launches the agent and seeds the prompt. Deletes
workspace_export.py and the server-side WorkspaceClient/get_status path."
```

---

## Task 3: Full regression sweep

**Files:** none (verification only).

- [ ] **Step 1: Targeted suite**

```bash
uv run pytest tests/test_coda_interactive.py tests/test_mcp_server.py tests/test_task_manager.py tests/test_databricks_preamble.py tests/test_replay_only_flag.py -v
```
Expect green. `test_replay_only_flag.py::test_coda_run_creates_pty_with_replay_only_true` is PTY-fd flaky in multi-file runs — if it fails, re-run that file alone; if it passes alone, it's environmental.

- [ ] **Step 2: Confirm `workspace_export` is fully gone**

```bash
grep -rn "workspace_export\|export_workspace_tree" coda_mcp/ tests/ || echo "CLEAN — no references remain"
```
Expect only (at most) the reworded comment in `test_replay_only_flag.py` if you kept any mention; ideally CLEAN.

- [ ] **Step 3: Ruff over the package**

```bash
uv run ruff check coda_mcp/ tests/test_coda_interactive.py
```
Expect clean.

No commit (verification only). Proceed to final critic + push.

---

## Self-review vs spec

- AC1 (no export/WorkspaceClient/get_status in coda_interactive) → Task 2 Steps 3, 5; guarded by `test_no_workspaceclient_in_module`.
- AC2 (module + tests deleted, no importers) → Task 2 Step 6; Task 3 Step 2.
- AC3 (`_safe_dirname`/`_normalize_workspace_path`) → Task 1 Steps 4; tests Step 1.
- AC4 (`_wait_for_output_stable` + wrapper, coda_run unaffected) → Task 1 Step 5; `test_wait_for_agent_ready_delegates` + `tests/test_mcp_server.py` regression.
- AC5 (first input = chained pull, normalized source, `<name>`) → `test_pull_command_is_sent_first`.
- AC6 (launch only if FS check passes; else error + close) → `test_empty_pull_returns_error_and_no_launch`.
- AC7 (prompt prefixed with context line) → `test_prompt_seeded_with_context_line`.
- AC8 (new + existing suites green) → Task 3.

**Placeholder scan:** none. **Type consistency:** `_wait_for_output_stable(pty, max_wait, stability)` signature identical across Task 1 def, the wrapper, and `coda_interactive`'s two call sites. `_safe_dirname`/`_normalize_workspace_path` names identical in helpers, tests, and `coda_interactive`.

**Risk flagged for the executor:** the `fake_send` mock in `test_coda_interactive.py` parses `<name>` from the command tail via `rsplit("cd ", 1)`. If the production `pull_cmd` quoting makes that parse brittle, the executor should instead compute `name` in the fixture from the known `workspace_path` basename rather than parsing the command. The intent: simulate files appearing at `~/.coda/projects/<pty_id>/<name>/`.
