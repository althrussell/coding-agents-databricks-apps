#!/usr/bin/env python
"""Pin a known-good AppKit version and pre-warm the npm cache at CoDA boot.

AppKit (`@databricks/appkit`) is the HARD default app builder for CoDA-built
apps (see .claude/skills/databricks-apps-python/). This script does NOT vendor
AppKit — it is consumed via the package manager / CLI. What it does at boot:

  1. Resolve a pinned, cooldown-respected version of `@databricks/appkit`
     (supply-chain hardening, same policy as the other npm CLIs) — overridable
     via APPKIT_VERSION.
  2. Pre-warm the npm cache for the AppKit packages so the first
     `databricks apps init` / scaffold in a lab is fast and reliable even on a
     flaky network.
  3. Record the resolved version at ~/.coda/appkit-version so the app-build
     skill can confirm the exact `@databricks/appkit-ui` component API for the
     version actually installed (`npx @databricks/appkit docs`).

Idempotent and non-fatal: a network failure here must never break boot — labs
still scaffold AppKit on demand; this is a warm-start optimisation. Honors the
NPM_REGISTRY mirror because npm reads npm_config_registry from the environment,
which enterprise_config.bootstrap() has already pushed in.

Disable with ENABLE_APPKIT_PRECACHE=false (the version pin is still recorded).
"""
import os
import subprocess
from pathlib import Path

from utils import get_npm_version

APPKIT_PKG = "@databricks/appkit"
APPKIT_UI_PKG = "@databricks/appkit-ui"

# Set HOME if not properly set (matches the other setup scripts).
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


def _resolve_version() -> str | None:
    """Resolve the AppKit version to pin.

    Priority: explicit APPKIT_VERSION env override > cooldown-respected latest
    stable from the registry. Returns None if the registry lookup fails (caller
    degrades to an unpinned scaffold).
    """
    explicit = os.environ.get("APPKIT_VERSION", "").strip()
    if explicit:
        print(f"Using APPKIT_VERSION override: {explicit}")
        return explicit
    version = get_npm_version(APPKIT_PKG)
    if version:
        print(f"Resolved {APPKIT_PKG}@{version} (cooldown-respected latest stable)")
    else:
        print(
            f"Could not resolve a pinned {APPKIT_PKG} version "
            "(registry lookup failed); apps will scaffold against @latest."
        )
    return version


def _record_version(version: str) -> None:
    """Persist the pinned version for the app-build skill to read."""
    coda_dir = home / ".coda"
    coda_dir.mkdir(parents=True, exist_ok=True)
    version_file = coda_dir / "appkit-version"
    version_file.write_text(f"{version}\n")
    print(f"Recorded pinned AppKit version: {version_file} -> {version}")


def _cache_add(spec: str) -> None:
    """Best-effort `npm cache add <spec>` to pre-warm the package tarball."""
    try:
        result = subprocess.run(
            ["npm", "cache", "add", spec],
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "HOME": str(home)},
        )
        if result.returncode == 0:
            print(f"Pre-cached {spec}")
        else:
            err = (result.stderr or result.stdout).strip()
            print(f"Skipped pre-cache of {spec} (rc={result.returncode}): {err[:300]}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Skipped pre-cache of {spec}: {e}")


def main() -> int:
    version = _resolve_version()

    if version:
        _record_version(version)

    if not _truthy(os.environ.get("ENABLE_APPKIT_PRECACHE")):
        print("ENABLE_APPKIT_PRECACHE=false — skipping npm cache pre-warm.")
        return 0

    # Pre-warm the AppKit packages. appkit-ui versions independently of the
    # CLI, so resolve its own cooldown-respected pin rather than reusing the
    # CLI version (which would 404 against the ui package).
    cli_spec = f"{APPKIT_PKG}@{version}" if version else APPKIT_PKG
    _cache_add(cli_spec)

    ui_version = os.environ.get("APPKIT_UI_VERSION", "").strip() or get_npm_version(APPKIT_UI_PKG)
    ui_spec = f"{APPKIT_UI_PKG}@{ui_version}" if ui_version else APPKIT_UI_PKG
    _cache_add(ui_spec)

    print("AppKit precache step complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
