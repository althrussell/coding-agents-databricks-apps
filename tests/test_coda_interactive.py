"""Tests for the coda_interactive MCP tool."""
import asyncio
import json
import os

import pytest

ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}


async def _no_wait(*a, **kw):
    """No-op replacement for _wait_for_agent_ready in tests."""
    return None


def test_coda_interactive_unknown_agent_returns_error():
    """An agent value not in the allow-list returns status=error and lists allowed values."""
    from coda_mcp import mcp_server

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
        agent="vim",
    ))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert "vim" in result["error"]
    # Error message lists all allowed agents so the calling LLM can correct itself.
    for allowed in ALLOWED_AGENTS:
        assert allowed in result["error"]


def test_coda_interactive_default_agent_is_claude():
    """Calling with no agent kwarg defaults to claude (assertion via signature inspection)."""
    import inspect
    from coda_mcp import mcp_server

    sig = inspect.signature(mcp_server.coda_interactive)
    assert sig.parameters["agent"].default == "claude"


def test_coda_interactive_workspace_path_not_found(monkeypatch):
    """If repos.list() returns no match for workspace_path, status=error."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    fake_client = MagicMock()
    fake_client.repos.list.return_value = []   # no Git Folder at that path

    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/nonexistent",
    ))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert "No Git Folder found" in result["error"]


def test_coda_interactive_branch_update_failure(monkeypatch):
    """If repos.update() raises, return error and don't proceed to PTY."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"

    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]
    fake_client.repos.update.side_effect = Exception("404 branch not found: nonexistent")

    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
        branch="nonexistent",
    ))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert "branch" in result["error"].lower() or "404" in result["error"]


def test_coda_interactive_skips_branch_update_when_empty(monkeypatch):
    """If branch is empty, repos.update() must NOT be called."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"

    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]

    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    # We don't expect a successful return yet (export+PTY not wired); we just
    # verify that repos.update was not called.
    asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
        branch="",
    ))
    fake_client.repos.update.assert_not_called()


def test_coda_interactive_export_failure_cleans_partial_dir(monkeypatch, tmp_path):
    """If export raises mid-way, the partial project dir is removed and the PTY is closed."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    monkeypatch.setenv("HOME", str(tmp_path))

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"
    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]
    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    # PTY-creation hook returns a deterministic id we can predict.
    monkeypatch.setattr(
        mcp_server, "_app_create_session", lambda **kw: "pty-exportfail-id",
    )

    closed = []
    monkeypatch.setattr(
        mcp_server, "_app_close_session", lambda sid: closed.append(sid),
    )

    def fake_export(client, workspace_path, dest_dir):
        # Create the dir + a partial file, then raise.
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "partial.txt"), "w") as f:
            f.write("partial")
        raise RuntimeError("simulated export failure")

    monkeypatch.setattr(mcp_server, "export_workspace_tree", fake_export)

    # send_input hook should NOT be called for export-failure path (we close before launch).
    sent = []
    monkeypatch.setattr(
        mcp_server, "_app_send_input", lambda sid, payload: sent.append((sid, payload)),
    )

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="hello",
        workspace_path="/Workspace/Users/x/proj",
    ))
    result = json.loads(result_str)

    assert result["status"] == "error"
    assert "export" in result["error"].lower()
    # PTY was created — must be closed on failure.
    assert "pty-exportfail-id" in closed, "PTY must be closed when export fails"
    # Project dir cleaned up.
    project_dir = tmp_path / ".coda" / "projects" / "pty-exportfail-id"
    assert not project_dir.exists(), "Partial project dir must be removed after export failure"


def test_coda_interactive_happy_path_sends_agent_command_and_prompt(monkeypatch, tmp_path):
    """End-to-end mock: export succeeds, PTY created, cd + agent + prompt sent in order."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    monkeypatch.setenv("HOME", str(tmp_path))

    fake_repo = MagicMock()
    fake_repo.id = 123
    fake_repo.path = "/Workspace/Users/x/proj"
    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]
    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)

    monkeypatch.setattr(
        mcp_server,
        "export_workspace_tree",
        lambda client, ws_path, dest_dir: os.makedirs(dest_dir, exist_ok=True),
    )
    monkeypatch.setattr(
        mcp_server, "_app_create_session", lambda **kw: "pty-happy-id",
    )

    sent_to_pty = []
    monkeypatch.setattr(
        mcp_server,
        "_app_send_input",
        lambda sid, payload: sent_to_pty.append((sid, payload)),
    )

    # Stub the agent-ready wait so the test runs fast.
    monkeypatch.setattr(mcp_server, "_wait_for_agent_ready", _no_wait)

    monkeypatch.setattr(
        mcp_server.url_builder,
        "build_viewer_url",
        lambda pty_id: f"https://test.example/?session={pty_id}",
    )

    result_str = asyncio.run(mcp_server.coda_interactive(
        prompt="continue debugging the auth flow",
        workspace_path="/Workspace/Users/x/proj",
        agent="claude",
    ))
    result = json.loads(result_str)

    assert result["status"] == "launched"
    assert result["agent"] == "claude"
    assert result["viewer_url"] == "https://test.example/?session=pty-happy-id"
    assert result["project_dir"].endswith("/pty-happy-id")

    # Three PTY writes, in order: cd, agent command, prompt.
    assert len(sent_to_pty) == 3, f"Expected 3 PTY writes; got {sent_to_pty}"
    assert sent_to_pty[0][0] == "pty-happy-id"
    assert sent_to_pty[0][1].startswith("cd "), \
        f"First write should be cd; got {sent_to_pty[0][1]!r}"
    assert sent_to_pty[1] == ("pty-happy-id", "claude\n")
    assert sent_to_pty[2] == ("pty-happy-id", "continue debugging the auth flow\n")


def test_coda_interactive_agent_command_matrix(monkeypatch, tmp_path):
    """Each allowed agent maps to its expected launch command."""
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server

    expected = {
        "claude": "claude\n",
        "hermes": "hermes chat\n",
        "codex": "codex\n",
        "gemini": "gemini\n",
        "opencode": "opencode\n",
    }

    for agent, expected_cmd in expected.items():
        monkeypatch.setenv("HOME", str(tmp_path / agent))

        fake_repo = MagicMock(); fake_repo.id = 1; fake_repo.path = "/W/x/p"
        fake_client = MagicMock()
        fake_client.repos.list.return_value = [fake_repo]
        monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)
        monkeypatch.setattr(
            mcp_server, "export_workspace_tree",
            lambda client, ws_path, dest_dir: os.makedirs(dest_dir, exist_ok=True),
        )
        monkeypatch.setattr(
            mcp_server, "_app_create_session", lambda **kw: f"pty-{agent}",
        )
        sent = []
        monkeypatch.setattr(
            mcp_server, "_app_send_input", lambda sid, p: sent.append(p),
        )
        monkeypatch.setattr(mcp_server, "_wait_for_agent_ready", _no_wait)
        monkeypatch.setattr(
            mcp_server.url_builder, "build_viewer_url",
            lambda pty_id: f"https://test/?s={pty_id}",
        )

        result_str = asyncio.run(mcp_server.coda_interactive(
            prompt="x", workspace_path="/W/x/p", agent=agent,
        ))
        result = json.loads(result_str)
        assert result["status"] == "launched", f"agent {agent}: {result}"

        # sent[0] is cd, sent[1] is the agent command, sent[2] is the prompt.
        assert sent[1] == expected_cmd, \
            f"agent {agent}: expected {expected_cmd!r}, got {sent[1]!r}"


def test_coda_interactive_does_not_use_blocking_sleep(monkeypatch, tmp_path):
    """Regression guard: coda_interactive is async; it must use asyncio.sleep, not time.sleep.

    A blocking sleep in an async handler stalls the event loop and prevents
    concurrent MCP requests from being processed.
    """
    from unittest.mock import MagicMock
    from coda_mcp import mcp_server
    import time as _time

    monkeypatch.setenv("HOME", str(tmp_path))

    fake_repo = MagicMock(); fake_repo.id = 1; fake_repo.path = "/W/x/p"
    fake_client = MagicMock()
    fake_client.repos.list.return_value = [fake_repo]
    monkeypatch.setattr(mcp_server, "WorkspaceClient", lambda: fake_client)
    monkeypatch.setattr(
        mcp_server, "export_workspace_tree",
        lambda client, ws_path, dest_dir: os.makedirs(dest_dir, exist_ok=True),
    )
    monkeypatch.setattr(mcp_server, "_app_create_session", lambda **kw: "pty-noblock-id")
    monkeypatch.setattr(mcp_server, "_app_send_input", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server, "_wait_for_agent_ready", _no_wait)
    monkeypatch.setattr(
        mcp_server.url_builder, "build_viewer_url", lambda pty_id: f"https://t/?s={pty_id}",
    )

    # Trap time.sleep — if anything in coda_interactive calls it, the test fails.
    blocking_calls = []
    monkeypatch.setattr(_time, "sleep", lambda s: blocking_calls.append(s))

    asyncio.run(mcp_server.coda_interactive(
        prompt="x", workspace_path="/W/x/p",
    ))

    assert blocking_calls == [], (
        f"coda_interactive called time.sleep({blocking_calls}); must use asyncio.sleep "
        f"instead so the event loop isn't blocked."
    )


def test_wait_for_agent_ready_returns_when_buffer_stabilizes(monkeypatch):
    """Helper returns once the output buffer has been stable for the configured window."""
    import asyncio
    from app import sessions
    from coda_mcp import mcp_server

    # Set up a fake session with a controllable output buffer.
    sid = "pty-stabilize-test"
    sessions[sid] = {"output_buffer": [b"banner line\n", b"prompt> "]}

    # Shrink the stability window so the test runs fast.
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 2.0)

    try:
        # Buffer is already populated and won't change → helper should return quickly.
        async def _run():
            import time
            t0 = time.time()
            await mcp_server._wait_for_agent_ready(sid)
            return time.time() - t0
        elapsed = asyncio.run(_run())

        # Should return roughly _PROMPT_SEED_STABILITY_S, definitely well under MAX_WAIT.
        assert elapsed < 1.0, f"Helper took {elapsed:.2f}s — should have returned quickly when buffer is stable"
    finally:
        sessions.pop(sid, None)


def test_wait_for_agent_ready_times_out_when_buffer_empty(monkeypatch):
    """Helper returns at max-wait if the buffer never gets any content."""
    import asyncio
    from app import sessions
    from coda_mcp import mcp_server

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

        # Should have hit max-wait since buffer never had content.
        assert 0.2 <= elapsed <= 0.8, f"Expected ~0.3s max-wait timeout; got {elapsed:.2f}s"
    finally:
        sessions.pop(sid, None)


def test_wait_for_agent_ready_returns_when_session_gone(monkeypatch):
    """Helper returns immediately if the session is no longer in the sessions dict."""
    import asyncio
    from coda_mcp import mcp_server

    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 5.0)

    async def _run():
        import time
        t0 = time.time()
        await mcp_server._wait_for_agent_ready("nonexistent-pty-id")
        return time.time() - t0
    elapsed = asyncio.run(_run())

    # Should return well under MAX_WAIT (within one poll cycle).
    assert elapsed < 0.5, f"Helper took {elapsed:.2f}s — should return immediately when session is gone"
