import os
import pty
import fcntl
import struct
import termios
import select
import subprocess
import uuid
import threading
import signal
import time
import copy
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait
from flask import Flask, send_from_directory, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from werkzeug.utils import secure_filename
from collections import deque

import tomllib
import requests

import app_state
import enterprise_config
from utils import ensure_https, get_gateway_host
from pat_rotator import PATRotator
from obo_auth import OBOTokenManager
from telemetry import log_telemetry, set_product_info

# Sanitize DATABRICKS_TOKEN early — the platform sometimes injects trailing
# newlines / whitespace which causes auth failures.  Cleaning it here prevents
# the agent from "fixing" it in the terminal and leaking the raw token.
_raw_token = os.environ.get("DATABRICKS_TOKEN", "")
if _raw_token != _raw_token.strip():
    os.environ["DATABRICKS_TOKEN"] = _raw_token.strip()

# App version (single source of truth: pyproject.toml)
_pyproject_file = os.path.join(os.path.dirname(__file__), 'pyproject.toml')
try:
    with open(_pyproject_file, 'rb') as _f:
        APP_VERSION = tomllib.load(_f)['project']['version']
except Exception:
    APP_VERSION = '0.0.0'

# Session timeout configuration
SESSION_TIMEOUT_SECONDS = 86400      # No poll for 24 hours = dead session
CLEANUP_INTERVAL_SECONDS = 900       # Check for stale sessions every 15 min
GRACEFUL_SHUTDOWN_WAIT = 3          # Seconds to wait after SIGHUP before SIGKILL
MAX_CONCURRENT_SESSIONS = int(os.environ.get("MAX_CONCURRENT_SESSIONS", "5"))

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# PAT auto-rotation — initialized after sessions dict is defined (see below)

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB — aligned with Claude Code's 30 MB file limit

# WebSocket support via Flask-SocketIO (simple-websocket transport, threading mode)
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins=[], logger=False, engineio_logger=False)

# Store sessions: {session_id: {"master_fd": fd, "pid": pid, "output_buffer": deque, "lock": Lock, ...}}
# sessions_lock guards dict-level ops (add/remove/iterate); each session["lock"] guards per-session state
sessions = {}
sessions_lock = threading.Lock()

# PAT auto-rotation (short-lived tokens, background refresh)
# Only rotates while active sessions exist — stops when all sessions are reaped
pat_rotator = PATRotator(
    session_count_fn=lambda: len(sessions),
)

# OBO agent auth (lab only): captures the attendee's forwarded user token and
# pumps it into the agent CLIs so they act AS the attendee. Dormant unless the
# CODA_OBO_ENABLED gate resolves on (see _obo_enabled / _agent_auth_mode).
obo_manager = OBOTokenManager()

# SIGTERM graceful shutdown: notify clients before gunicorn stops the worker
shutting_down = False

_start_time = time.time()

def handle_sigterm(signum, frame):
    """Notify clients that app is shutting down, then let gunicorn handle the rest."""
    global shutting_down
    # Ignore SIGTERMs in the first 10s — likely stale signals from a prior process kill
    if time.time() - _start_time < 10:
        logger.info("SIGTERM received during startup — ignoring (likely stale signal)")
        return
    shutting_down = True
    logger.info("SIGTERM received — setting shutting_down flag for clients")
    # Notify WS clients immediately (HTTP poll clients will see shutting_down on next poll)
    try:
        socketio.emit('shutting_down', {})
    except Exception:
        pass

# NOTE: Do not register SIGTERM handler at module level.
# It is installed in initialize_app() for gunicorn only.
# For local dev (__main__), we keep SIG_DFL so the process just exits.

# Setup state tracking
setup_lock = threading.Lock()
setup_state = {
    "status": "pending",
    "started_at": None,
    "completed_at": None,
    "error": None,
    "steps": [
        {"id": "git",        "label": "Configuring git identity",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "micro",      "label": "Installing micro editor",      "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "gh",         "label": "Installing GitHub CLI",        "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "dbcli",     "label": "Upgrading Databricks CLI",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "node",       "label": "Ensuring Node.js v22+ (AppKit)", "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "proxy",   "label": "Starting content-filter proxy", "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "claude",     "label": "Configuring Claude CLI",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "codex",      "label": "Configuring Codex CLI",        "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "opencode",   "label": "Configuring OpenCode CLI",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "gemini",     "label": "Configuring Gemini CLI",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "hermes",     "label": "Configuring Hermes Agent",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "appkit",     "label": "Pinning AppKit + warming cache", "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "databricks", "label": "Setting up Databricks CLI",    "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "mlflow",     "label": "Enabling MLflow tracing",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
    ]
}


def _update_step(step_id, **kwargs):
    with setup_lock:
        for step in setup_state["steps"]:
            if step["id"] == step_id:
                step.update(kwargs)
                break


def _get_setup_state_snapshot():
    with setup_lock:
        return copy.deepcopy(setup_state)


# --- Agent enablement / lean lab profile -----------------------------------
#
# Toggleable agents can be turned off to shrink the boot footprint for labs.
# Claude (the primary agent), AppKit, the Databricks CLI, git/editor, and
# MLflow are core and always run. Each toggleable agent maps to an
# ENABLE_<AGENT> env var; CODA_PROFILE provides presets.
_TOGGLEABLE_AGENTS = {
    "codex": "ENABLE_CODEX",
    "opencode": "ENABLE_OPENCODE",
    "gemini": "ENABLE_GEMINI",
    "hermes": "ENABLE_HERMES",
}


def _env_truthy(value):
    return bool(value) and value.strip().lower() in ("true", "1", "yes", "on")


def _coda_profile(env=None):
    """Raw CODA_PROFILE value (lowercased), or "" when unset."""
    env = env if env is not None else os.environ
    return env.get("CODA_PROFILE", "").strip().lower()


def _resolved_profile(env=None):
    """Effective profile. **Unset/empty resolves to ``lab``** — CoDA is
    lab-first, so the lean footprint + guided coach are the default. Set
    ``CODA_PROFILE=full`` explicitly for the power-user build.
    """
    return _coda_profile(env) or "lab"


def _lab_mode(env=None):
    """True when the effective profile is the lab profile (the default)."""
    return _resolved_profile(env) == "lab"


def _agent_enabled(agent, env=None):
    """Whether a toggleable agent should be set up.

    Resolution order:
      1. Explicit ``ENABLE_<AGENT>`` env var (true/false) — always wins.
      2. ``CODA_PROFILE`` preset default: ``lab`` (the default when unset)
         disables all toggleable agents (lean footprint = Claude + AppKit +
         Databricks core only); ``full`` enables them.

    Core steps not in ``_TOGGLEABLE_AGENTS`` are always enabled.
    """
    env = env if env is not None else os.environ
    flag = _TOGGLEABLE_AGENTS.get(agent)
    if flag is None:
        return True
    raw = env.get(flag)
    if raw is not None and raw.strip() != "":
        return _env_truthy(raw)
    return not _lab_mode(env)


def _enabled_setup_steps(env=None):
    """Return the (step_id, command) parallel-setup catalog filtered by toggles.

    Claude / AppKit / Databricks are always present; the rest are included only
    when ``_agent_enabled`` says so for the given environment.
    """
    catalog = [
        ("claude",     ["uv", "run", "python", "setup_claude.py"]),
        ("codex",      ["uv", "run", "python", "setup_codex.py"]),
        ("opencode",   ["uv", "run", "python", "setup_opencode.py"]),
        ("gemini",     ["uv", "run", "python", "setup_gemini.py"]),
        ("hermes",     ["uv", "run", "python", "setup_hermes.py"]),
        ("appkit",     ["uv", "run", "python", "setup_appkit.py"]),
        ("databricks", ["uv", "run", "python", "setup_databricks.py"]),
    ]
    return [(sid, cmd) for sid, cmd in catalog if _agent_enabled(sid, env)]


_LAB_COACH_MARKER = "<!-- coda-lab-coach -->"


def _inject_lab_coach(env=None, home_dir=None):
    """Append the lab-coach block to the agent's user-memory file(s).

    Lab mode only. **Additive + idempotent** (guarded by a sentinel marker):
    it never mutates the tracked ``CLAUDE.md`` and never double-appends. Targets
    Claude Code's global memory (``~/.claude/CLAUDE.md``) plus any *enabled*
    agent's adapted instruction file that exists, so a lab that re-enables an
    agent still gets the coach. Best-effort: any per-file failure is logged and
    skipped, never fatal to boot.
    """
    env = env if env is not None else os.environ
    if not _lab_mode(env):
        return
    coach = Path(__file__).parent / "instructions" / "lab_coach.md"
    if not coach.exists():
        logger.warning("instructions/lab_coach.md not found; skipping coach injection")
        return
    block = coach.read_text()
    home_dir = (
        Path(home_dir)
        if home_dir is not None
        else Path(os.environ.get("HOME", str(Path.home())))
    )

    # Claude's global memory always gets it (created if absent — Claude is the
    # primary lab agent). Other agents only if enabled AND already adapted.
    targets = [home_dir / ".claude" / "CLAUDE.md"]
    optional = {
        "codex": home_dir / ".codex" / "AGENTS.md",
        "gemini": home_dir / ".gemini" / "GEMINI.md",
    }
    for agent, path in optional.items():
        if _agent_enabled(agent, env) and path.exists():
            targets.append(path)

    for path in targets:
        try:
            existing = path.read_text() if path.exists() else ""
            if _LAB_COACH_MARKER in existing:
                continue  # already injected — stay idempotent
            path.parent.mkdir(parents=True, exist_ok=True)
            sep = "" if (existing == "" or existing.endswith("\n")) else "\n"
            with path.open("a") as fh:
                fh.write(f"{sep}\n{block}")
            logger.info(f"Lab coach injected into {path}")
        except Exception as e:  # noqa: BLE001 — never fatal to boot
            logger.warning(f"Lab coach injection skipped for {path}: {e}")


# --- Lab "agent speaks first" auto-launch -----------------------------------
#
# In the lab profile the attendee shouldn't need to know to type `claude`. On
# the first terminal session we seed the PTY with a Claude launch carrying a
# short opening prompt; Claude's coach memory (instructions/lab_coach.md) then
# greets the attendee and runs the persona check. Disable with
# CODA_LAB_AUTOLAUNCH=false.
_LAB_AUTOLAUNCH_PROMPT = (
    "Start the lab: greet me, ask whether I'm technical or business "
    "(skip the question if you already have my saved persona), then guide me to "
    "build and deploy my first Databricks app."
)
_lab_autolaunch_done = False  # only the first eligible lab session auto-launches


def _lab_autolaunch_enabled(env=None):
    """Whether lab auto-launch is on (lab mode + not explicitly disabled)."""
    env = env if env is not None else os.environ
    if not _lab_mode(env):
        return False
    raw = env.get("CODA_LAB_AUTOLAUNCH")
    if raw is not None and raw.strip() != "":
        return _env_truthy(raw)
    return True


def _lab_autolaunch_command(home_dir, env=None):
    """Return the shell command that launches Claude with the seeded opening, or
    ``None`` if it must not run yet (disabled, claude not installed, or no token
    configured — in which case a later session retries).
    """
    env = env if env is not None else os.environ
    if not _lab_autolaunch_enabled(env):
        return None
    claude_bin = Path(home_dir) / ".local" / "bin" / "claude"
    if not claude_bin.exists():
        return None
    if not env.get("DATABRICKS_TOKEN", "").strip():
        return None
    return f'claude "{_LAB_AUTOLAUNCH_PROMPT}"\n'


# Single-user security: only the token owner can access the terminal
app_owner = None


def _run_step(step_id, command):
    _update_step(step_id, status="running", started_at=time.time())
    try:
        env = os.environ.copy()
        if not env.get("HOME") or env["HOME"] == "/":
            env["HOME"] = "/app/python/source_code"
        home = env.get("HOME", "/app/python/source_code")
        # Ensure uv and other tools in ~/.local/bin are on PATH
        local_bin = os.path.join(home, ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        env.pop("DATABRICKS_CLIENT_ID", None)
        env.pop("DATABRICKS_CLIENT_SECRET", None)

        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            _update_step(step_id, status="complete", completed_at=time.time())
        else:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            _update_step(step_id, status="error", completed_at=time.time(), error=err[:500])
    except subprocess.TimeoutExpired:
        _update_step(step_id, status="error", completed_at=time.time(), error="Timed out after 300s")
    except Exception as e:
        _update_step(step_id, status="error", completed_at=time.time(), error=str(e))


def _build_terminal_shell_env(base_env: dict) -> dict:
    """Build the env dict for a user terminal PTY.

    Starts from ``base_env`` (typically ``os.environ``) and strips the
    credentials and CLI-state vars that should never reach a user shell:

    - ``CLAUDECODE`` / ``CLAUDE_CODE_SESSION`` — would mark the terminal as
      a nested-Claude session.
    - ``DATABRICKS_TOKEN`` / ``DATABRICKS_HOST`` — forces CLIs to read
      ``~/.databrickscfg`` per-request so they pick up rotated PATs without
      an env-snapshot rewrite.
    - ``GEMINI_API_KEY`` — same pattern, read from config file instead.
    - ``NPM_TOKEN`` / ``UV_DEFAULT_INDEX`` / ``UV_INDEX_*_PASSWORD`` /
      ``UV_INDEX_*_USERNAME`` / ``npm_config_//host/:_authToken`` —
      deployer-level credentials from app.yaml that must not be readable
      via ``env`` inside the user terminal. The user's npm/uv operations
      still work because ``~/.npmrc`` (written by
      ``enterprise_config.bootstrap``) holds the registry config — they
      just can't see the bearer token in plaintext. (F-01)
    """
    shell_env = base_env.copy()
    shell_env["TERM"] = "xterm-256color"

    # Always-strip fixed names
    for key in (
        "CLAUDECODE", "CLAUDE_CODE_SESSION",
        "DATABRICKS_TOKEN", "DATABRICKS_HOST",
        "GEMINI_API_KEY",
        "NPM_TOKEN", "UV_DEFAULT_INDEX",
    ):
        shell_env.pop(key, None)

    # Pattern-strip operator-named registry credentials
    for key in list(shell_env.keys()):
        if (
            key.startswith("npm_config_//")  # derived registry-auth tokens
            or (
                key.startswith("UV_INDEX_")
                and (key.endswith("_PASSWORD") or key.endswith("_USERNAME"))
            )
        ):
            shell_env.pop(key, None)

    return shell_env


def _setup_git_config():
    """Configure git identity and hooks by writing files directly (no subprocess)."""
    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"

    # Get user identity from Databricks token
    user_email = None
    display_name = None
    try:
        from databricks.sdk import WorkspaceClient
        db_host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
        db_token = os.environ.get("DATABRICKS_TOKEN")
        if db_host and db_token:
            w = WorkspaceClient(host=db_host, token=db_token, auth_type="pat")
            set_product_info(w)
            me = w.current_user.me()
            user_email = me.user_name
            display_name = me.display_name or user_email.split("@")[0]
    except Exception as e:
        logger.warning(f"Could not get user identity from token: {e}")

    # Write ~/.gitconfig directly (more reliable than subprocess git config)
    gitconfig_path = os.path.join(home, ".gitconfig")
    hooks_dir = os.path.join(home, ".githooks")
    os.makedirs(hooks_dir, exist_ok=True)

    lines = []
    if user_email and display_name:
        lines.append("[user]")
        lines.append(f"\temail = {user_email}")
        lines.append(f"\tname = {display_name}")
    lines.append("[core]")
    lines.append(f"\thooksPath = {hooks_dir}")

    with open(gitconfig_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    logger.info(f"Git config written to {gitconfig_path}")

    # Write post-commit hook for workspace sync (works from any CLI: Claude, Gemini, OpenCode, etc.)
    # Only syncs repos inside ~/projects/ — skips the app source and any other repos
    post_commit = os.path.join(hooks_dir, "post-commit")
    with open(post_commit, "w") as f:
        f.write('#!/bin/bash\n')
        f.write('# Auto-sync to Databricks Workspace on commit (works from any CLI)\n')
        f.write('SYNC_LOG="$HOME/.sync.log"\n')
        f.write('\n')
        f.write('# Resolve git repo root (handles commits from subdirectories)\n')
        f.write('REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"\n')
        f.write('if [ -z "$REPO_ROOT" ]; then\n')
        f.write('    echo "[post-commit] $(date +%H:%M:%S) SKIP: not inside a git repo" >> "$SYNC_LOG"\n')
        f.write('    exit 0\n')
        f.write('fi\n')
        f.write('\n')
        f.write('# Only sync repos inside ~/projects/\n')
        f.write('PROJECTS_DIR="$HOME/projects"\n')
        f.write('case "$REPO_ROOT" in\n')
        f.write('    "$PROJECTS_DIR"/*)\n')
        f.write('        ;; # allowed - continue\n')
        f.write('    *)\n')
        f.write('        echo "[post-commit] $(date +%H:%M:%S) SKIP: $REPO_ROOT is outside $PROJECTS_DIR" >> "$SYNC_LOG"\n')
        f.write('        exit 0\n')
        f.write('        ;;\n')
        f.write('esac\n')
        f.write('\n')
        f.write('echo "[post-commit] $(date +%H:%M:%S) syncing $REPO_ROOT" >> "$SYNC_LOG"\n')
        f.write('\n')
        f.write('# Use uv run so sync script gets the correct Python + deps\n')
        f.write('APP_DIR="/app/python/source_code"\n')
        f.write('SYNC_SCRIPT="$APP_DIR/sync_to_workspace.py"\n')
        f.write('\n')
        f.write('if [ -f "$SYNC_SCRIPT" ]; then\n')
        f.write('    nohup uv run --project "$APP_DIR" python "$SYNC_SCRIPT" "$REPO_ROOT" >> "$SYNC_LOG" 2>&1 & disown\n')
        f.write('else\n')
        f.write('    echo "[post-commit] $(date +%H:%M:%S) SKIP: sync script not found" >> "$SYNC_LOG"\n')
        f.write('fi\n')
    os.chmod(post_commit, 0o755)
    logger.info(f"Post-commit hook written to {post_commit}")

    # Reinit app source git to remove template origin (Databricks Apps only)
    _reinit_app_git()


def _reinit_app_git():
    """On Databricks Apps, reinit git to remove template origin remote.

    Safe under Control Tower's ``repos.create`` deploy source: the app
    container's ``/app/python/source_code`` is an ephemeral COPY of the
    workspace source, so reinitializing git here cannot affect the workspace
    Git folder, its repo link, or workspace sync (the post-commit hook only
    syncs repos under ``~/projects``, never the app source). Any failure here
    is non-fatal — we log and continue rather than erroring the git step.
    """
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if app_dir != "/app/python/source_code":
        return  # Local dev — leave git intact

    git_path = os.path.join(app_dir, ".git")
    if not os.path.exists(git_path) and not os.path.islink(git_path):
        return  # Already clean (no template origin to remove)

    import shutil

    try:
        # `.git` is normally a directory, but a Databricks Git folder or a
        # worktree/submodule checkout can surface it as a file or symlink.
        # Handle all three so an unexpected shape never raises mid-setup.
        if os.path.islink(git_path) or os.path.isfile(git_path):
            os.unlink(git_path)
        else:
            shutil.rmtree(git_path)
    except Exception as e:
        # Could not remove the existing git metadata — leave the source as-is.
        # The template origin staying put is harmless in the isolated app
        # container, and is strictly better than aborting git setup.
        logger.warning(f"Skipping app git reinit (could not clear .git): {e}")
        return

    try:
        subprocess.run(["git", "init"], cwd=app_dir, capture_output=True, timeout=30)
        subprocess.run(["git", "add", "."], cwd=app_dir, capture_output=True, timeout=60)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit from coding-agents template"],
            cwd=app_dir, capture_output=True, timeout=60,
        )
        logger.info("Reinitialized app source git (template origin removed)")
    except Exception as e:
        logger.warning(f"App git reinit incomplete (template origin removed): {e}")


def _configure_all_cli_auth(token):
    """Configure auth for ALL coding-agent CLIs after a PAT is provided.

    Called from /api/configure-pat when a user supplies a PAT interactively.
    Handles: Claude CLI (inline), Databricks CLI (via pat_rotator), and
    Codex/OpenCode/Gemini CLIs (by re-running their setup scripts with token in env).
    """
    import json

    from utils import resolve_and_cache_gateway
    resolve_and_cache_gateway()

    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"

    # 1. Configure Claude CLI (~/.claude/settings.json)
    claude_dir = os.path.join(home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    gateway_host = get_gateway_host()
    databricks_host = ensure_https(os.environ.get("DATABRICKS_HOST", "").rstrip("/"))

    if gateway_host:
        anthropic_base_url = f"{gateway_host}/anthropic"
    else:
        anthropic_base_url = f"{databricks_host}/serving-endpoints/anthropic"

    # Read-merge-write to preserve env vars from other setup scripts (e.g. setup_mlflow.py)
    settings_path = os.path.join(claude_dir, "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}

    settings.setdefault("env", {})
    settings["env"]["ANTHROPIC_MODEL"] = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-opus-4-7")
    settings["env"]["ANTHROPIC_BASE_URL"] = anthropic_base_url
    settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
    settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "databricks-claude-opus-4-7"
    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "databricks-claude-sonnet-4-6"
    settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "databricks-claude-haiku-4-5"
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "x-databricks-use-coding-agent-mode: true"
    settings["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    logger.info(f"Claude CLI auth configured: {settings_path}")

    # 2. Configure Databricks CLI (~/.databrickscfg) — already called by
    #    configure_pat() via pat_rotator, but explicit for clarity
    pat_rotator._write_databrickscfg(token)
    logger.info("Databricks CLI auth configured: ~/.databrickscfg")

    # 3. Re-run enabled Codex, OpenCode, Gemini, Hermes setup scripts with the
    #    token in env. They are idempotent: detect CLI already installed, just
    #    write config files. Disabled agents (toggle / lab profile) are skipped
    #    so a lean deployment doesn't re-run setup for agents it never installed.
    env = {**os.environ, "DATABRICKS_TOKEN": token}
    _rerun_scripts = {
        "codex": "setup_codex.py",
        "opencode": "setup_opencode.py",
        "gemini": "setup_gemini.py",
        "hermes": "setup_hermes.py",
    }
    for _agent, script in _rerun_scripts.items():
        if not _agent_enabled(_agent):
            logger.info(f"Skipping CLI re-config for disabled agent '{_agent}'")
            continue
        try:
            result = subprocess.run(
                ["uv", "run", "python", script],
                env=env, capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                logger.info(f"CLI config updated: {script}")
            else:
                logger.warning(f"CLI config failed: {script}: {result.stderr[:200]}")
        except Exception as e:
            logger.warning(f"CLI config error: {script}: {e}")


def _maybe_trigger_setup():
    """Start run_setup() in the background if it hasn't completed. Idempotent."""
    with setup_lock:
        if setup_state["status"] == "complete":
            return False
    threading.Thread(target=run_setup, daemon=True, name="setup-thread").start()
    logger.info("Setup triggered")
    return True


def _capture_obo(headers):
    """In obo mode: capture the forwarded user token; trigger setup on first capture.

    No-op unless the OBO gate is active (lab + CODA_OBO_ENABLED). Idempotent — the
    manager dedupes unchanged tokens, so this is safe to call on every request.
    """
    if _agent_auth_mode() != "obo":
        return
    if obo_manager.update_from_headers(headers):
        _maybe_trigger_setup()


def run_setup():
    with setup_lock:
        setup_state["status"] = "running"
        setup_state["started_at"] = time.time()

    # Apply enterprise (proxy/registry) config before any subprocess runs:
    # writes ~/.npmrc, pushes derived env vars (npm_config_registry, CURL_CA_BUNDLE,
    # etc.) into os.environ so every child process inherits them, and logs a
    # banner of the effective config. No-op when no enterprise env vars are set.
    enterprise_config.bootstrap()

    # Probe AI Gateway once; result is cached in _GATEWAY_RESOLVED for subprocesses
    from utils import resolve_and_cache_gateway
    resolve_and_cache_gateway()

    # --- Sequential prerequisites (git identity + editor) ---
    # Git config — done directly in Python, not as a subprocess
    _update_step("git", status="running", started_at=time.time())
    try:
        _setup_git_config()
        _update_step("git", status="complete", completed_at=time.time())
    except Exception as e:
        _update_step("git", status="error", completed_at=time.time(), error=str(e))

    _run_step("micro", ["bash", "-c",
        "mkdir -p ~/.local/bin && bash install_micro.sh && mv micro ~/.local/bin/ 2>/dev/null || true"])

    _run_step("gh", ["bash", "install_gh.sh"])

    # --- Upgrade Databricks CLI (runtime image ships an older version) ---
    _run_step("dbcli", ["bash", "install_databricks_cli.sh"])

    # --- Ensure Node v22+ for AppKit scaffolding (runtime image may ship older) ---
    # Idempotent: no-op when the present Node already satisfies the minimum.
    # Runs before the parallel agent setup so the npm-based CLIs (codex,
    # opencode, gemini) install against the upgraded Node/npm when one was needed.
    _run_step("node", ["bash", "install_node.sh"])

    # --- Mark disabled (toggled-off / lab-profile) agents as skipped up front ---
    # so the setup UI shows them as intentionally skipped rather than stuck
    # pending.
    for _agent in _TOGGLEABLE_AGENTS:
        if not _agent_enabled(_agent):
            _update_step(_agent, status="skipped", completed_at=time.time())
            logger.info(f"Agent '{_agent}' disabled (toggle/profile) — skipping setup")

    # --- Content-filter proxy (only OpenCode needs it) ---
    # Sanitizes requests/responses between OpenCode and Databricks
    # (see OpenCode #5028, docs/plans/2026-03-11-litellm-empty-content-blocks-design.md).
    # Gated on OpenCode being enabled — no point starting the proxy otherwise.
    if _agent_enabled("opencode"):
        _run_step("proxy", ["uv", "run", "python", "setup_proxy.py"])
    else:
        _update_step("proxy", status="skipped", completed_at=time.time())

    # --- Parallel agent setup (enabled steps only) ---
    parallel_steps = _enabled_setup_steps()

    with ThreadPoolExecutor(max_workers=len(parallel_steps)) as executor:
        futures = [
            executor.submit(_run_step, step_id, command)
            for step_id, command in parallel_steps
        ]
        wait(futures)

    # --- Inject the guided lab-coach block (lab profile only) ---
    # Runs after the agent setups have written their adapted instruction files,
    # so the additive append lands on top of them. No-op outside lab mode.
    try:
        _inject_lab_coach()
    except Exception as e:  # noqa: BLE001 — never fatal to boot
        logger.warning(f"Lab coach injection failed: {e}")

    # --- MLflow setup runs AFTER claude setup to avoid settings.json race ---
    # setup_mlflow.py merges env vars into ~/.claude/settings.json which
    # setup_claude.py also writes; running sequentially prevents clobbering.
    _run_step("mlflow", ["uv", "run", "python", "setup_mlflow.py"])

    # Sync latest token into all CLI configs — covers the race where PAT
    # rotation happened while a setup script was still installing (the
    # rotation's update_cli_tokens() call silently skips missing config files).
    current_token = os.environ.get("DATABRICKS_TOKEN", "")
    if current_token:
        try:
            from cli_auth import update_cli_tokens
            update_cli_tokens(current_token)
            logger.info("Post-setup token sync: all CLI configs updated with current token")
        except Exception as e:
            logger.warning(f"Post-setup token sync failed: {e}")

    with setup_lock:
        any_error = any(s["status"] == "error" for s in setup_state["steps"])
        setup_state["status"] = "error" if any_error else "complete"
        setup_state["completed_at"] = time.time()


def get_token_owner():
    """Get the owner email. Priority: Apps API (app.creator) > PAT (current_user.me).

    Uses the auto-provisioned SP to call the Apps API — no PAT needed for
    owner resolution. Falls back to PAT-based lookup for backward compat.
    """
    from databricks.sdk import WorkspaceClient

    # 1. Try Apps API via SP credentials (no PAT needed)
    app_name = os.environ.get("DATABRICKS_APP_NAME")
    if app_name:
        try:
            w = WorkspaceClient()  # auto-detects SP credentials
            set_product_info(w)
            app = w.apps.get(name=app_name)
            owner = (app.creator or "").lower()
            logger.info(f"Owner resolved from app.creator: {owner}")
            return owner
        except Exception as e:
            logger.warning(f"Could not resolve owner via Apps API: {e}")

    # 2. Fallback: PAT-based resolution
    try:
        host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
        token = os.environ.get("DATABRICKS_TOKEN")
        if not host or not token:
            return None
        w = WorkspaceClient(host=host, token=token, auth_type="pat")
        set_product_info(w)
        username = w.current_user.me().user_name
        return username.lower() if username else username
    except Exception as e:
        logger.warning(f"Could not determine token owner: {e}")
        return None


def get_request_user():
    """Extract user email from Databricks Apps request headers.

    Returns lowercase email to ensure case-insensitive matching against app_owner.
    """
    email = (
        request.headers.get("X-Forwarded-Email")
        or request.headers.get("X-Forwarded-User")
        or request.headers.get("X-Databricks-User-Email")
    )
    return email.lower() if email else email


def _is_databricks_apps():
    """Detect if we're running on Databricks Apps (not local dev)."""
    return os.environ.get("DATABRICKS_APP_PORT") or os.path.isdir("/app/python/source_code")


def _additional_authorized_emails():
    """Emails allowed to use the app *in addition to* the resolved owner.

    The default single-user model authorizes only the app's creator. When the
    app is deployed by a service principal (e.g. an automated workshop/lab
    provisioner), the creator is the SP — whose identity is not a usable human
    login — so no attendee could ever pass the check. Set
    ``CONTROL_TOWER_AUTHORIZED_EMAILS`` (or ``AUTHORIZED_EMAILS``, or the
    singular ``DATABRICKS_APP_AUTHORIZED_EMAIL``) to a comma-separated list of
    emails (attendee + operators) to authorize them explicitly. Empty by
    default, so standard single-user deployments are unchanged.
    """
    raw = (
        os.environ.get("CONTROL_TOWER_AUTHORIZED_EMAILS", "")
        or os.environ.get("AUTHORIZED_EMAILS", "")
    )
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    # Fold in the singular Control-Tower-style var if set (one attendee email).
    single = os.environ.get("DATABRICKS_APP_AUTHORIZED_EMAIL", "").strip().lower()
    if single:
        emails.add(single)
    return emails


def _is_allowlisted(current_user):
    """True if ``current_user`` is in the explicit additional-authorized set."""
    return bool(current_user) and current_user in _additional_authorized_emails()


_VALID_AUTH_MODES = ("owner", "allowlist", "workspace")


def _coda_auth_mode():
    """Resolve the active authorization mode from ``CODA_AUTH_MODE``.

    Modes:
      - ``owner`` (default): single-user. Authorized iff the request identity
        is the resolved app owner OR in the explicit allowlist. Fails CLOSED on
        Databricks Apps when the owner can't be resolved. This is the original
        CoDA behaviour.
      - ``allowlist``: authorized iff the request identity is in the explicit
        allowlist (owner is also allowed when resolved). Owner resolution is
        NOT required — works for SP-deployed apps with a known attendee set.
      - ``workspace``: any authenticated workspace user is authorized (the
        request must carry a verified identity header). Isolation is handled by
        provisioning one app instance per attendee in their own workspace, so
        Control Tower no longer needs to patch the app source / allowlist.

    Unknown / unset values fall back to ``owner``.
    """
    mode = os.environ.get("CODA_AUTH_MODE", "").strip().lower()
    return mode if mode in _VALID_AUTH_MODES else "owner"


def _obo_enabled(env=None):
    """Dedicated gate for OBO (on-behalf-of-user) agent auth. **On by default.**

    In OBO mode the coding-agent CLIs authenticate as the attendee via the
    forwarded user token (``x-forwarded-access-token``) instead of a pasted PAT,
    so everything they build/deploy is owned by the attendee. OBO's refresh model
    is browser-driven (the token is re-captured on inbound requests + a keepalive),
    which only suits attended lab/workshop sessions — so the gate is **hard-gated
    to lab mode** and is ignored entirely outside it (``CODA_PROFILE=full`` →
    always PAT). Within lab (the default, since CoDA is lab-first) it defaults ON;
    set ``CODA_OBO_ENABLED=false`` to fall back to the PAT flow.
    """
    env = env if env is not None else os.environ
    if not _lab_mode(env):
        return False
    raw = env.get("CODA_OBO_ENABLED")
    if raw is not None and raw.strip() != "":
        return _env_truthy(raw)
    return True


def _agent_auth_mode(env=None):
    """Derived agent-auth mode: ``obo`` when the OBO gate is active, else ``pat``
    (user pastes a PAT; PATRotator keeps it fresh)."""
    return "obo" if _obo_enabled(env) else "pat"


def _user_is_authorized(current_user):
    """Central authorization decision shared by HTTP, WebSocket, and configure-pat.

    Returns ``(authorized: bool, denied_user: str | None)``. ``denied_user`` is
    the identity to surface when denying — the request identity, or ``"unknown"``
    when the identity (or, in owner mode, the owner) couldn't be resolved.

    Fails OPEN only for local development (not Databricks Apps).
    """
    mode = _coda_auth_mode()

    # Explicit allowlist always wins, in every mode, and works even when the
    # owner couldn't be resolved (e.g. SP-deployed lab apps).
    if _is_allowlisted(current_user):
        return True, None

    # Workspace mode: any authenticated workspace user is authorized.
    if mode == "workspace":
        if current_user:
            return True, None
        if _is_databricks_apps():
            logger.warning("No user identity in request (workspace mode) — denying access")
            return False, "unknown"
        return True, None  # Local dev only

    # Allowlist mode: owner resolution is not required; only the allowlist
    # (checked above) plus the owner (if resolved) are authorized.
    if mode == "allowlist":
        if current_user and app_owner and current_user == app_owner:
            return True, None
        if not current_user:
            if _is_databricks_apps():
                logger.warning("No user identity in request (allowlist mode) — denying access")
                return False, "unknown"
            return True, None  # Local dev only
        logger.warning(f"Unauthorized access attempt by {current_user} (allowlist mode)")
        return False, current_user

    # Owner mode (default): require owner resolution; fail closed otherwise.
    if not app_owner:
        if _is_databricks_apps():
            logger.error("SECURITY: app_owner not resolved — denying all access (fail-closed)")
            return False, "unknown"
        return True, None  # Local dev only

    if not current_user:
        if _is_databricks_apps():
            logger.warning("No user identity in request on Databricks Apps — denying access")
            return False, "unknown"
        return True, None  # Local dev only

    if current_user != app_owner:
        logger.warning(f"Unauthorized access attempt by {current_user} (owner: {app_owner})")
        return False, current_user

    return True, None


def check_authorization():
    """Check if the current user is authorized to access the app (HTTP).

    Delegates to the central ``_user_is_authorized`` so HTTP, WebSocket, and
    configure-pat share one decision under the active ``CODA_AUTH_MODE``.
    Fails CLOSED on Databricks Apps; fails open only for local dev.
    Fixes: https://github.com/datasciencemonkey/coding-agents-databricks-apps/issues/57
    """
    return _user_is_authorized(get_request_user())


def _check_ws_authorization():
    """Check authorization for WebSocket connections — mirrors HTTP check_authorization().

    Reads the identity from the Socket.IO handshake headers and delegates to the
    same central ``_user_is_authorized`` decision the HTTP path uses.
    """
    # Socket.IO passes HTTP headers from the initial handshake via request context
    raw_user = (
        request.headers.get("X-Forwarded-Email")
        or request.headers.get("X-Forwarded-User")
        or request.headers.get("X-Databricks-User-Email")
    )
    current_user = raw_user.lower() if raw_user else raw_user
    authorized, _ = _user_is_authorized(current_user)
    return authorized


# ── WebSocket Event Handlers ──────────────────────────────────────────────

@socketio.on('connect')
def handle_ws_connect():
    """Authenticate WebSocket connections (AC-3)."""
    if not _check_ws_authorization():
        disconnect()
        return False
    # The WS handshake carries the forwarded token too — capture it as a reliable
    # second capture point (idempotent; the manager dedupes unchanged tokens).
    _capture_obo(request.headers)
    logger.info("WebSocket client connected")


@socketio.on('join_session')
def handle_join_session(data):
    """Client joins a session room to receive output (AC-4)."""
    session_id = data.get('session_id')
    if not session_id:
        return {'status': 'error', 'message': 'session_id required'}

    session = _get_session(session_id)
    if not session:
        return {'status': 'error', 'message': 'Session not found'}

    with session["lock"]:
        session["last_poll_time"] = time.time()
        session["output_buffer"].clear()  # Prevent duplicate output on WS↔HTTP switch

    join_room(session_id)
    logger.info(f"WebSocket client joined session room {session_id}")
    return {'status': 'ok'}


@socketio.on('leave_session')
def handle_leave_session(data):
    """Client leaves a session room (AC-5)."""
    session_id = data.get('session_id')
    if session_id:
        leave_room(session_id)
        logger.info(f"WebSocket client left session room {session_id}")


@socketio.on('terminal_input')
def handle_terminal_input(data):
    """Receive keystrokes from client, write to PTY (AC-6)."""
    session_id = data.get('session_id')
    input_data = data.get('input', '')

    session = _get_session(session_id)
    if not session:
        return

    with session["lock"]:
        session["last_poll_time"] = time.time()
    fd = session["master_fd"]

    try:
        os.write(fd, input_data.encode())
    except OSError as e:
        logger.warning(f"WebSocket input write error for {session_id}: {e}")


@socketio.on('terminal_resize')
def handle_terminal_resize(data):
    """Receive resize events from client (AC-7)."""
    session_id = data.get('session_id')
    cols = data.get('cols', 80)
    rows = data.get('rows', 24)

    session = _get_session(session_id)
    if not session:
        return

    with session["lock"]:
        session["last_poll_time"] = time.time()
    fd = session["master_fd"]

    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError as e:
        logger.warning(f"WebSocket resize error for {session_id}: {e}")


@socketio.on('heartbeat')
def handle_ws_heartbeat(data):
    """Periodic keepalive from WS client — prevents idle session reaping (AC-17)."""
    session_ids = data.get('session_ids', [])
    now = time.time()
    for sid in session_ids:
        session = _get_session(sid)
        if session:
            with session["lock"]:
                session["last_poll_time"] = now


@socketio.on('disconnect')
def handle_ws_disconnect():
    """Log WebSocket disconnections. Do NOT auto-close PTY — client may reconnect."""
    logger.info("WebSocket client disconnected")


def _get_session(session_id):
    """Get a session dict reference under the global lock. Returns None if not found."""
    with sessions_lock:
        return sessions.get(session_id)


def read_pty_output(session_id, fd):
    """Background thread to read PTY output into buffer and push via WebSocket."""
    session = _get_session(session_id)
    if not session:
        return
    pid = session["pid"]
    session_lock = session["lock"]

    while True:
        with sessions_lock:
            if session_id not in sessions:
                break
        try:
            readable, _, errors = select.select([fd], [], [fd], 0.05)
            if readable or errors:
                output = os.read(fd, 65536)
                if not output:
                    # EOF — process exited
                    break
                decoded = output.decode(errors="replace")
                with session_lock:
                    # Buffer for HTTP polling fallback (AC-15)
                    session["output_buffer"].append(decoded)
                    session["last_poll_time"] = time.time()  # Keep session alive during WS output
                # Push via WebSocket to the session room (AC-8)
                try:
                    socketio.emit('terminal_output',
                                  {'session_id': session_id, 'output': decoded},
                                  room=session_id)
                except Exception:
                    pass  # No WebSocket clients — HTTP polling handles it
            else:
                # select timed out — check if process is still alive
                try:
                    pid_result, _ = os.waitpid(pid, os.WNOHANG)
                    if pid_result != 0:
                        # Process exited
                        break
                except ChildProcessError:
                    # Process already reaped
                    break
        except OSError:
            break

    # Process exited or fd closed — notify WebSocket clients (AC-9)
    try:
        socketio.emit('session_exited', {'session_id': session_id}, room=session_id)
    except Exception:
        pass

    logger.info(f"Session {session_id} process exited")

    # Clean up immediately — no zombie sessions in the picker
    if session:
        terminate_session(session_id, session["pid"], session["master_fd"])


def terminate_session(session_id, pid, master_fd):
    """Gracefully terminate a session: SIGHUP -> wait -> SIGKILL -> cleanup."""
    logger.info(f"Terminating stale session {session_id} (pid={pid})")

    # Notify WebSocket clients that the session is closed
    try:
        socketio.emit('session_closed', {'session_id': session_id}, room=session_id)
    except Exception:
        pass

    try:
        os.kill(pid, signal.SIGHUP)
        time.sleep(GRACEFUL_SHUTDOWN_WAIT)

        # Check if still alive, force kill if needed
        try:
            os.kill(pid, 0)  # Check if process exists
            os.kill(pid, signal.SIGKILL)
            logger.info(f"Force killed session {session_id} (pid={pid})")
        except OSError:
            pass  # Already dead

        os.close(master_fd)
    except OSError:
        pass  # Process or fd already gone

    with sessions_lock:
        sessions.pop(session_id, None)


def _get_session_process(pid):
    """Return the name of the foreground child process for *pid*.

    Uses ``pgrep -P`` to find children (works on both macOS and Linux),
    then ``ps -o comm=`` to resolve the process name.

    Returns:
        str: process name, or ``"unknown"`` on any error / dead PID.
    """
    if not isinstance(pid, int) or pid <= 0:
        return "unknown"

    try:
        # Step 1 — find child PIDs via pgrep (cross-platform)
        child_result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if child_result.returncode == 0 and child_result.stdout.strip():
            child_pids = child_result.stdout.strip().splitlines()
            last_child_pid = child_pids[-1].strip()

            # Step 2 — resolve child name
            name_result = subprocess.run(
                ["ps", "-o", "comm=", "-p", last_child_pid],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if name_result.returncode == 0 and name_result.stdout.strip():
                name = name_result.stdout.strip().splitlines()[0].strip()
                # ps may return the full path; take basename
                return os.path.basename(name)

        # Step 3 — no children: fall back to the process itself
        self_result = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if self_result.returncode == 0 and self_result.stdout.strip():
            name = self_result.stdout.strip().splitlines()[0].strip()
            return os.path.basename(name)

        return "unknown"
    except Exception:
        return "unknown"


def cleanup_stale_sessions():
    """Background thread that removes sessions with no recent polling."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)

        now = time.time()
        stale_sessions = []
        warning_threshold = SESSION_TIMEOUT_SECONDS * 0.8

        with sessions_lock:
            session_snapshot = list(sessions.items())

        for session_id, session in session_snapshot:
            with session["lock"]:
                idle = now - session["last_poll_time"]
                if idle > SESSION_TIMEOUT_SECONDS:
                    stale_sessions.append((session_id, session["pid"], session["master_fd"]))
                elif idle > warning_threshold:
                    session["timeout_warning"] = True

        if stale_sessions:
            logger.info(f"Found {len(stale_sessions)} stale session(s) to clean up")

        # Terminate each stale session (outside the lock)
        for session_id, pid, master_fd in stale_sessions:
            terminate_session(session_id, pid, master_fd)


@app.before_request
def authorize_request():
    """Check authorization before processing any request."""
    # Skip auth for health check, setup status, and Socket.IO (has own auth via connect event)
    if request.path in ("/health", "/api/setup-status", "/api/pat-status", "/api/configure-pat", "/api/app-state", "/api/obo-refresh") or request.path.startswith("/socket.io"):
        return None

    authorized, user = check_authorization()
    if not authorized:
        return jsonify({
            "error": "Unauthorized",
            "message": f"This app belongs to {app_owner}. You are logged in as {user}."
        }), 403

    # In OBO mode, every authenticated request carries a fresh forwarded user
    # token — capture it so the agent CLIs stay authed as the attendee.
    _capture_obo(request.headers)

    return None


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # CSP: restrict scripts to self + inline (needed for embedded <script> block),
    # styles to self + inline, block all other sources. Prevents external script injection.
    # connect-src allows WebSocket + API calls to self.
    # Fixes: https://github.com/datasciencemonkey/coding-agents-databricks-apps/issues/58
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/setup-status")
def get_setup_status():
    snap = _get_setup_state_snapshot()
    # Surface lab UX flags so the client can show the "Start building" fallback
    # affordance and know the agent will speak first.
    snap["lab_mode"] = _lab_mode()
    snap["agent_speaks_first"] = _lab_autolaunch_enabled()
    return jsonify(snap)


@app.route("/api/app-state")
def get_app_state():
    """Admin endpoint: persisted app state (owner, last rotation)."""
    return jsonify(app_state.get_state())


@app.route("/api/sessions")
def list_sessions():
    """Return a JSON array of active (non-exited) sessions with metadata."""
    now = time.time()
    with sessions_lock:
        snapshot = list(sessions.items())

    result = []
    for session_id, sess in snapshot:
        if sess.get("exited"):
            continue
        result.append({
            "session_id": session_id,
            "label": sess.get("label", ""),
            "created_at": sess.get("created_at"),
            "last_poll_time": sess.get("last_poll_time"),
            "exited": False,
            "process": _get_session_process(sess["pid"]),
            "idle_seconds": round(now - sess.get("last_poll_time", now), 1),
        })
    return jsonify(result)


@app.route("/api/session/attach", methods=["POST"])
def attach_session():
    """Reattach to an existing session — returns buffered output for replay."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")

    sess = _get_session(session_id)
    if not sess or sess.get("exited"):
        return jsonify({"error": "Session not found or exited"}), 404

    # Reset idle clock so the 24h reaper starts fresh
    sess["last_poll_time"] = time.time()

    return jsonify({
        "session_id": session_id,
        "label": sess.get("label", ""),
        "output": list(sess["output_buffer"]),
        "process": _get_session_process(sess["pid"]),
        "created_at": sess.get("created_at"),
    })


@app.route("/health")
def health():
    with sessions_lock:
        session_count = len(sessions)
    with setup_lock:
        current_setup_status = setup_state["status"]
    return jsonify({
        "status": "healthy",
        "version": APP_VERSION,
        "setup_status": current_setup_status,
        "active_sessions": session_count,
        "session_timeout_seconds": SESSION_TIMEOUT_SECONDS
    })


@app.route("/api/version")
def get_version():
    return jsonify({"version": APP_VERSION})


@app.route("/api/pat-status")
def pat_status():
    """Check if a valid, usable PAT is configured."""
    host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))

    # In OBO mode the agents auth with the forwarded user token, not a pasted PAT.
    # Report "configured" once we've captured one so the UI skips the PAT prompt.
    if _agent_auth_mode() == "obo" and obo_manager.has_token:
        return jsonify({"configured": True, "valid": True,
                        "user": get_request_user() or "user"})

    token = os.environ.get("DATABRICKS_TOKEN", "").strip()

    if not token or pat_rotator.is_token_expired:
        # No token, or token lifetime exceeded (rotation stopped while no sessions)
        return jsonify({"configured": False, "valid": False,
                       "workspace_host": host})

    # Validate with direct HTTP — avoids SDK auth fallback to SP
    try:
        resp = requests.get(f"{host}/api/2.0/preview/scim/v2/Me",
                           headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if resp.status_code == 200:
            user = resp.json().get("userName", "unknown")
            return jsonify({"configured": True, "valid": True, "user": user})
        return jsonify({"configured": True, "valid": False,
                       "workspace_host": host})
    except Exception:
        return jsonify({"configured": True, "valid": False,
                       "workspace_host": host})


@app.route("/api/obo-refresh")
def obo_refresh():
    """Keepalive: re-capture the forwarded user token to keep agents authed.

    The browser hits this every ~20 min; each request carries a fresh
    ``x-forwarded-access-token`` which ``_capture_obo`` pumps into the agent
    configs. No-op in PAT mode. Whitelisted from the owner-auth gate so it can't
    403 a keepalive — it only ever reads the platform-injected token header.
    """
    _capture_obo(request.headers)
    return jsonify({"ok": True})


@app.route("/api/configure-pat", methods=["POST"])
def configure_pat():
    """Accept a user-provided PAT, validate it, and start rotation."""
    # Only an authorized identity may (re-)configure the PAT. Without this,
    # any workspace-SSO'd user who reaches the app could submit their own valid
    # PAT and persistently impersonate the owner — every CLI call would then
    # run under the submitter's identity. The authorization decision honours
    # CODA_AUTH_MODE (owner / allowlist / workspace) via the shared
    # _user_is_authorized helper, so e.g. in workspace mode any authenticated
    # attendee on their own isolated app may bootstrap their PAT.
    #
    # The `and app_owner` guard preserves the bootstrap window: before the
    # owner is resolved (first boot, no PAT yet), this short-circuits to "allow"
    # so the very first configure-pat can bootstrap owner resolution — matching
    # the rest of the auth surface's fail-open-until-resolved behaviour.
    if _is_databricks_apps() and app_owner:
        _req_user = get_request_user()
        _authorized, _ = _user_is_authorized(_req_user)
        if not _authorized:
            logger.warning(f"Rejected configure-pat from unauthorized {_req_user}")
            return jsonify({"error": "Forbidden"}), 403

    # Idempotency / defence-in-depth: bootstrap is single-shot. Once a PAT
    # is configured and the rotator is alive, refuse re-submission. Without
    # this, an XSS or session-hijack vector inside the owner's browser could
    # drive a swap to an attacker-controlled PAT — the owner-gate above
    # would let it through because the request truly does come from the
    # owner's session. The expired-token escape hatch preserves the legitimate
    # re-bootstrap path (rotator timed out while idle, owner needs to refresh).
    if pat_rotator.token and not pat_rotator.is_token_expired:
        logger.warning(
            f"Rejected configure-pat: PAT already active "
            f"(user={get_request_user()}, source={request.remote_addr})"
        )
        return jsonify({
            "error": "PAT already configured. Restart the app to reconfigure."
        }), 409

    data = request.json
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "Token required"}), 400

    # Validate the token — direct HTTP, no SDK fallback
    host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
    try:
        resp = requests.get(f"{host}/api/2.0/preview/scim/v2/Me",
                           headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": "Invalid token"}), 400
        user = resp.json().get("userName", "unknown")
    except Exception as e:
        return jsonify({"error": f"Token validation failed: {e}"}), 400

    # Immediately mint a controlled short-lived token from the user-pasted PAT.
    # This gives us a token ID we own — all future rotations can revoke the old one.
    os.environ["DATABRICKS_TOKEN"] = token
    pat_rotator._current_token = token
    pat_rotator._current_token_id = None
    rotated = pat_rotator._rotate_once()
    if rotated:
        token = pat_rotator.token  # use the newly minted token from here on
        # Revoke only the bootstrap PAT — leave other user PATs intact (#98)
        pat_rotator.revoke_bootstrap_token()
    else:
        # Rotation failed — fall back to user-pasted token (still valid)
        pat_rotator._write_databrickscfg(token)
    pat_rotator.start()

    # Configure all CLI tools (Claude, Codex, OpenCode, Gemini, Databricks)
    _configure_all_cli_auth(pat_rotator.token or token)

    # Run setup now that we have a valid token (installs CLIs, configures agents)
    # Only run if setup hasn't completed yet
    _maybe_trigger_setup()

    logger.info(f"PAT configured interactively by {user} — rotation started")
    return jsonify({"status": "ok", "user": user, "message": "Token configured. Auto-rotation started."})


@app.route("/api/session", methods=["POST"])
def create_session():
    """Create a new terminal session."""
    # Quick reject before forking a PTY (approximate — authoritative check below)
    with sessions_lock:
        if len(sessions) >= MAX_CONCURRENT_SESSIONS:
            return jsonify({"error": f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Close an existing session first."}), 429

    data = request.get_json(silent=True) or {}
    label = data.get("label", "")
    try:
        master_fd, slave_fd = pty.openpty()
        # Set up environment for the shell — strips PAT, SP creds, registry
        # tokens, and other secrets that must not be readable from the
        # user's terminal. See _build_terminal_shell_env docstring for the
        # full list.
        shell_env = _build_terminal_shell_env(os.environ)
        # Ensure HOME is set correctly
        if not shell_env.get("HOME") or shell_env["HOME"] == "/":
            shell_env["HOME"] = "/app/python/source_code"
        # Add ~/.local/bin to PATH for claude command
        local_bin = f"{shell_env['HOME']}/.local/bin"
        shell_env["PATH"] = f"{local_bin}:{shell_env.get('PATH', '')}"

        # Start shell in ~/projects/ directory
        projects_dir = os.path.join(shell_env["HOME"], "projects")
        os.makedirs(projects_dir, exist_ok=True)

        pid = subprocess.Popen(
            ["/bin/bash"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=shell_env,
            cwd=projects_dir
        ).pid
        os.close(slave_fd)  # Parent doesn't need the slave side; child inherited it

        session_id = str(uuid.uuid4())

        with sessions_lock:
            # Authoritative check under the same lock as insertion — prevents
            # TOCTOU race where two concurrent requests both pass the early check.
            if len(sessions) >= MAX_CONCURRENT_SESSIONS:
                os.close(master_fd)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                return jsonify({"error": f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Close an existing session first."}), 429
            sessions[session_id] = {
                "master_fd": master_fd,
                "pid": pid,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": time.time(),
                "created_at": time.time(),
                "label": label,
            }

        # Start background reader thread
        thread = threading.Thread(target=read_pty_output, args=(session_id, master_fd), daemon=True)
        thread.start()

        # Lab profile: the agent speaks first. On the first eligible session,
        # seed Claude with the coach opening so the attendee is greeted without
        # needing to type `claude`. Best-effort; if claude/token aren't ready
        # the flag resets so a later session retries.
        global _lab_autolaunch_done
        do_launch = False
        with sessions_lock:
            if not _lab_autolaunch_done and _lab_autolaunch_enabled():
                _lab_autolaunch_done = True
                do_launch = True
        if do_launch:
            cmd = _lab_autolaunch_command(shell_env["HOME"])
            if cmd:
                try:
                    os.write(master_fd, cmd.encode())
                    logger.info("Lab autolaunch: seeded Claude coach greeting into first session")
                except OSError as e:
                    logger.warning(f"Lab autolaunch write failed: {e}")
                    with sessions_lock:
                        _lab_autolaunch_done = False
            else:
                # claude/token not ready yet — let a later session try again.
                with sessions_lock:
                    _lab_autolaunch_done = False

        # Telemetry: track session creation with agent type
        log_telemetry("agent", label or "shell")

        return jsonify({"session_id": session_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/input", methods=["POST"])
def send_input():
    """Send input to the terminal."""
    data = request.json
    session_id = data.get("session_id")
    input_data = data.get("input", "")

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    fd = session["master_fd"]

    try:
        os.write(fd, input_data.encode())
        return jsonify({"status": "ok"})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Save an uploaded file (e.g. clipboard image) and return its path."""
    logger.info(f"Upload request: content_type={request.content_type}, content_length={request.content_length}")

    if "file" not in request.files:
        logger.warning(f"Upload missing 'file' key. Keys: {list(request.files.keys())}")
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    logger.info(f"Upload file: name={f.filename}, content_type={f.content_type}")

    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"
    upload_dir = os.path.join(home, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = f"{uuid.uuid4().hex[:8]}_{secure_filename(f.filename)}"
    file_path = os.path.join(upload_dir, safe_name)
    f.save(file_path)

    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    logger.info(f"Upload saved: {file_path} ({file_size} bytes)")

    # Telemetry: track file uploads
    log_telemetry("event", "file_upload")

    return jsonify({"path": file_path})


@app.route("/api/output", methods=["POST"])
def get_output():
    """Get output from the terminal."""
    data = request.json
    session_id = data.get("session_id")

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    with session["lock"]:
        session["last_poll_time"] = time.time()
        # Atomic buffer swap: replace buffer, then join outside the lock
        old_buffer = session["output_buffer"]
        session["output_buffer"] = deque(maxlen=1000)
        exited = session.get("exited", False)
        timeout_warning = session.pop("timeout_warning", False)

    output = "".join(old_buffer)

    return jsonify({"output": output, "exited": exited, "shutting_down": shutting_down, "timeout_warning": timeout_warning})


@app.route("/api/output-batch", methods=["POST"])
def get_output_batch():
    """Get output from multiple terminal sessions in one request.

    Accepts: {"session_ids": ["id1", "id2", ...]}
    Returns: {"outputs": {"id1": {"output": "...", "exited": false}, ...}}
    """
    data = request.json or {}
    session_ids = data.get("session_ids")

    if session_ids is None:
        return jsonify({"error": "session_ids required"}), 400

    outputs = {}
    now = time.time()

    # Step 1: Resolve session refs under global lock (fast dict lookups only)
    resolved = {}
    with sessions_lock:
        for sid in session_ids:
            if sid in sessions:
                resolved[sid] = sessions[sid]

    # Step 2: Swap buffers under per-session locks (same pattern as get_output)
    swapped = {}
    for sid, session in resolved.items():
        with session["lock"]:
            session["last_poll_time"] = now
            old_buffer = session["output_buffer"]
            session["output_buffer"] = deque(maxlen=1000)
            exited = session.get("exited", False)
            timeout_warning = session.pop("timeout_warning", False)
        swapped[sid] = (old_buffer, exited, timeout_warning)

    # Step 3: Join strings outside all locks
    for sid, (old_buffer, exited, timeout_warning) in swapped.items():
        outputs[sid] = {
            "output": "".join(old_buffer),
            "exited": exited,
            "timeout_warning": timeout_warning,
        }

    return jsonify({"outputs": outputs, "shutting_down": shutting_down})


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    """Lightweight keep-alive — resets timeout without draining output buffer."""
    data = request.json
    session_id = data.get("session_id")

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    with session["lock"]:
        session["last_poll_time"] = time.time()
        timeout_warning = session.pop("timeout_warning", False)
    return jsonify({"status": "ok", "timeout_warning": timeout_warning})


@app.route("/api/resize", methods=["POST"])
def resize_terminal():
    """Resize the terminal."""
    data = request.json
    session_id = data.get("session_id")
    cols = data.get("cols", 80)
    rows = data.get("rows", 24)

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    fd = session["master_fd"]

    try:
        # Set terminal size using TIOCSWINSZ
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        return jsonify({"status": "ok"})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/session/close", methods=["POST"])
def close_session():
    """Gracefully close a terminal session, killing the process."""
    data = request.json
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    session = _get_session(session_id)
    if not session:
        return jsonify({"status": "ok", "detail": "session not found"})

    pid = session["pid"]
    master_fd = session["master_fd"]

    terminate_session(session_id, pid, master_fd)
    logger.info(f"Session {session_id} closed by client")
    return jsonify({"status": "ok"})


def initialize_app(local_dev=False):
    """One-time init: detect owner, start cleanup thread."""
    global app_owner

    # Install SIGTERM handler only for gunicorn (production).
    # For local dev, SIG_DFL is fine — the process just exits cleanly.
    if not local_dev:
        signal.signal(signal.SIGTERM, handle_sigterm)

    # SP credentials preserved — needed for Apps API (owner resolution) and secret persistence

    # Resolve owner: Apps API (app.creator via SP) > PAT (current_user.me)
    app_owner = get_token_owner()
    if app_owner:
        logger.info(f"App owner: {app_owner}")
        os.environ["APP_OWNER"] = app_owner
        app_state.set_app_owner(app_owner)
    else:
        logger.warning("Could not determine app owner - authorization disabled")

    # Strip SP credentials — only needed for owner resolution above.
    # Keeping them causes SDK to silently fall back to SP auth when PAT is dead.
    os.environ.pop("DATABRICKS_CLIENT_ID", None)
    os.environ.pop("DATABRICKS_CLIENT_SECRET", None)
    logger.info("SP credentials stripped — user-token auth from this point")

    # Agent auth mode (observability). In OBO mode we do NOT await/require a PAT
    # at boot — setup is kicked off by _capture_obo on the first authenticated
    # request (the forwarded token arrives with it). PAT mode is unchanged: the
    # interactive configure-pat flow triggers setup.
    mode = _agent_auth_mode()
    logger.info(f"Agent auth mode: {mode}")
    if mode == "obo":
        logger.info("OBO mode: agents act as the attendee; setup starts on first forwarded-token capture")

    # Telemetry: app startup ping (fire-and-forget in background thread)
    log_telemetry("event", "app_startup")

    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    logger.info(f"Started session cleanup thread (timeout={SESSION_TIMEOUT_SECONDS}s, interval={CLEANUP_INTERVAL_SECONDS}s)")


if __name__ == "__main__":
    # Local dev — no SIGTERM handler (SIG_DFL), no shutting_down flag
    initialize_app(local_dev=True)
    shutting_down = False  # safety net: ensure clean state before serving
    port = int(os.environ.get("DATABRICKS_APP_PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
