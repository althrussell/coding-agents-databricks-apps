"""Unit tests for scripts/lab_deploy.py.

These exercise the pure helpers and the idempotency logic against a fake
duck-typed WorkspaceClient — no network, no real SDK calls. The lab contract
(CODA_AUTH_MODE=workspace + CODA_PROFILE=lab) is asserted end to end through
the recorded deployment env vars.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Load scripts/lab_deploy.py as a module (scripts/ isn't a package).
_SPEC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lab_deploy.py"
_spec = importlib.util.spec_from_file_location("lab_deploy", _SPEC_PATH)
lab_deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab_deploy)  # type: ignore[union-attr]


# ── pure helpers ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/althrussell/coding-agents-databricks-apps", "coding-agents-databricks-apps"),
        ("https://github.com/foo/bar.git", "bar"),
        ("https://github.com/foo/bar/", "bar"),
        ("git@github.com:foo/baz.git", "baz"),
    ],
)
def test_derive_repo_name(url, expected):
    assert lab_deploy.derive_repo_name(url) == expected


def test_merge_env_defaults_are_the_lab_contract():
    env = lab_deploy.merge_env()
    assert env["CODA_AUTH_MODE"] == "workspace"
    assert env["CODA_PROFILE"] == "lab"


def test_merge_env_overrides_win():
    env = lab_deploy.merge_env({"CODA_PROFILE": "full", "MLFLOW_TRACING_ENABLED": "true"})
    # Caller override wins over the lab default...
    assert env["CODA_PROFILE"] == "full"
    # ...but the rest of the contract is preserved + extra is merged in.
    assert env["CODA_AUTH_MODE"] == "workspace"
    assert env["MLFLOW_TRACING_ENABLED"] == "true"


def test_merge_env_does_not_mutate_module_constant():
    lab_deploy.merge_env({"CODA_AUTH_MODE": "owner"})
    assert lab_deploy.LAB_ENV_OVERRIDES["CODA_AUTH_MODE"] == "workspace"


def test_parse_extra_env_ok():
    out = lab_deploy.parse_extra_env(["A=1", "B=x=y", "C="])
    assert out == {"A": "1", "B": "x=y", "C": ""}


def test_parse_extra_env_rejects_missing_eq():
    with pytest.raises(ValueError):
        lab_deploy.parse_extra_env(["NOPE"])


def test_parse_extra_env_rejects_empty_key():
    with pytest.raises(ValueError):
        lab_deploy.parse_extra_env(["=val"])


def test_build_env_vars_sorted_and_typed():
    env_vars = lab_deploy.build_env_vars({"B": "2", "A": "1"})
    assert [e.name for e in env_vars] == ["A", "B"]
    assert [e.value for e in env_vars] == ["1", "2"]


# ── fake client ──────────────────────────────────────────────────────────


class _State:
    def __init__(self, value):
        self.value = value


class _Compute:
    def __init__(self, value):
        self.state = _State(value)


class _App:
    def __init__(self, name, state="ACTIVE", url="https://app.example"):
        self.name = name
        self.url = url
        self.compute_status = _Compute(state) if state else None


class _Apps:
    """Records calls; configurable get() behaviour for idempotency tests."""

    def __init__(self, *, existing=None, delete_after=0):
        self._existing = existing  # _App or None
        self._delete_after = delete_after  # calls to get() before DELETING clears
        self.calls = []
        self.create_calls = []
        self.deploy_calls = []
        self.perm_calls = []

    def get(self, name):
        self.calls.append(("get", name))
        if self._existing is None:
            raise RuntimeError("resource not found")
        # Simulate a DELETING app eventually disappearing.
        if (
            self._delete_after
            and lab_deploy._compute_state(self._existing) == "DELETING"
        ):
            self._delete_after -= 1
            if self._delete_after <= 0:
                self._existing = None
                raise RuntimeError("resource not found")
        return self._existing

    def create_and_wait(self, app):
        self.create_calls.append(app.name)
        created = _App(app.name)
        self._existing = created
        return created

    def deploy_and_wait(self, name, deployment):
        self.deploy_calls.append((name, deployment))

        class _D:
            status = "SUCCEEDED"

        return _D()

    def update_permissions(self, name, access_control_list):
        self.perm_calls.append((name, access_control_list))


class _Repos:
    def __init__(self, raise_exc=None):
        self._raise = raise_exc
        self.create_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._raise is not None:
            raise self._raise


class _FakeClient:
    def __init__(self, apps, repos=None):
        self.apps = apps
        self.repos = repos or _Repos()


# ── ensure_repo idempotency ──────────────────────────────────────────────


def test_ensure_repo_clones_when_absent():
    client = _FakeClient(_Apps(), _Repos())
    lab_deploy.ensure_repo(
        client, git_url="https://x/y", provider="gitHub", path="/Workspace/Users/a/y"
    )
    assert client.repos.create_calls == [
        {"url": "https://x/y", "provider": "gitHub", "path": "/Workspace/Users/a/y"}
    ]


def test_ensure_repo_swallows_already_exists():
    client = _FakeClient(_Apps(), _Repos(raise_exc=RuntimeError("Repo already exists")))
    # Must not raise.
    lab_deploy.ensure_repo(
        client, git_url="https://x/y", provider="gitHub", path="/p"
    )


def test_ensure_repo_reraises_other_errors():
    client = _FakeClient(_Apps(), _Repos(raise_exc=RuntimeError("permission denied")))
    with pytest.raises(RuntimeError, match="permission denied"):
        lab_deploy.ensure_repo(
            client, git_url="https://x/y", provider="gitHub", path="/p"
        )


def test_ensure_repo_passes_branch():
    client = _FakeClient(_Apps(), _Repos())
    lab_deploy.ensure_repo(
        client, git_url="https://x/y", provider="gitHub", path="/p", branch="dev"
    )
    assert client.repos.create_calls[0]["branch"] == "dev"


# ── ensure_app idempotency ───────────────────────────────────────────────


def test_ensure_app_creates_when_absent():
    apps = _Apps(existing=None)
    client = _FakeClient(apps)
    lab_deploy.ensure_app(client, "coda-lab")
    assert apps.create_calls == ["coda-lab"]


def test_ensure_app_reuses_when_active():
    apps = _Apps(existing=_App("coda-lab", state="ACTIVE"))
    client = _FakeClient(apps)
    lab_deploy.ensure_app(client, "coda-lab")
    assert apps.create_calls == []  # did NOT recreate


def test_ensure_app_waits_then_recreates_when_deleting():
    apps = _Apps(existing=_App("coda-lab", state="DELETING"), delete_after=2)
    client = _FakeClient(apps)
    lab_deploy.ensure_app(client, "coda-lab", poll_seconds=0)
    assert apps.create_calls == ["coda-lab"]  # recreated after delete cleared


# ── deploy_lab_app end to end (lab contract is enforced) ─────────────────


def test_deploy_lab_app_injects_lab_contract_env():
    apps = _Apps(existing=None)
    client = _FakeClient(apps, _Repos())
    lab_deploy.deploy_lab_app(
        client,
        app_name="coda-lab",
        attendee="user@example.com",
        git_url="https://github.com/althrussell/coding-agents-databricks-apps",
    )
    assert len(apps.deploy_calls) == 1
    name, deployment = apps.deploy_calls[0]
    assert name == "coda-lab"
    env = {e.name: e.value for e in deployment.env_vars}
    assert env["CODA_AUTH_MODE"] == "workspace"
    assert env["CODA_PROFILE"] == "lab"
    # Cloned to the attendee's workspace path.
    assert deployment.source_code_path == (
        "/Workspace/Users/user@example.com/coding-agents-databricks-apps"
    )
    # Attendee granted CAN_MANAGE by default.
    assert apps.perm_calls and apps.perm_calls[0][0] == "coda-lab"


def test_deploy_lab_app_skips_clone_with_source_path():
    apps = _Apps(existing=None)
    repos = _Repos()
    client = _FakeClient(apps, repos)
    lab_deploy.deploy_lab_app(
        client,
        app_name="coda-lab",
        attendee="user@example.com",
        source_path="/Workspace/Users/me/coda",
        grant_attendee=False,
    )
    assert repos.create_calls == []  # no clone
    assert apps.deploy_calls[0][1].source_code_path == "/Workspace/Users/me/coda"
    assert apps.perm_calls == []  # grant skipped


def test_deploy_lab_app_extra_env_overrides():
    apps = _Apps(existing=None)
    client = _FakeClient(apps, _Repos())
    lab_deploy.deploy_lab_app(
        client,
        app_name="coda-lab",
        attendee="u@e.com",
        extra_env={"CODA_PROFILE": "full", "FOO": "bar"},
    )
    env = {e.name: e.value for e in apps.deploy_calls[0][1].env_vars}
    assert env["CODA_PROFILE"] == "full"  # override wins
    assert env["CODA_AUTH_MODE"] == "workspace"  # contract preserved
    assert env["FOO"] == "bar"


# ── _compute_state robustness ────────────────────────────────────────────


def test_compute_state_handles_none_and_missing():
    assert lab_deploy._compute_state(None) is None
    assert lab_deploy._compute_state(_App("x", state=None)) is None
    assert lab_deploy._compute_state(_App("x", state="ACTIVE")) == "ACTIVE"
