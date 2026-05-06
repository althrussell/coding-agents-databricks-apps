"""Update literal tokens in CLI config files on PAT rotation.

Called by pat_rotator._persist_token() every 10 minutes. Lightweight —
just swaps token values in existing files, no installs or script runs.

All writes are atomic (write to `.tmp`, then `os.replace`) so a Hermes / OpenCode
/ Codex invocation that reads the file mid-update sees the old token whole or
the new token whole — never a half-written file. Errors other than "file does
not exist" surface as warnings rather than being silently swallowed.
"""

import json
import os
import re
import logging

logger = logging.getLogger(__name__)

_HOME = os.environ.get("HOME", "/app/python/source_code")
if not _HOME or _HOME == "/":
    _HOME = "/app/python/source_code"


def _atomic_write_text(path, content):
    """Write `content` to `path` atomically via tmp file + rename.

    Prevents the read-while-rewriting race that bit Hermes specifically:
    Hermes reads `~/.hermes/config.yaml` on every invocation, so a bare
    open(path, 'w') by the rotator could leave the file in a partial state
    visible to a concurrent Hermes call → 403 Invalid access token.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)


def update_cli_tokens(token):
    """Update the literal token in all CLI config files."""
    _update_claude(token)
    _update_codex(token)
    _update_opencode(token)
    _update_gemini(token)
    _update_hermes(token)


def _update_claude(token):
    """Update ANTHROPIC_AUTH_TOKEN in ~/.claude/settings.json."""
    path = os.path.join(_HOME, ".claude", "settings.json")
    if not os.path.exists(path):
        return  # setup_claude.py hasn't run yet
    try:
        with open(path) as f:
            settings = json.load(f)
        if "env" in settings and "ANTHROPIC_AUTH_TOKEN" in settings["env"]:
            settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
            _atomic_write_text(path, json.dumps(settings, indent=2))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to update Claude token in %s: %s", path, e)


def _update_codex(token):
    """Update OPENAI_API_KEY in ~/.codex/.env."""
    path = os.path.join(_HOME, ".codex", ".env")
    _replace_dotenv_key(path, "OPENAI_API_KEY", token)


def _update_opencode(token):
    """Update api_key values in ~/.local/share/opencode/auth.json."""
    path = os.path.join(_HOME, ".local", "share", "opencode", "auth.json")
    if not os.path.exists(path):
        return  # setup_opencode.py hasn't run yet
    try:
        with open(path) as f:
            auth = json.load(f)
        changed = False
        for provider in auth.values():
            if isinstance(provider, dict) and "api_key" in provider:
                provider["api_key"] = token
                changed = True
        if changed:
            _atomic_write_text(path, json.dumps(auth, indent=2))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to update OpenCode token in %s: %s", path, e)


def _update_gemini(token):
    """Update GEMINI_API_KEY in ~/.gemini/.env."""
    path = os.path.join(_HOME, ".gemini", ".env")
    _replace_dotenv_key(path, "GEMINI_API_KEY", token)


def _update_hermes(token):
    """Update api_key lines in ~/.hermes/config.yaml."""
    path = os.path.join(_HOME, ".hermes", "config.yaml")
    if not os.path.exists(path):
        return  # setup_hermes.py hasn't run yet
    try:
        with open(path) as f:
            content = f.read()
        new_content = re.sub(
            r'^(  api_key: ).*$',
            rf'\g<1>{token}',
            content,
            flags=re.MULTILINE
        )
        if new_content != content:
            _atomic_write_text(path, new_content)
    except OSError as e:
        logger.warning("Failed to update Hermes token in %s: %s", path, e)


def _replace_dotenv_key(path, key, value):
    """Replace a KEY=value line in a dotenv file."""
    if not os.path.exists(path):
        return  # caller's setup script hasn't run yet
    try:
        with open(path) as f:
            content = f.read()
        new_content = re.sub(
            rf'^{re.escape(key)}=.*$',
            f'{key}={value}',
            content,
            flags=re.MULTILINE
        )
        if new_content != content:
            _atomic_write_text(path, new_content)
    except OSError as e:
        logger.warning("Failed to update %s in %s: %s", key, path, e)
