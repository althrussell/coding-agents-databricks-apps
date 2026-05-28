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
