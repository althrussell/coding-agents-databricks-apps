"""Tests for the coda_interactive MCP tool."""
import asyncio
import json
import os

import pytest

ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}


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
