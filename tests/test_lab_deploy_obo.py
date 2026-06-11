"""lab_deploy enables the OBO gate and provisions OBO headlessly.

Covers the corrected provisioning: camelCase REST setting-name resolution,
read-before-write on the scope allowlist, and the agentsObo gate detection/enable.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lab_deploy.py"
_spec = importlib.util.spec_from_file_location("lab_deploy_obo", _SPEC_PATH)
ld = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ld)  # type: ignore[union-attr]


# ── fakes ────────────────────────────────────────────────────────────────


class _Meta:
    def __init__(self, name):
        self.name = name


class _Scopes:
    def __init__(self, allowed):
        self.allowed_scopes = allowed


class _Setting:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _WS:
    def __init__(self, metadata_names=(), reads=None):
        self._meta = [_Meta(n) for n in metadata_names]
        self._reads = reads or {}
        self.patch_calls = []

    def list_workspace_settings_metadata(self):
        return iter(self._meta)

    def get_public_workspace_setting(self, name):
        if name in self._reads:
            return self._reads[name]
        raise RuntimeError("ResourceDoesNotExist")

    def patch_public_workspace_setting(self, name, setting):
        self.patch_calls.append((name, setting))
        return setting


class _Apps:
    def __init__(self):
        self.create_calls = []

    def create(self, *, app):
        self.create_calls.append(app)
        return app


class _Client:
    def __init__(self, ws, apps=None):
        self.workspace_settings_v2 = ws
        self.apps = apps or _Apps()


# ── env defaults ───────────────────────────────────────────────────────────


def test_default_env_enables_obo_gate():
    env = ld.default_lab_env()
    assert env["CODA_OBO_ENABLED"] == "true"
    assert env["CODA_AUTH_MODE"] == "workspace"
    assert env["CODA_PROFILE"] == "lab"


def test_default_env_is_a_copy():
    ld.default_lab_env()["CODA_PROFILE"] = "full"
    assert ld.LAB_ENV_OVERRIDES["CODA_PROFILE"] == "lab"  # constant not mutated


# ── setting-name resolution (snake/camel mismatch) ──────────────────────────


def test_resolve_setting_name_matches_camelcase():
    ws = _WS(metadata_names=["allowedAppsUserApiScopes", "agentsObo"])
    client = _Client(ws)
    # snake_case candidate must resolve to the workspace's camelCase name.
    name = ld.resolve_setting_name(client, "allowedAppsUserApiScopes",
                                   "allowed_apps_user_api_scopes")
    assert name == "allowedAppsUserApiScopes"


def test_resolve_setting_name_falls_back_to_first_candidate():
    client = _Client(_WS(metadata_names=[]))  # metadata empty
    assert ld.resolve_setting_name(client, "allowedAppsUserApiScopes") == "allowedAppsUserApiScopes"


# ── ensure_obo_scopes: read-before-write ────────────────────────────────────


def test_ensure_obo_scopes_skips_when_already_wildcard():
    reads = {"allowedAppsUserApiScopes": _Setting(
        effective_allowed_apps_user_api_scopes=_Scopes(["*"]))}
    ws = _WS(metadata_names=["allowedAppsUserApiScopes"], reads=reads)
    ld.ensure_obo_scopes(_Client(ws))
    assert ws.patch_calls == []  # already permissive → no patch


def test_ensure_obo_scopes_patches_camelcase_when_missing():
    # No read available (ResourceDoesNotExist) → must patch with camelCase name + "*".
    ws = _WS(metadata_names=["allowedAppsUserApiScopes"])
    ld.ensure_obo_scopes(_Client(ws))
    assert len(ws.patch_calls) == 1
    name, setting = ws.patch_calls[0]
    assert name == "allowedAppsUserApiScopes"
    assert setting.allowed_apps_user_api_scopes.allowed_scopes == ["*"]


# ── check_obo_gates: agentsObo is the real blocker ──────────────────────────


def test_check_obo_gates_enables_agents_obo_when_disabled():
    reads = {
        "enableOboUserApps": _Setting(effective_boolean_val=True),
        "agentsObo": _Setting(effective_boolean_val=False),
    }
    ws = _WS(metadata_names=["enableOboUserApps", "agentsObo"], reads=reads)
    report = ld.check_obo_gates(_Client(ws))
    assert report["enableOboUserApps"] is True
    # best-effort workspace enable attempted on the disabled gate
    assert any(name == "agentsObo" and getattr(s, "boolean_val", None) is True
               for name, s in ws.patch_calls)


def test_check_obo_gates_no_enable_when_already_on():
    reads = {
        "enableOboUserApps": _Setting(effective_boolean_val=True),
        "agentsObo": _Setting(effective_boolean_val=True),
    }
    ws = _WS(metadata_names=["enableOboUserApps", "agentsObo"], reads=reads)
    ld.check_obo_gates(_Client(ws))
    assert ws.patch_calls == []  # nothing to do


def test_check_obo_gates_never_raises_on_unreadable():
    # Empty metadata + reads raise → must degrade gracefully, no exception.
    report = ld.check_obo_gates(_Client(_WS()))
    assert report["agentsObo"] is None


# ── enable_obo_and_create_app (convenience entrypoint) ──────────────────────


def test_enable_obo_and_create_app_sets_scopes():
    ws = _WS(metadata_names=["allowedAppsUserApiScopes"])  # reads raise → provisions
    apps = _Apps()
    ld.enable_obo_and_create_app(_Client(ws, apps), "lab-x")
    assert len(apps.create_calls) == 1
    created = apps.create_calls[0]
    assert created.name == "lab-x"
    assert created.user_api_scopes == ld.OBO_SCOPES


def test_obo_scopes_are_granular_not_all_apis():
    # "all-apis" is NOT a valid Apps user-authorization scope — declaring it
    # leaves the app unscoped and agents 403. The critical scope for the agent
    # model/gateway call is serving.serving-endpoints.
    assert "all-apis" not in ld.OBO_SCOPES
    assert "serving.serving-endpoints" in ld.OBO_SCOPES


# ── _bool_val unwraps BooleanMessage (real settings shape) ──────────────────


class _BooleanMessage:
    def __init__(self, value):
        self.value = value


def test_bool_val_unwraps_boolean_message():
    assert ld._bool_val(_BooleanMessage(True)) is True
    assert ld._bool_val(_BooleanMessage(False)) is False
    assert ld._bool_val(True) is True
    assert ld._bool_val(None) is None


def test_check_obo_gates_enables_agents_obo_when_disabled_boolean_message():
    # The live API returns BooleanMessage(value=...), not a bare bool — the
    # disabled-gate detection must still fire.
    reads = {
        "enableOboUserApps": _Setting(effective_boolean_val=_BooleanMessage(True)),
        "agentsObo": _Setting(effective_boolean_val=_BooleanMessage(False)),
    }
    ws = _WS(metadata_names=["enableOboUserApps", "agentsObo"], reads=reads)
    report = ld.check_obo_gates(_Client(ws))
    assert report["agentsObo"] is False or any(
        name == "agentsObo" and getattr(s, "boolean_val", None) is True
        for name, s in ws.patch_calls
    )
    assert any(name == "agentsObo" for name, _ in ws.patch_calls)
