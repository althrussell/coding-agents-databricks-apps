#!/usr/bin/env -S uv run python
"""Idempotent, headless lab deploy of CoDA into a Databricks workspace.

This mirrors the exact SDK path Control Tower's ``RealAppDeploymentService``
uses (see ``scripts/spike_app_deploy.py`` in the control-tower repo):

    repos.create(url, provider, path[, branch])   # clone source into the WS
    apps.create_and_wait(App(name=...))           # provision the app shell
    apps.deploy_and_wait(name, AppDeployment(...)) # deploy with env_vars
    apps.update_permissions(name, [...])          # grant attendee CAN_MANAGE

The crucial difference from the old Control Tower flow: instead of patching
the app source / injecting a per-attendee ``DATABRICKS_APP_AUTHORIZED_EMAIL``,
this script sets the **lab contract** env on the deployment::

    CODA_AUTH_MODE=workspace   # any authenticated workspace user may use it
    CODA_PROFILE=lab           # lean agent footprint (Claude + AppKit + DB CLI)

Because auth is now workspace-wide, Control Tower no longer needs to know or
patch anything about CoDA's authorization model — each attendee gets their own
isolated app instance in their own workspace.

Every step is idempotent: re-running against an existing repo / app / deployment
converges rather than erroring, so this is safe to use as a retry-on-failure
provisioning primitive.

Usage (workspace-profile path — simplest, for local / single-workspace runs)::

    uv run python scripts/lab_deploy.py --profile LAB --app-name coda-lab

    # deploy from an already-synced workspace source path instead of cloning:
    uv run python scripts/lab_deploy.py --profile LAB --app-name coda-lab \\
        --source-path /Workspace/Users/me@x.com/coding-agents-databricks-apps

    # add or override deployment env vars (repeatable):
    uv run python scripts/lab_deploy.py --profile LAB \\
        --extra-env MLFLOW_TRACING_ENABLED=true

Exit code 0 on success; non-zero on any unrecoverable error.
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager
from typing import Any

# The lab contract: workspace-wide auth + lean profile + OBO agent auth. These
# are injected as deployment env vars so Control Tower never has to patch app
# source/yaml. CODA_OBO_ENABLED is on by default in lab anyway; we set it
# explicitly to document intent and make it trivial to flip off (=false → PAT).
LAB_ENV_OVERRIDES: dict[str, str] = {
    "CODA_AUTH_MODE": "workspace",
    "CODA_PROFILE": "lab",
    "CODA_OBO_ENABLED": "true",
}

# Per-APP OBO scope declaration (App.user_api_scopes). "all-apis" gives the
# coding agents the full Databricks API breadth they need to build + deploy as
# the attendee. NOTE: this is the *app-side* vocabulary and is distinct from the
# *workspace allowlist* value below.
OBO_SCOPES: list[str] = ["all-apis"]

# Workspace-level scope ALLOWLIST value (allowedAppsUserApiScopes.allowed_scopes).
# This uses a different vocabulary than the app-side scopes: "*" means "any
# scope". On live lab workspaces this is already effectively ["*"], so the patch
# below is usually a no-op (we read-before-write and skip when it already covers).
OBO_WORKSPACE_ALLOWED_SCOPES: list[str] = ["*"]

# Setting identifiers as exposed on /api/2.1/settings/{name}. The REST resource
# names are camelCase and do NOT match the snake_case SDK dataclass field names
# (e.g. dataclass field ``allowed_apps_user_api_scopes`` ↔ REST name
# ``allowedAppsUserApiScopes``). Passing the snake_case form as ``name`` 404s
# (ResourceDoesNotExist). We pass camelCase AND resolve dynamically against
# list_workspace_settings_metadata() so a rename on either side can't break us.
OBO_ALLOWLIST_SETTING_CANDIDATES = ("allowedAppsUserApiScopes", "allowed_apps_user_api_scopes")
OBO_USER_APPS_SETTING_CANDIDATES = ("enableOboUserApps", "enable_obo_user_apps")
OBO_AGENTS_SETTING_CANDIDATES = ("agentsObo", "agents_obo")

DEFAULT_GIT_URL = "https://github.com/althrussell/coding-agents-databricks-apps"


def default_lab_env() -> dict[str, str]:
    """The lab contract env as a fresh dict (safe to mutate by the caller)."""
    return dict(LAB_ENV_OVERRIDES)


@contextmanager
def _timed(label: str):
    start = time.monotonic()
    print(f"\n=== {label} ...", flush=True)
    try:
        yield
    finally:
        print(f"=== {label} took {time.monotonic() - start:.1f}s", flush=True)


def derive_repo_name(git_url: str) -> str:
    """Return the repo directory name from a git URL (``foo.git`` -> ``foo``)."""
    tail = git_url.rstrip("/").rsplit("/", 1)[-1]
    return (tail[:-4] if tail.endswith(".git") else tail) or "repo"


def merge_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Merge the lab contract with caller overrides (overrides win)."""
    merged = dict(LAB_ENV_OVERRIDES)
    if extra:
        merged.update(extra)
    return merged


def parse_extra_env(pairs: list[str] | None) -> dict[str, str]:
    """Parse repeated ``KEY=VALUE`` CLI args into a dict.

    Raises ``ValueError`` on a malformed pair so a typo fails loudly rather
    than silently dropping config.
    """
    out: dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise ValueError(f"--extra-env expects KEY=VALUE, got {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--extra-env has empty key in {raw!r}")
        out[key] = value
    return out


def build_env_vars(env: dict[str, str]) -> list[Any]:
    """Convert an env dict to a list of SDK ``EnvVar`` objects (sorted)."""
    from databricks.sdk.service.apps import EnvVar

    return [EnvVar(name=k, value=v) for k, v in sorted(env.items())]


def ensure_repo(
    client: Any,
    *,
    git_url: str,
    provider: str,
    path: str,
    branch: str | None = None,
) -> None:
    """Idempotently clone ``git_url`` into workspace ``path`` via repos.create.

    A clone that already exists is treated as success (the SDK raises an
    already-exists error which we swallow), mirroring the Control Tower spike.
    """
    kwargs: dict[str, object] = {"url": git_url, "provider": provider, "path": path}
    if branch:
        kwargs["branch"] = branch
    try:
        client.repos.create(**kwargs)
        print(f"repo cloned -> {path}")
    except Exception as exc:  # noqa: BLE001 — SDK raises typed + generic errors
        msg = str(exc).lower()
        if "exist" in msg or "already" in msg or "conflict" in msg:
            print(f"repo already present at {path} (ok)")
        else:
            raise


def _norm_setting(name: str) -> str:
    """Normalise a setting name for case/underscore-insensitive comparison."""
    return name.lower().replace("_", "")


def resolve_setting_name(client: Any, *candidates: str) -> str:
    """Return the workspace's ACTUAL setting name matching any candidate.

    The REST resource name (camelCase, used in ``/api/2.1/settings/{name}``) does
    not match the SDK dataclass field (snake_case). We list the workspace's
    settings metadata and match case/underscore-insensitively so either spelling
    resolves to the real name. Falls back to the first candidate if metadata is
    unavailable (best-effort — a wrong guess just yields a clear 404 we handle).
    """
    wanted = {_norm_setting(c) for c in candidates}
    try:
        for meta in client.workspace_settings_v2.list_workspace_settings_metadata():
            name = getattr(meta, "name", None)
            if name and _norm_setting(name) in wanted:
                return name
    except Exception as exc:  # noqa: BLE001 — metadata unavailable / not permitted
        print(f"note: could not list settings metadata ({exc}); using {candidates[0]!r}",
              file=sys.stderr)
    return candidates[0]


def _allowlist_already_covers(setting: Any, scopes: list[str]) -> bool:
    """True if the read allowlist already permits ``scopes`` (or is wildcard ``*``)."""
    eff = (getattr(setting, "effective_allowed_apps_user_api_scopes", None)
           or getattr(setting, "allowed_apps_user_api_scopes", None))
    current = list(getattr(eff, "allowed_scopes", []) or [])
    return "*" in current or set(scopes).issubset(set(current))


def ensure_obo_scopes(client: Any, *, scopes: list[str] | None = None) -> None:
    """Headlessly ensure the workspace OBO scope allowlist covers ``scopes``.

    Resolves the real (camelCase) ``allowedAppsUserApiScopes`` setting name,
    READS it first, and only PATCHES when the effective allowlist doesn't already
    cover the requested scopes (live lab workspaces are typically already ``["*"]``,
    so this is a no-op). Requires workspace-admin auth to patch. Idempotent.
    """
    from databricks.sdk.service.settingsv2 import (
        AllowedAppsUserApiScopesMessage,
        Setting,
    )

    scopes = scopes if scopes is not None else OBO_WORKSPACE_ALLOWED_SCOPES
    name = resolve_setting_name(client, *OBO_ALLOWLIST_SETTING_CANDIDATES)

    try:
        current = client.workspace_settings_v2.get_public_workspace_setting(name=name)
        if _allowlist_already_covers(current, scopes):
            print(f"workspace OBO scope allowlist '{name}' already covers {scopes}; skipping patch")
            return
    except Exception as exc:  # noqa: BLE001 — not found / not readable: try the patch
        print(f"note: could not read setting '{name}' ({exc}); attempting patch", file=sys.stderr)

    setting = Setting(
        name=name,
        allowed_apps_user_api_scopes=AllowedAppsUserApiScopesMessage(allowed_scopes=scopes),
    )
    with _timed(f"patch_public_workspace_setting(name={name!r}, scopes={scopes})"):
        client.workspace_settings_v2.patch_public_workspace_setting(name=name, setting=setting)
    print(f"workspace OBO scope allowlist '{name}' set to {scopes}")


def check_obo_gates(client: Any, *, enable_agents_obo: bool = True) -> dict[str, bool | None]:
    """Read (and best-effort enable) the boolean OBO gates that actually decide
    whether an app's AGENTS can use the forwarded user token.

    Two gates matter beyond the scope allowlist:
      - ``enableOboUserApps`` — general OBO-for-user-apps (usually already True).
      - ``agentsObo``        — agent-specific OBO. **This is the real blocker** and
        is often a one-time account/preview toggle ("On-Behalf-Of User
        Authorization"). A workspace-level patch may not stick; we try, then warn
        loudly so an operator can enable it once at the account/preview level.

    Returns a dict of effective values (None when unreadable). Never raises.
    """
    from databricks.sdk.service.settingsv2 import Setting

    report: dict[str, bool | None] = {}
    gates = (
        ("enableOboUserApps", OBO_USER_APPS_SETTING_CANDIDATES),
        ("agentsObo", OBO_AGENTS_SETTING_CANDIDATES),
    )
    resolved: dict[str, str] = {}
    for label, candidates in gates:
        name = resolve_setting_name(client, *candidates)
        resolved[label] = name
        try:
            s = client.workspace_settings_v2.get_public_workspace_setting(name=name)
            val = getattr(s, "effective_boolean_val", None)
            report[label] = val
            print(f"OBO gate '{name}': effective={val}")
        except Exception as exc:  # noqa: BLE001
            report[label] = None
            print(f"note: could not read OBO gate '{name}' ({exc})", file=sys.stderr)

    if report.get("agentsObo") is False:
        print(
            "WARNING: OBO gate 'agentsObo' is DISABLED — apps' agents cannot use "
            "the forwarded user token until it is enabled. This is typically a "
            "ONE-TIME account/preview toggle ('On-Behalf-Of User Authorization'), "
            "not a per-attendee step.",
            file=sys.stderr,
        )
        if enable_agents_obo:
            name = resolved["agentsObo"]
            try:
                client.workspace_settings_v2.patch_public_workspace_setting(
                    name=name, setting=Setting(name=name, boolean_val=True),
                )
                print(f"enabled OBO gate '{name}'=True (workspace-level)")
                report["agentsObo"] = True
            except Exception as exc:  # noqa: BLE001 — likely account/preview-scoped
                print(
                    f"WARNING: workspace-level enable of '{name}' failed ({exc}). "
                    f"Enable it once at the account/preview level.",
                    file=sys.stderr,
                )
    return report


def provision_obo(client: Any) -> None:
    """Best-effort headless OBO provisioning: scope allowlist + gate checks.

    Each step is independently guarded so a non-admin / preview-gated environment
    degrades to a clear warning rather than failing the whole deploy (agents then
    fall back to the PAT prompt).
    """
    try:
        ensure_obo_scopes(client)
    except Exception as exc:  # noqa: BLE001 — non-admin / SDK shape
        print(f"WARNING: could not ensure workspace OBO scopes ({exc}); continuing.",
              file=sys.stderr)
    check_obo_gates(client)


def enable_obo_and_create_app(client: Any, app_name: str, **app_kwargs: Any) -> Any:
    """Provision OBO at the workspace level and create the app declaring its scopes.

    Convenience entrypoint for callers (e.g. Control Tower) that want a single
    call. Order matters: OBO is provisioned BEFORE the app is created so the app's
    declared ``user_api_scopes`` are within the allowlist.
    """
    from databricks.sdk.service.apps import App

    provision_obo(client)
    return client.apps.create(
        app=App(name=app_name, user_api_scopes=list(OBO_SCOPES), **app_kwargs)
    )


def ensure_app(
    client: Any,
    app_name: str,
    *,
    user_api_scopes: list[str] | None = None,
    poll_seconds: float = 10.0,
) -> Any:
    """Idempotently ensure an app named ``app_name`` exists and is provisioned.

    - If it exists and is not mid-deletion, reuse it.
    - If it is ``DELETING``, wait for the delete to finish, then recreate.
    - Otherwise create it and wait for it to go ACTIVE.

    When ``user_api_scopes`` is given, the app is created declaring those OBO
    scopes (ignored for an already-existing app — scopes are set at create time).
    """
    from databricks.sdk.service.apps import App

    existing = _get_app(client, app_name)
    state = _compute_state(existing)
    if existing is not None and state != "DELETING":
        print(f"app '{app_name}' already exists (state={state or 'unknown'}); reusing")
        return existing

    if state == "DELETING":
        print(f"app '{app_name}' is DELETING; waiting for removal...")
        while _compute_state(_get_app(client, app_name)) == "DELETING":
            time.sleep(poll_seconds)
        print("    delete complete.")

    app_obj = App(name=app_name, user_api_scopes=user_api_scopes) if user_api_scopes \
        else App(name=app_name)
    with _timed(f"apps.create_and_wait(App(name={app_name!r}, user_api_scopes={user_api_scopes}))"):
        app = client.apps.create_and_wait(app_obj)
    print(f"app created: name={app.name} url={getattr(app, 'url', None)}")
    return app


def deploy_app(
    client: Any,
    app_name: str,
    *,
    source_path: str,
    env: dict[str, str],
) -> Any:
    """Deploy ``app_name`` from ``source_path`` with the merged lab env."""
    from databricks.sdk.service.apps import AppDeployment

    with _timed("apps.deploy_and_wait(name, AppDeployment(source_code_path, env_vars))"):
        deployment = client.apps.deploy_and_wait(
            app_name,
            AppDeployment(
                source_code_path=source_path,
                env_vars=build_env_vars(env),
            ),
        )
    print(f"deploy status={getattr(deployment, 'status', None)}")
    return deployment


def grant_manage(client: Any, app_name: str, attendee: str) -> None:
    """Grant ``attendee`` CAN_MANAGE on the app (idempotent on the API side)."""
    from databricks.sdk.service.apps import (
        AppAccessControlRequest,
        AppPermissionLevel,
    )

    client.apps.update_permissions(
        app_name,
        access_control_list=[
            AppAccessControlRequest(
                user_name=attendee,
                permission_level=AppPermissionLevel.CAN_MANAGE,
            )
        ],
    )
    print(f"granted CAN_MANAGE to {attendee}")


def deploy_lab_app(
    client: Any,
    *,
    app_name: str,
    attendee: str,
    git_url: str = DEFAULT_GIT_URL,
    git_provider: str = "gitHub",
    git_branch: str | None = None,
    source_path: str | None = None,
    extra_env: dict[str, str] | None = None,
    grant_attendee: bool = True,
    enable_obo: bool = True,
) -> Any:
    """End-to-end idempotent lab deploy. Returns the final app object.

    If ``source_path`` is given the repo clone is skipped and the app is
    deployed directly from that existing workspace path (e.g. one populated by
    ``databricks sync``). Otherwise the repo is cloned to
    ``/Workspace/Users/<attendee>/<repo>`` first.

    When ``enable_obo`` is set (default), OBO is provisioned headlessly (scope
    allowlist read-before-write + gate checks) and the app declares
    ``user_api_scopes`` so agents can act as the attendee. Provisioning is
    best-effort: it needs workspace-admin auth and the ``agentsObo`` gate may be
    account/preview-scoped, so a non-admin / not-yet-enabled run logs warnings
    and continues (the deploy still succeeds; attendees fall back to the PAT
    prompt until OBO is fully enabled).
    """
    app_scopes: list[str] | None = None
    if enable_obo:
        app_scopes = list(OBO_SCOPES)
        provision_obo(client)

    if source_path is None:
        repo_name = derive_repo_name(git_url)
        source_path = f"/Workspace/Users/{attendee}/{repo_name}"
        ensure_repo(
            client,
            git_url=git_url,
            provider=git_provider,
            path=source_path,
            branch=git_branch,
        )
    else:
        print(f"using existing source path (skipping clone): {source_path}")

    ensure_app(client, app_name, user_api_scopes=app_scopes)
    deploy_app(client, app_name, source_path=source_path, env=merge_env(extra_env))

    if grant_attendee:
        grant_manage(client, app_name, attendee)

    final = client.apps.get(app_name)
    print("\n========================================")
    print(f"APP URL: {getattr(final, 'url', None)}")
    print(f"auth mode: workspace (any authenticated workspace user)")
    print("========================================")
    return final


def _get_app(client: Any, app_name: str) -> Any:
    """Return the app object or ``None`` if it does not exist."""
    try:
        return client.apps.get(app_name)
    except Exception:  # noqa: BLE001 — not-found surfaces as a typed error
        return None


def _compute_state(app: Any) -> str | None:
    """Best-effort extraction of the app's compute state string."""
    if app is None:
        return None
    compute = getattr(app, "compute_status", None)
    state = getattr(compute, "state", None)
    if state is None:
        return None
    return getattr(state, "value", str(state))


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
    ap.add_argument("--profile", default=None, help="~/.databrickscfg profile to auth with")
    ap.add_argument("--app-name", default="coda-lab", help="Databricks App name")
    ap.add_argument(
        "--attendee",
        default=None,
        help="Lab user email (defaults to the authed user). Used for the clone "
        "path and the CAN_MANAGE grant.",
    )
    ap.add_argument("--git-url", default=DEFAULT_GIT_URL)
    ap.add_argument("--git-branch", default=None)
    ap.add_argument("--git-provider", default="gitHub")
    ap.add_argument(
        "--source-path",
        default=None,
        help="Deploy from this existing workspace path instead of cloning the repo.",
    )
    ap.add_argument(
        "--extra-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra deployment env var (repeatable). Overrides the lab defaults.",
    )
    ap.add_argument(
        "--no-grant",
        action="store_true",
        help="Skip granting the attendee CAN_MANAGE.",
    )
    args = ap.parse_args(argv)

    try:
        extra_env = parse_extra_env(args.extra_env)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = _make_client(args.profile)

    attendee = args.attendee
    if not attendee:
        attendee = client.current_user.me().user_name
        print(f"--attendee defaulted to authed user: {attendee}")

    try:
        deploy_lab_app(
            client,
            app_name=args.app_name,
            attendee=attendee,
            git_url=args.git_url,
            git_provider=args.git_provider,
            git_branch=args.git_branch,
            source_path=args.source_path,
            extra_env=extra_env,
            grant_attendee=not args.no_grant,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean non-zero exit
        print(f"\nlab deploy failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
