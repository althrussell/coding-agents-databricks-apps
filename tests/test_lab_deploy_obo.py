"""lab_deploy enables the OBO gate and provisions OBO scopes headlessly."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

_SPEC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lab_deploy.py"
_spec = importlib.util.spec_from_file_location("lab_deploy_obo", _SPEC_PATH)
ld = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ld)  # type: ignore[union-attr]


def test_default_env_enables_obo_gate():
    env = ld.default_lab_env()
    assert env["CODA_OBO_ENABLED"] == "true"
    assert env["CODA_AUTH_MODE"] == "workspace"
    assert env["CODA_PROFILE"] == "lab"


def test_default_env_is_a_copy():
    ld.default_lab_env()["CODA_PROFILE"] = "full"
    assert ld.LAB_ENV_OVERRIDES["CODA_PROFILE"] == "lab"  # constant not mutated


def test_ensure_obo_scopes_patches_workspace_setting():
    w = mock.MagicMock()
    ld.ensure_obo_scopes(w)
    w.workspace_settings_v2.patch_public_workspace_setting.assert_called_once()
    kwargs = w.workspace_settings_v2.patch_public_workspace_setting.call_args.kwargs
    assert kwargs["name"] == "allowed_apps_user_api_scopes"
    assert kwargs["setting"].allowed_apps_user_api_scopes.allowed_scopes == ["all-apis"]


def test_enable_obo_and_create_app_sets_scopes():
    w = mock.MagicMock()
    ld.enable_obo_and_create_app(w, "lab-x")
    # workspace-level enablement happened first
    w.workspace_settings_v2.patch_public_workspace_setting.assert_called_once()
    # per-app scope declaration
    app_arg = w.apps.create.call_args.kwargs["app"]
    assert app_arg.name == "lab-x"
    assert app_arg.user_api_scopes == ["all-apis"]
