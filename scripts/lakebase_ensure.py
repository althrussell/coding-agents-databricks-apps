#!/usr/bin/env -S uv run python
"""On-demand Lakebase (Databricks managed Postgres) provisioner for CoDA.

This is **not** run at boot. The coding agent runs it during a build, and only
when the app it is helping the user build actually needs persistence (CRUD
records, user prefs, saved state). Apps that don't need a database never touch
this and never incur Lakebase cost.

What it does, idempotently:

  1. Resolve the instance name (``--name`` / ``LAKEBASE_INSTANCE_NAME`` /
     default ``coda-lab``). The name is deterministic so a lab reuses ONE
     instance across multiple apps, and so Control Tower can find + tear it
     down later without any CoDA-side bookkeeping.
  2. ``database.get_database_instance(name)`` — if it already exists, reuse it
     (wait until AVAILABLE if it is still starting). No second instance is ever
     created.
  3. If absent, ``create_database_instance_and_wait(...)`` with a small default
     capacity, tagged with any Control-Tower-injected ``LAB_RESOURCE_TAGS`` so
     CT can attribute cost and clean up.
  4. Record ``~/.coda/lakebase.json`` ``{name, state, read_write_dns,
     database_name}`` and print the exact non-interactive ``databricks apps
     init`` resource binding to use, so the agent never triggers the
     interactive "missing required resource Postgres" prompt.

Provisioning a new instance can take a few minutes; because this only runs when
an app genuinely needs a database, blocking is acceptable — progress is printed
so the agent can relay status to the user.

Exit code 0 on success; non-zero with a human-readable message on
permission/quota failure (so the agent can explain and fall back rather than
leaving a half-bound app).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_INSTANCE_NAME = "coda-lab"
DEFAULT_CAPACITY = "CU_1"
# Lakebase provisions a default database in each instance; AppKit binds to it.
DEFAULT_DATABASE_NAME = "databricks_postgres"


def _home() -> Path:
    home = os.environ.get("HOME")
    if not home or home == "/":
        home = "/app/python/source_code"
    return Path(home)


def resolve_instance_name(arg: str | None = None, env: dict | None = None) -> str:
    """Resolve the instance name: arg > LAKEBASE_INSTANCE_NAME > default."""
    env = env if env is not None else os.environ
    name = (arg or "").strip() or env.get("LAKEBASE_INSTANCE_NAME", "").strip()
    return name or DEFAULT_INSTANCE_NAME


def parse_tags(env: dict | None = None) -> list[Any]:
    """Parse ``LAB_RESOURCE_TAGS`` (``k=v,k2=v2``) into SDK ``CustomTag`` objects.

    Control Tower injects this so the created instance is attributable for cost
    and discoverable for teardown. Empty / unset yields no tags. Malformed
    pairs are skipped (never fail a provision over a tag typo).
    """
    env = env if env is not None else os.environ
    raw = env.get("LAB_RESOURCE_TAGS", "").strip()
    if not raw:
        return []
    from databricks.sdk.service.database import CustomTag

    tags: list[Any] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if key:
            tags.append(CustomTag(key=key, value=value.strip()))
    return tags


def _state(instance: Any) -> str | None:
    """Best-effort extraction of the instance state string."""
    if instance is None:
        return None
    state = getattr(instance, "state", None)
    if state is None:
        return None
    return getattr(state, "value", str(state))


def _get_instance(client: Any, name: str) -> Any:
    """Return the instance or ``None`` if it does not exist.

    A genuine NotFound returns None (provision path). Any OTHER error
    (permission, quota, network) is re-raised so we never mistake an auth
    failure for "needs creating".
    """
    from databricks.sdk.errors import NotFound

    try:
        return client.database.get_database_instance(name)
    except NotFound:
        return None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg or "resource_does_not_exist" in msg:
            return None
        raise


def ensure_instance(
    client: Any,
    name: str,
    *,
    capacity: str = DEFAULT_CAPACITY,
    tags: list[Any] | None = None,
    poll_seconds: float = 10.0,
) -> Any:
    """Idempotently ensure a Lakebase instance ``name`` exists and is AVAILABLE.

    Reuses an existing instance (waiting for it to finish starting if needed);
    creates one only when absent. Returns the instance object.
    """
    from databricks.sdk.service.database import DatabaseInstance

    existing = _get_instance(client, name)
    if existing is not None:
        state = _state(existing)
        if state == "AVAILABLE":
            print(f"Lakebase instance '{name}' already available; reusing.")
            return existing
        print(f"Lakebase instance '{name}' exists (state={state}); waiting until available...")
        return _wait_available(client, name, poll_seconds=poll_seconds)

    print(
        f"Provisioning Lakebase instance '{name}' (capacity={capacity}). "
        "This usually takes a few minutes..."
    )
    instance = DatabaseInstance(name=name, capacity=capacity)
    if tags:
        instance.custom_tags = tags
    created = client.database.create_database_instance_and_wait(instance)
    print(f"Lakebase instance '{name}' is now {_state(created)}.")
    return created


def _wait_available(client: Any, name: str, *, poll_seconds: float = 10.0) -> Any:
    """Wait until the instance reports AVAILABLE.

    Prefers the SDK waiter; falls back to manual polling for fakes/older SDKs.
    """
    waiter = getattr(client.database, "wait_get_database_instance_database_available", None)
    if callable(waiter):
        return waiter(name)
    while True:
        inst = client.database.get_database_instance(name)
        if _state(inst) == "AVAILABLE":
            return inst
        time.sleep(poll_seconds)


def binding(instance: Any, name: str, *, database_name: str = DEFAULT_DATABASE_NAME) -> dict:
    """Build the binding record persisted to ~/.coda/lakebase.json."""
    return {
        "name": name,
        "state": _state(instance),
        "read_write_dns": getattr(instance, "read_write_dns", None),
        "database_name": database_name,
    }


def write_binding(data: dict, *, path: Path | None = None) -> Path:
    """Persist the binding so the app-build skill can read it."""
    target = path or (_home() / ".coda" / "lakebase.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return target


def _print_binding_guidance(data: dict) -> None:
    print("\n========================================")
    print(f"Lakebase ready: instance={data['name']}  database={data['database_name']}")
    print(f"  read_write_dns: {data.get('read_write_dns')}")
    print("\nBind it non-interactively when scaffolding the app, e.g.:")
    print(
        f"  databricks apps init --name <app> --features=lakebase --auto-approve \\\n"
        f"    --set lakebase.database.instance_name={data['name']} \\\n"
        f"    --set lakebase.database.database_name={data['database_name']}"
    )
    print(
        "\n(--set keys are <plugin>.database.<field>; confirm the plugin/resource "
        "key for the template with `databricks apps init --help`. With "
        "--auto-approve, the DB resource is configured only because these --set "
        "values are provided.)"
    )
    print("========================================")


def _make_client(profile: str | None) -> Any:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    try:
        from telemetry import set_product_info

        set_product_info(w)
    except Exception:
        pass
    return w


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default=None, help="Lakebase instance name (default: coda-lab)")
    ap.add_argument("--capacity", default=DEFAULT_CAPACITY, help="Instance capacity (default: CU_1)")
    ap.add_argument("--database-name", default=DEFAULT_DATABASE_NAME)
    ap.add_argument("--profile", default=None, help="~/.databrickscfg profile to auth with")
    args = ap.parse_args(argv)

    name = resolve_instance_name(args.name)
    client = _make_client(args.profile)

    try:
        instance = ensure_instance(client, name, capacity=args.capacity, tags=parse_tags())
    except Exception as exc:  # noqa: BLE001 — clean non-zero exit for the agent
        print(
            f"\nCould not provision Lakebase instance '{name}': {exc}\n"
            "If this is a permissions error, the deploying identity needs the "
            "database-create entitlement on this workspace. The app can still be "
            "built without persistence — ask the user whether they want to "
            "proceed without a database.",
            file=sys.stderr,
        )
        return 1

    data = binding(instance, name, database_name=args.database_name)
    target = write_binding(data)
    print(f"Recorded binding: {target}")
    _print_binding_guidance(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
