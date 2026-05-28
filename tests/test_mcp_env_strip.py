"""Tests for _build_terminal_shell_env's credential-stripping behavior.

Replaces the inline 5-key strip that mcp_create_pty_session used to do.
Both create_session (HTTP path) and mcp_create_pty_session (MCP path)
now call this helper, so it must strip both the original 5 keys and
the registry-credential patterns the HTTP path was already covering.
"""
import os
import pytest

from app import _build_terminal_shell_env


# Keys that must be absent from the child shell's env after the strip.
STRIPPED_KEYS = [
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION",
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "GEMINI_API_KEY",
    "NPM_TOKEN",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_MYREG_PASSWORD",
    "UV_INDEX_MYREG_USERNAME",
    "npm_config_//registry.example/:_authToken",
]


@pytest.mark.parametrize("key", STRIPPED_KEYS)
def test_build_terminal_shell_env_strips_credential_key(key):
    """Each known credential / registry-auth key is stripped from the child env."""
    fake_env = {
        "PATH": "/usr/bin:/usr/local/bin",  # positive control — must survive
        "HOME": "/home/test",
        key: "leak-me-test-value",
    }
    result = _build_terminal_shell_env(fake_env)
    assert key not in result, (
        f"{key} survived the strip — registry/auth credential leaked into "
        f"the child shell's env. Result keys: {sorted(result)}"
    )


def test_build_terminal_shell_env_preserves_benign_keys():
    """Positive control: non-credential keys survive the strip.

    Guards against a future regression where the strip becomes too aggressive
    and wipes the env entirely. If THIS test fails, the negative assertions
    above would silently pass for the wrong reason.
    """
    fake_env = {
        "PATH": "/usr/bin:/usr/local/bin",
        "HOME": "/home/test",
        "LANG": "en_US.UTF-8",
    }
    result = _build_terminal_shell_env(fake_env)
    assert result.get("PATH") and "/usr/bin" in result["PATH"]
    assert result.get("HOME") == "/home/test"
    assert result.get("LANG") == "en_US.UTF-8"


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
def test_mcp_create_pty_session_respects_cwd_kwarg(tmp_path):
    """When cwd is passed, sessions[sid]['cwd'] records it."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = None
    try:
        sid = mcp_create_pty_session(label="t-cwd", cwd=str(tmp_path))
        assert sessions[sid].get("cwd") == str(tmp_path)
    finally:
        if sid is not None:
            mcp_close_pty_session(sid)


@_pty_skip
def test_mcp_create_pty_session_cwd_defaults_to_none():
    """When cwd is not passed, sessions[sid]['cwd'] is None (preserves current behavior)."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = None
    try:
        sid = mcp_create_pty_session(label="t-no-cwd")
        assert sessions[sid].get("cwd") is None
    finally:
        if sid is not None:
            mcp_close_pty_session(sid)
