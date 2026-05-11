"""Configure MLflow tracing for Claude Code sessions.

Merges MLflow env vars and a Stop hook into ~/.claude/settings.json so that
every Claude Code session automatically logs traces to a Databricks MLflow
experiment at /Users/{app_owner}/{app_name}.

Tracing is gated by the `MLFLOW_TRACING_ENABLED` environment variable. The
same flag also enables Codex (via @mlflow/codex notify hook in setup_codex.py)
and Gemini CLI (via native OTLP exporter in setup_gemini.py) — one switch,
three agents.
"""

import os
import json
from pathlib import Path

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])
settings_path = home / ".claude" / "settings.json"

# Read existing settings (written by setup_claude.py)
if settings_path.exists():
    settings = json.loads(settings_path.read_text())
else:
    settings = {}

app_owner = os.environ.get("APP_OWNER", "")
app_name = os.environ.get("DATABRICKS_APP_NAME", "coding-agents")

if not app_owner:
    print("MLflow tracing skipped: APP_OWNER not set")
    raise SystemExit(0)

experiment_name = f"/Users/{app_owner}/{app_name}"

# Single switch that controls tracing for Claude, Codex, and Gemini.
# Defaults to "false" so opt-in requires explicit configuration.
tracing_enabled = os.environ.get("MLFLOW_TRACING_ENABLED", "false").lower() == "true"
tracing_value = "true" if tracing_enabled else "false"

# Merge MLflow env vars (always written so flipping the flag at runtime works
# without rerunning setup — Claude reads MLFLOW_CLAUDE_TRACING_ENABLED on launch).
settings.setdefault("env", {})
settings["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] = tracing_value
settings["env"]["MLFLOW_TRACKING_URI"] = "databricks"
settings["env"]["MLFLOW_EXPERIMENT_NAME"] = experiment_name
# Override container-level OTEL endpoint so MLflow uses its native MlflowV3SpanExporter
# instead of sending traces to a non-existent localhost:4314 OTLP collector
settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

# Add Stop hook (processes full transcript at session end).
# The hook is harmless when MLFLOW_CLAUDE_TRACING_ENABLED=false — mlflow's
# stop_hook_handler short-circuits if tracing isn't enabled.
python_cmd = "uv run python"
mlflow_hook = {
    "hooks": [
        {
            "type": "command",
            "command": f"{python_cmd} -c \"from mlflow.claude_code.hooks import stop_hook_handler; stop_hook_handler()\""
        }
    ]
}

existing_hooks = settings.get("hooks", {})
stop_hooks = existing_hooks.get("Stop", [])
# Avoid duplicating the hook if setup runs multiple times
already_present = any(
    "stop_hook_handler" in h.get("hooks", [{}])[0].get("command", "")
    for h in stop_hooks if isinstance(h, dict)
)
if not already_present:
    stop_hooks.append(mlflow_hook)
existing_hooks["Stop"] = stop_hooks
settings["hooks"] = existing_hooks

settings_path.write_text(json.dumps(settings, indent=2))
print(f"MLflow tracing {'ENABLED' if tracing_enabled else 'disabled'}: experiment={experiment_name}")
print(f"  Tracking URI: databricks")
print(f"  Settings updated: {settings_path}")
if not tracing_enabled:
    print("  Set MLFLOW_TRACING_ENABLED=true (in app.yaml) to enable Claude + Codex + Gemini tracing.")
