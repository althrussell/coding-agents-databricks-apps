"""CODA_OBO_ENABLED gate (on by default, lab-only) and the derived auth mode.

OBO lets the coding-agent CLIs authenticate as the attendee via the forwarded
user token. It is gated to lab mode (CoDA is lab-first, so unset profile resolves
to lab) and is ON by default there; the full profile always uses PAT.
"""

from unittest import mock


def _get_app_module():
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
        return app_module


class TestOboGate:
    def test_on_by_default_in_lab(self):
        app = _get_app_module()
        assert app._obo_enabled(env={"CODA_PROFILE": "lab"}) is True
        assert app._agent_auth_mode(env={"CODA_PROFILE": "lab"}) == "obo"

    def test_on_by_default_when_profile_unset(self):
        # Lab-first: an unset CODA_PROFILE resolves to lab, so OBO is on by default.
        app = _get_app_module()
        assert app._obo_enabled(env={}) is True
        assert app._agent_auth_mode(env={}) == "obo"

    def test_can_be_disabled_in_lab(self):
        app = _get_app_module()
        env = {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": "false"}
        assert app._obo_enabled(env=env) is False
        assert app._agent_auth_mode(env=env) == "pat"

    def test_disabled_values(self):
        app = _get_app_module()
        for val in ("false", "0", "no", "off", "FALSE", "Off"):
            env = {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": val}
            assert app._obo_enabled(env=env) is False

    def test_empty_value_is_treated_as_default_on(self):
        # An empty CODA_OBO_ENABLED is "unset" → default ON in lab.
        app = _get_app_module()
        assert app._obo_enabled(env={"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": ""}) is True

    def test_enabled_values(self):
        app = _get_app_module()
        for val in ("true", "1", "yes", "on", "TRUE"):
            env = {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": val}
            assert app._obo_enabled(env=env) is True

    def test_ignored_outside_lab_even_if_on(self):
        # OBO must NOT engage outside lab mode, even if requested explicitly.
        app = _get_app_module()
        env = {"CODA_PROFILE": "full", "CODA_OBO_ENABLED": "true"}
        assert app._obo_enabled(env=env) is False
        assert app._agent_auth_mode(env=env) == "pat"

    def test_full_profile_is_pat_by_default(self):
        app = _get_app_module()
        assert app._obo_enabled(env={"CODA_PROFILE": "full"}) is False
        assert app._agent_auth_mode(env={"CODA_PROFILE": "full"}) == "pat"
