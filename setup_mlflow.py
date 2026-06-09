"""Wire up Claude Code's Stop hook for MLflow tracing.

Gated on MLFLOW_TRACING_ENABLED — the same switch enables Codex and Gemini
tracing in their respective setup scripts. Traces land in
/Users/{app_owner}/{app_name}.
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

# Merge MLflow env vars (always written so flipping the flag at runtime works
# without rerunning setup — Claude reads MLFLOW_CLAUDE_TRACING_ENABLED on launch).
settings.setdefault("env", {})
settings["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] = "true" if tracing_enabled else "false"
settings["env"]["MLFLOW_TRACKING_URI"] = "databricks"
settings["env"]["MLFLOW_EXPERIMENT_NAME"] = experiment_name
# Override container-level OTEL endpoint so MLflow uses its native MlflowV3SpanExporter
# instead of sending traces to a non-existent localhost:4314 OTLP collector
settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

# Add Stop hook (processes full transcript at session end).
#
# The hook is wired even when tracing is disabled so the flag can be flipped at
# runtime without rerunning setup. It MUST be import-safe: the default
# dependency is mlflow-skinny (no mlflow.claude_code) and full mlflow is only
# present when tracing is opted in — so we invoke a small wrapper that swallows
# a missing-module import instead of inlining the import in the command. An
# inline import made every agent turn print "Stop hook error: No module named
# 'mlflow'". The wrapper lives beside this file in the deployed app source.
import shlex

python_cmd = "uv run python"
hook_script = str(Path(__file__).resolve().with_name("mlflow_stop_hook.py"))
mlflow_hook = {
    "hooks": [
        {
            "type": "command",
            "command": f"{python_cmd} {shlex.quote(hook_script)}",
        }
    ]
}

existing_hooks = settings.get("hooks", {})
stop_hooks = existing_hooks.get("Stop", [])


def _is_mlflow_stop_hook(h: dict) -> bool:
    """True for any mlflow tracing Stop hook — new wrapper OR old inline import.

    Dropping the old inline-import variant on upgrade is essential: re-running
    setup over a settings.json that still carries the broken
    ``... import stop_hook_handler ...`` command would otherwise leave it
    alongside the safe wrapper, and it'd keep erroring every turn.
    """
    if not isinstance(h, dict):
        return False
    cmd = h.get("hooks", [{}])[0].get("command", "")
    return "mlflow_stop_hook.py" in cmd or "mlflow.claude_code.hooks" in cmd


# Replace any existing mlflow Stop hook (new or legacy) with the single safe one.
stop_hooks = [h for h in stop_hooks if not _is_mlflow_stop_hook(h)]
stop_hooks.append(mlflow_hook)
existing_hooks["Stop"] = stop_hooks
settings["hooks"] = existing_hooks

settings_path.write_text(json.dumps(settings, indent=2))
print(f"MLflow tracing {'ENABLED' if tracing_enabled else 'disabled'}: experiment={experiment_name}")
print(f"  Tracking URI: databricks")
print(f"  Settings updated: {settings_path}")
if not tracing_enabled:
    print("  Set MLFLOW_TRACING_ENABLED=true (in app.yaml) to enable Claude + Codex + Gemini tracing.")
