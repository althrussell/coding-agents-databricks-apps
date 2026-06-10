"""obo mode does not require a PAT at boot; setup waits for first capture."""
from unittest import mock


def _get_app():
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
    return app_module


def test_initialize_does_not_trigger_setup_at_boot_in_obo():
    app = _get_app()
    with mock.patch.object(app, "_agent_auth_mode", return_value="obo"), \
         mock.patch.object(app, "get_token_owner", return_value="owner@x.com"), \
         mock.patch.object(app, "log_telemetry"), \
         mock.patch.object(app, "cleanup_stale_sessions"), \
         mock.patch.object(app, "run_setup") as run_setup:
        app.initialize_app(local_dev=True)
    # In OBO mode setup is NOT kicked off at boot — it waits for token capture.
    run_setup.assert_not_called()


def test_pat_mode_resolves_when_gate_disabled():
    app = _get_app()
    assert app._agent_auth_mode(env={"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": "false"}) == "pat"
