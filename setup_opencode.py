#!/usr/bin/env python
"""Configure OpenCode CLI with Databricks Model Serving (via content-filter proxy) local proxy.

Routes requests through a local content-filter proxy proxy (localhost:4000) which sanitizes empty
text content blocks before forwarding to Databricks AI Gateway. This fixes OpenCode
issue #5028 where empty content blocks cause "Bad Request" errors.
See docs/plans/2026-03-11-litellm-empty-content-blocks-design.md for details.
"""
import os
import json
import subprocess
from pathlib import Path

from utils import ensure_https, get_gateway_host, get_npm_version

# content-filter proxy local proxy — sanitizes empty content blocks before reaching Databricks
# (see https://github.com/sst/opencode/issues/5028)
CONTENT_FILTER_PROXY_URL = "http://127.0.0.1:4000"

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
token = os.environ.get("DATABRICKS_TOKEN", "")
anthropic_model = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-sonnet-4-6")

# 1. Install OpenCode CLI into ~/.local/bin (always, even without token)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
opencode_bin = local_bin / "opencode"

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

if not opencode_bin.exists():
    npm_prefix = str(home / ".local")

    # Resolve exact versions to avoid mutable @latest tags (supply chain hardening)
    oc_version = get_npm_version("opencode-ai")
    oc_pkg = f"opencode-ai@{oc_version}" if oc_version else "opencode-ai@latest"

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"Installing {oc_pkg} (attempt {attempt}/{MAX_RETRIES})...")
        result = subprocess.run(
            ["npm", "install", "-g", f"--prefix={npm_prefix}", oc_pkg],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)}
        )
        if result.returncode == 0 and opencode_bin.exists():
            print(f"OpenCode CLI installed to {opencode_bin}")
            break
        else:
            stderr = result.stderr.strip()
            print(f"OpenCode install failed (attempt {attempt}/{MAX_RETRIES}, rc={result.returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
            if result.stdout.strip():
                print(f"  stdout: {result.stdout.strip()[:500]}")
            if attempt < MAX_RETRIES:
                import time
                print(f"  Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"ERROR: OpenCode installation failed after {MAX_RETRIES} attempts. "
                      f"Run manually: npm install -g --prefix=$HOME/.local opencode-ai")

    # Install @ai-sdk/openai for GPT models (Responses API support)
    sdk_version = get_npm_version("@ai-sdk/openai")
    sdk_pkg = f"@ai-sdk/openai@{sdk_version}" if sdk_version else "@ai-sdk/openai"
    print(f"Installing {sdk_pkg}...")
    result = subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", sdk_pkg],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home)}
    )
    if result.returncode == 0:
        print(f"@ai-sdk/openai@{sdk_version or 'latest'} installed (Responses API support)")
    else:
        print(f"@ai-sdk/openai install warning: {result.stderr[:500]}")
else:
    print(f"OpenCode CLI already installed at {opencode_bin}")

# 2. Skip auth config if no token (will be configured after PAT setup)
if not host or not token:
    print("OpenCode CLI installed — config will be set after PAT setup")
    exit(0)

# Strip trailing slash and ensure https:// prefix
host = ensure_https(host.rstrip("/"))

gateway_host = get_gateway_host()
gateway_token = os.environ.get("DATABRICKS_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: AI Gateway resolved but DATABRICKS_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    print(f"Using Databricks Host: {host}")

# 3. Write global opencode.json config
# OpenCode looks for config at ~/.config/opencode/opencode.json (global)
# and ./opencode.json (project-level)
opencode_config_dir = home / ".config" / "opencode"
opencode_config_dir.mkdir(parents=True, exist_ok=True)

# Build the MCP server dict once, honouring enterprise overrides — empty
# DEEPWIKI_MCP_URL / EXA_MCP_URL drops the corresponding server (F-04).
from enterprise_config import deepwiki_mcp_url, exa_mcp_url

_mcp_servers = {}
if dw_url := deepwiki_mcp_url():
    _mcp_servers["deepwiki"] = {
        "type": "remote",
        "url": dw_url,
        "enabled": True,
        "oauth": False,
    }
if exa_url := exa_mcp_url():
    _mcp_servers["exa"] = {
        "type": "remote",
        "url": exa_url,
        "enabled": True,
    }

CTX_200K = {"context": 200000, "output": 8192}
CTX_200K_LONG = {"context": 200000, "output": 16384}
CTX_1M = {"context": 1000000, "output": 8192}

PROXY_MODELS = {
    "databricks-claude-opus-4-6":      {"name": "Claude Opus 4.6 (Databricks)",      "limit": CTX_200K_LONG},
    "databricks-claude-sonnet-4-6":    {"name": "Claude Sonnet 4.6 (Databricks)",    "limit": CTX_200K},
    "databricks-claude-haiku-4-5":     {"name": "Claude Haiku 4.5 (Databricks)",     "limit": CTX_200K},
    "databricks-gemini-2-5-flash":     {"name": "Gemini 2.5 Flash (Databricks)",     "limit": CTX_1M},
    "databricks-gemini-2-5-pro":       {"name": "Gemini 2.5 Pro (Databricks)",       "limit": CTX_1M},
    "databricks-gemini-3-5-flash":     {"name": "Gemini 3.5 Flash (Databricks)",     "limit": CTX_1M},
    "databricks-gemini-3-1-flash-lite":{"name": "Gemini 3.1 Flash Lite (Databricks)","limit": CTX_1M},
}

OPENAI_MODELS = {
    "databricks-gpt-5-3-codex": {"name": "GPT 5.3 Codex (Databricks)", "limit": CTX_200K_LONG},
    "databricks-gpt-5-2-codex": {"name": "GPT 5.2 Codex (Databricks)", "limit": CTX_200K_LONG},
}

import urllib.request

_UA = {"User-Agent": "coda-setup/1.0"}


def _fetch_served_chat_models(host_url, bearer):
    """Return {endpoint_name: display_name} for ready llm/v1/chat endpoints."""
    req = urllib.request.Request(
        f"{host_url}/api/2.0/serving-endpoints",
        headers={"Authorization": f"Bearer {bearer}", **_UA},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    served = {}
    for ep in (data.get("endpoints") or []):
        if ep.get("task") != "llm/v1/chat":
            continue
        if (ep.get("state") or {}).get("ready") != "READY":
            continue
        name = ep.get("name") or ""
        if not name.startswith("databricks-"):
            continue
        fm = ((ep.get("config") or {}).get("served_entities") or [{}])[0].get("foundation_model") or {}
        served[name] = fm.get("display_name") or name
    return served


def _fetch_models_dev_databricks_ids():
    req = urllib.request.Request("https://models.dev/api.json", headers=_UA)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return set((data.get("databricks") or {}).get("models", {}).keys())


try:
    _served = _fetch_served_chat_models(host, token)
    _catalog = _fetch_models_dev_databricks_ids()
    _already = set(PROXY_MODELS) | set(OPENAI_MODELS)
    _hidden = [mid for mid in _catalog if mid not in _served]
    _new = [mid for mid in _served if mid not in _catalog and mid not in _already]
    for mid in _hidden:
        PROXY_MODELS.setdefault(mid, {})["enabled"] = False
    for mid in _new:
        PROXY_MODELS[mid] = {
            "name": f"{_served[mid]} (Databricks)",
            "limit": CTX_200K,
        }
    print(f"Workspace introspection: hid {len(_hidden)} unserved catalog model(s), surfaced {len(_new)} workspace-exclusive model(s)")
except Exception as _e:
    print(f"Workspace introspection skipped: {_e}")


providers = {
    "databricks": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Databricks AI Gateway (via content-filter proxy)" if gateway_host
                else "Databricks Model Serving (via content-filter proxy)",
        "options": {"baseURL": CONTENT_FILTER_PROXY_URL},
        "models": PROXY_MODELS,
    },
}
if gateway_host:
    providers["databricks-openai"] = {
        "npm": "@ai-sdk/openai",
        "name": "Databricks AI Gateway (OpenAI)",
        "options": {"baseURL": f"{gateway_host}/openai/v1", "compatibility": "compatible"},
        "models": OPENAI_MODELS,
    }

opencode_config = {
    "$schema": "https://opencode.ai/config.json",
    "enabled_providers": list(providers.keys()),
    "provider": providers,
    "mcp": _mcp_servers,
    "model": f"databricks/{anthropic_model}",
}

config_path = opencode_config_dir / "opencode.json"
config_path.write_text(json.dumps(opencode_config, indent=2))
print(f"OpenCode configured: {config_path}")

# 4. Also create auth credentials for the databricks provider(s)
# OpenCode stores credentials at ~/.local/share/opencode/auth.json
opencode_data_dir = home / ".local" / "share" / "opencode"
opencode_data_dir.mkdir(parents=True, exist_ok=True)

if gateway_host:
    auth_data = {
        "databricks": {"type": "api", "key": gateway_token},
        "databricks-openai": {"type": "api", "key": gateway_token},
    }
else:
    auth_data = {
        "databricks": {"type": "api", "key": token},
    }

auth_path = opencode_data_dir / "auth.json"
auth_path.write_text(json.dumps(auth_data, indent=2))
auth_path.chmod(0o600)
print(f"OpenCode auth configured: {auth_path}")

print(f"\nOpenCode ready! Default model: {anthropic_model}")
print("  opencode                          # Start OpenCode TUI")
if gateway_host:
    print("  opencode -m databricks-openai/databricks-gpt-5-3-codex  # Use GPT 5.3 Codex")
print("  opencode -m databricks/databricks-gemini-2-5-flash  # Use Gemini")
print(f"  opencode -m databricks/{anthropic_model} # Use Claude (default)")
