"""Tests for coda_interactive — terminal-side workspace pull (no server-side export)."""
import asyncio
import inspect
import json
import os

import pytest

from coda_mcp import mcp_server

ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Wire PTY hooks with recording mocks; HOME -> tmp so project_dir is sandboxed.

    The ``_app_send_input`` mock simulates a SUCCESSFUL ``export-dir`` by creating
    the target dir + a file when it sees the pull command. Tests that want the
    failure path set ``state["simulate_pull"] = False``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs: list[str] = []
    state = {"pty_id": "pty-abc123", "simulate_pull": True, "closed": []}

    def fake_create(label, replay_only=False, **kw):
        return state["pty_id"]

    def fake_send(pty_id, text):
        inputs.append(text)
        # Simulate export-dir landing files on disk for the success path.
        if state["simulate_pull"] and "export-dir" in text:
            project_dir = os.path.join(
                os.path.expanduser("~/.coda/projects"), state["pty_id"]
            )
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
    monkeypatch.setattr(
        mcp_server.url_builder, "build_viewer_url", lambda pid: f"https://viewer/{pid}"
    )
    return inputs, state


# ── new contract: terminal-side pull ─────────────────────────────────


@pytest.mark.asyncio
async def test_pull_command_is_sent_first(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="analyze", workspace_path="/Workspace/Users/x@y.com/WAM", agent="claude"
    )
    first = inputs[0]
    assert "databricks workspace export-dir" in first
    assert "/Users/x@y.com/WAM" in first          # /Workspace prefix stripped
    assert "/Workspace/Users" not in first
    assert "./WAM" in first
    assert first.rstrip().endswith("WAM")          # final `cd <name>`


@pytest.mark.asyncio
async def test_agent_launches_after_successful_pull(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"
    )
    assert any(t.strip() == "claude" for t in inputs)


@pytest.mark.asyncio
async def test_prompt_seeded_with_context_line(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="DO THE THING", workspace_path="/Users/x/WAM", agent="claude"
    )
    seeded = inputs[-1]
    assert "/Users/x/WAM" in seeded
    assert "DO THE THING" in seeded
    assert "Workspace" in seeded                                    # precondition (clean fail, not ValueError)
    assert seeded.index("Workspace") < seeded.index("DO THE THING")  # context precedes prompt


@pytest.mark.asyncio
async def test_empty_pull_returns_error_and_no_launch(wired):
    inputs, state = wired
    state["simulate_pull"] = False  # export-dir produces nothing
    out = json.loads(await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"
    ))
    assert out["status"] == "error"
    assert state["closed"] == [state["pty_id"]]              # PTY closed
    assert not any(t.strip() == "claude" for t in inputs)    # agent NOT launched


@pytest.mark.asyncio
async def test_happy_path_returns_launched(wired):
    out = json.loads(await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"
    ))
    assert out["status"] == "launched"
    assert out["viewer_url"] == "https://viewer/pty-abc123"
    assert out["project_dir"].endswith(os.path.join("pty-abc123", "WAM"))


@pytest.mark.asyncio
async def test_unknown_agent_rejected(wired):
    out = json.loads(await mcp_server.coda_interactive(
        prompt="x", workspace_path="/Users/x/WAM", agent="bogus"
    ))
    assert out["status"] == "error" and "Unknown agent" in out["error"]
    for allowed in ALLOWED_AGENTS:
        assert allowed in out["error"]


@pytest.mark.asyncio
async def test_pty_hook_not_wired(monkeypatch):
    monkeypatch.setattr(mcp_server, "_app_create_session", None)
    monkeypatch.setattr(mcp_server, "_app_send_input", None)
    out = json.loads(await mcp_server.coda_interactive(
        prompt="x", workspace_path="/Users/x/WAM", agent="claude"
    ))
    assert out["status"] == "error" and "PTY hook" in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,cmd", [
    ("claude", "claude"), ("hermes", "hermes chat"), ("codex", "codex"),
    ("gemini", "gemini"), ("opencode", "opencode"),
])
async def test_agent_matrix(wired, agent, cmd):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent=agent
    )
    assert any(t.strip() == cmd for t in inputs)


def test_no_blocking_sleep_in_source():
    src = inspect.getsource(mcp_server.coda_interactive)
    assert "time.sleep(" not in src


def test_no_workspaceclient_in_module():
    """The export-era WorkspaceClient import/use is gone from the module."""
    src = inspect.getsource(mcp_server)
    assert "export_workspace_tree" not in src
    assert "workspace.get_status(" not in src


# ── preserved signature / contract guards ────────────────────────────


def test_default_agent_is_claude():
    sig = inspect.signature(mcp_server.coda_interactive)
    assert sig.parameters["agent"].default == "claude"


def test_no_branch_parameter():
    sig = inspect.signature(mcp_server.coda_interactive)
    assert "branch" not in sig.parameters


def test_instructions_drop_stale_export_wording_and_keep_contract():
    """Server-level MCP instructions: no stale server-side export claim; contract intact."""
    txt = mcp_server.mcp.instructions
    lowered = txt.lower()
    # Stale server-side export wording is gone; real mechanism named.
    assert "server-side snapshot" not in txt
    assert "export-dir" in txt
    # Still-valid broadened contract: plain folders accepted + upload-first pattern.
    assert "coda_interactive" in txt
    assert (
        "git folder or" in lowered
        or "plain workspace folder" in lowered
        or "plain folder" in lowered
    )
    assert "upload" in lowered or "workspace.import" in lowered


# ── preserved wait-helper behavior tests (now via the wrapper) ────────


def test_wait_for_agent_ready_returns_when_buffer_stabilizes(monkeypatch):
    """Wrapper returns once the output buffer has been stable for the window."""
    from app import sessions

    sid = "pty-stabilize-test"
    sessions[sid] = {"output_buffer": [b"banner line\n", b"prompt> "]}
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 2.0)
    try:
        async def _run():
            import time
            t0 = time.time()
            await mcp_server._wait_for_agent_ready(sid)
            return time.time() - t0
        elapsed = asyncio.run(_run())
        assert elapsed < 1.0, f"Helper took {elapsed:.2f}s — should return quickly when stable"
    finally:
        sessions.pop(sid, None)


def test_wait_for_agent_ready_times_out_when_buffer_empty(monkeypatch):
    """Wrapper returns at max-wait if the buffer never gets content."""
    from app import sessions

    sid = "pty-empty-test"
    sessions[sid] = {"output_buffer": []}
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 0.3)
    try:
        async def _run():
            import time
            t0 = time.time()
            await mcp_server._wait_for_agent_ready(sid)
            return time.time() - t0
        elapsed = asyncio.run(_run())
        assert 0.2 <= elapsed <= 0.8, f"Expected ~0.3s max-wait; got {elapsed:.2f}s"
    finally:
        sessions.pop(sid, None)


def test_wait_for_agent_ready_returns_when_session_gone(monkeypatch):
    """Wrapper returns immediately if the session is no longer present."""
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 5.0)

    async def _run():
        import time
        t0 = time.time()
        await mcp_server._wait_for_agent_ready("nonexistent-pty-id")
        return time.time() - t0
    elapsed = asyncio.run(_run())
    assert elapsed < 0.5, f"Helper took {elapsed:.2f}s — should return when session gone"
