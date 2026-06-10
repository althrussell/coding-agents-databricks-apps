"""obo mode captures the forwarded token on requests and starts setup once."""
from unittest import mock


def _get_app(env):
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
    return app_module, env


# OBO is on by default in lab; pat mode = explicitly disabled gate.
_OBO_ENV = {"CODA_PROFILE": "lab"}
_PAT_ENV = {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": "false"}


def test_capture_helper_pumps_and_triggers_setup_first_time():
    with mock.patch("app.initialize_app"):
        import app
    with mock.patch.object(app, "_agent_auth_mode", return_value="obo"), \
         mock.patch.object(app.obo_manager, "update_from_headers", return_value=True), \
         mock.patch.object(app, "_maybe_trigger_setup") as trig:
        app._capture_obo({"x-forwarded-access-token": "t1"})
        trig.assert_called_once()


def test_capture_no_change_does_not_trigger_setup():
    with mock.patch("app.initialize_app"):
        import app
    with mock.patch.object(app, "_agent_auth_mode", return_value="obo"), \
         mock.patch.object(app.obo_manager, "update_from_headers", return_value=False), \
         mock.patch.object(app, "_maybe_trigger_setup") as trig:
        app._capture_obo({"x-forwarded-access-token": "t1"})
        trig.assert_not_called()


def test_capture_noop_in_pat_mode():
    with mock.patch("app.initialize_app"):
        import app
    with mock.patch.object(app, "_agent_auth_mode", return_value="pat"), \
         mock.patch.object(app.obo_manager, "update_from_headers") as upd:
        app._capture_obo({"x-forwarded-access-token": "t1"})
        upd.assert_not_called()
