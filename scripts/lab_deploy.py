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

# The lab contract: workspace-wide auth + lean profile. These are injected as
# deployment env vars so Control Tower never has to patch app source/yaml.
LAB_ENV_OVERRIDES: dict[str, str] = {
    "CODA_AUTH_MODE": "workspace",
    "CODA_PROFILE": "lab",
}

DEFAULT_GIT_URL = "https://github.com/althrussell/coding-agents-databricks-apps"


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


def ensure_app(client: Any, app_name: str, *, poll_seconds: float = 10.0) -> Any:
    """Idempotently ensure an app named ``app_name`` exists and is provisioned.

    - If it exists and is not mid-deletion, reuse it.
    - If it is ``DELETING``, wait for the delete to finish, then recreate.
    - Otherwise create it and wait for it to go ACTIVE.
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

    with _timed(f"apps.create_and_wait(App(name={app_name!r}))"):
        app = client.apps.create_and_wait(App(name=app_name))
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
) -> Any:
    """End-to-end idempotent lab deploy. Returns the final app object.

    If ``source_path`` is given the repo clone is skipped and the app is
    deployed directly from that existing workspace path (e.g. one populated by
    ``databricks sync``). Otherwise the repo is cloned to
    ``/Workspace/Users/<attendee>/<repo>`` first.
    """
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

    ensure_app(client, app_name)
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
