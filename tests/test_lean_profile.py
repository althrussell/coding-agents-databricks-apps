"""Tests for the lean lab profile: CODA_PROFILE + ENABLE_<AGENT> toggles.

Covers the pure resolution helpers (`_agent_enabled`, `_enabled_setup_steps`)
and a functional drive of the real `run_setup` under CODA_PROFILE=lab that
asserts disabled agents are skipped (not executed, and marked "skipped").
"""

from unittest import mock

import pytest


def _get_app_module():
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
        return app_module


# ---------------------------------------------------------------------------
# 1. _agent_enabled resolution
# ---------------------------------------------------------------------------


class TestAgentEnabled:
    def test_core_agents_always_enabled(self):
        app_module = _get_app_module()
        for core in ("claude", "appkit", "databricks", "git", "node"):
            assert app_module._agent_enabled(core, env={"CODA_PROFILE": "lab"}) is True

    def test_full_profile_enables_all_toggleable(self):
        app_module = _get_app_module()
        for agent in ("codex", "opencode", "gemini", "hermes"):
            assert app_module._agent_enabled(agent, env={"CODA_PROFILE": "full"}) is True

    def test_unset_profile_enables_all_toggleable(self):
        app_module = _get_app_module()
        for agent in ("codex", "opencode", "gemini", "hermes"):
            assert app_module._agent_enabled(agent, env={}) is True

    def test_lab_profile_disables_all_toggleable(self):
        app_module = _get_app_module()
        for agent in ("codex", "opencode", "gemini", "hermes"):
            assert app_module._agent_enabled(agent, env={"CODA_PROFILE": "lab"}) is False

    def test_explicit_enable_overrides_lab_profile(self):
        app_module = _get_app_module()
        env = {"CODA_PROFILE": "lab", "ENABLE_GEMINI": "true"}
        assert app_module._agent_enabled("gemini", env=env) is True
        assert app_module._agent_enabled("codex", env=env) is False

    def test_explicit_disable_overrides_full_profile(self):
        app_module = _get_app_module()
        env = {"CODA_PROFILE": "full", "ENABLE_OPENCODE": "false"}
        assert app_module._agent_enabled("opencode", env=env) is False
        assert app_module._agent_enabled("codex", env=env) is True

    @pytest.mark.parametrize("val,expected", [
        ("true", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
    ])
    def test_truthy_parsing(self, val, expected):
        app_module = _get_app_module()
        assert app_module._agent_enabled("codex", env={"ENABLE_CODEX": val}) is expected


# ---------------------------------------------------------------------------
# 2. _enabled_setup_steps filtering
# ---------------------------------------------------------------------------


class TestEnabledSetupSteps:
    def _ids(self, app_module, env):
        return [sid for sid, _cmd in app_module._enabled_setup_steps(env=env)]

    def test_full_profile_includes_everything(self):
        app_module = _get_app_module()
        ids = self._ids(app_module, {"CODA_PROFILE": "full"})
        assert set(ids) == {"claude", "codex", "opencode", "gemini", "hermes", "appkit", "databricks"}

    def test_lab_profile_keeps_only_core(self):
        app_module = _get_app_module()
        ids = self._ids(app_module, {"CODA_PROFILE": "lab"})
        assert set(ids) == {"claude", "appkit", "databricks"}
        for dropped in ("codex", "opencode", "gemini", "hermes"):
            assert dropped not in ids

    def test_lab_profile_with_one_reenabled(self):
        app_module = _get_app_module()
        ids = self._ids(app_module, {"CODA_PROFILE": "lab", "ENABLE_CODEX": "true"})
        assert set(ids) == {"claude", "codex", "appkit", "databricks"}

    def test_commands_use_uv_run(self):
        app_module = _get_app_module()
        for _sid, cmd in app_module._enabled_setup_steps(env={"CODA_PROFILE": "full"}):
            assert cmd[:3] == ["uv", "run", "python"]


# ---------------------------------------------------------------------------
# 3. Functional: run_setup under CODA_PROFILE=lab skips disabled agents
# ---------------------------------------------------------------------------


class TestRunSetupLeanProfile:
    """Drive the real run_setup with side-effecting steps stubbed to recorders,
    and assert the lab profile actually skips the disabled agents end-to-end."""

    def _reset_steps(self, app_module):
        with app_module.setup_lock:
            for step in app_module.setup_state["steps"]:
                step.update(status="pending", started_at=None, completed_at=None, error=None)
            app_module.setup_state["status"] = "pending"

    def test_lab_profile_skips_disabled_agents(self, monkeypatch):
        app_module = _get_app_module()
        self._reset_steps(app_module)

        monkeypatch.setenv("CODA_PROFILE", "lab")
        # No token → post-setup token sync is skipped.
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)

        executed = []

        def _record_run_step(step_id, command):
            executed.append(step_id)
            app_module._update_step(step_id, status="complete")

        monkeypatch.setattr(app_module, "_run_step", _record_run_step)
        monkeypatch.setattr(app_module, "_setup_git_config", lambda: None)
        monkeypatch.setattr(app_module.enterprise_config, "bootstrap", lambda *a, **k: None)
        monkeypatch.setattr("utils.resolve_and_cache_gateway", lambda: "")

        app_module.run_setup()

        # Disabled agents must NOT have been executed via _run_step…
        for disabled in ("codex", "opencode", "gemini", "hermes", "proxy"):
            assert disabled not in executed, f"{disabled} should not run under lab profile"

        # …and must be marked "skipped" in the state.
        statuses = {s["id"]: s["status"] for s in app_module.setup_state["steps"]}
        for disabled in ("codex", "opencode", "gemini", "hermes"):
            assert statuses[disabled] == "skipped", f"{disabled} should be skipped"

        # Core agents must have run.
        for core in ("claude", "appkit", "databricks"):
            assert core in executed, f"{core} should run under lab profile"

        # Overall status is complete (no errors).
        assert app_module.setup_state["status"] == "complete"

    def test_full_profile_runs_all_agents(self, monkeypatch):
        app_module = _get_app_module()
        self._reset_steps(app_module)

        monkeypatch.setenv("CODA_PROFILE", "full")
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)

        executed = []
        monkeypatch.setattr(app_module, "_run_step",
                            lambda sid, cmd: (executed.append(sid),
                                              app_module._update_step(sid, status="complete")))
        monkeypatch.setattr(app_module, "_setup_git_config", lambda: None)
        monkeypatch.setattr(app_module.enterprise_config, "bootstrap", lambda *a, **k: None)
        monkeypatch.setattr("utils.resolve_and_cache_gateway", lambda: "")

        app_module.run_setup()

        for agent in ("claude", "codex", "opencode", "gemini", "hermes", "appkit", "databricks", "proxy"):
            assert agent in executed, f"{agent} should run under full profile"
