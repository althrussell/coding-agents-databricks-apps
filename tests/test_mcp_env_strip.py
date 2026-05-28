"""Tests for env-stripping consistency between MCP and HTTP PTY creation paths."""
import os
import pytest

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
def test_mcp_create_pty_session_strips_registry_credentials(monkeypatch):
    """mcp_create_pty_session must strip NPM_TOKEN, UV_DEFAULT_INDEX, UV_INDEX_*_PASSWORD,
    UV_INDEX_*_USERNAME, and npm_config_//* from the child shell's environment —
    matching the HTTP create_session path. Today, these leak into MCP-created PTYs.
    """
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    # Plant registry-credential env vars before creating the PTY.
    monkeypatch.setenv("NPM_TOKEN", "leak-me-npm")
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://leaked-index.example/")
    monkeypatch.setenv("UV_INDEX_MYREG_PASSWORD", "leak-me-uv-pw")
    monkeypatch.setenv("UV_INDEX_MYREG_USERNAME", "leak-me-uv-user")
    monkeypatch.setenv("npm_config_//registry.example/:_authToken", "leak-me-npm-cfg")

    sid = mcp_create_pty_session(label="t-env-strip")
    try:
        env = sessions[sid].get("env", {})
        assert "NPM_TOKEN" not in env, f"NPM_TOKEN leaked into MCP PTY: keys={list(env)}"
        assert "UV_DEFAULT_INDEX" not in env, "UV_DEFAULT_INDEX leaked"
        assert "UV_INDEX_MYREG_PASSWORD" not in env, "UV_INDEX_*_PASSWORD leaked"
        assert "UV_INDEX_MYREG_USERNAME" not in env, "UV_INDEX_*_USERNAME leaked"
        assert not any(k.startswith("npm_config_//") for k in env), "npm_config_// keys leaked"
    finally:
        mcp_close_pty_session(sid)
