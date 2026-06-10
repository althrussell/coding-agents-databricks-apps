"""pat-status reports OBO; configure-pat remains a usable PAT fallback."""
import os
from unittest import mock


def _get_app():
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
    return app_module


def test_pat_status_configured_when_obo_token_present():
    app = _get_app()
    with mock.patch.object(app, "_agent_auth_mode", return_value="obo"), \
         mock.patch.object(type(app.obo_manager), "has_token",
                           new_callable=mock.PropertyMock, return_value=True), \
         mock.patch.object(app, "get_request_user", return_value="alice@x.com"):
        body = app.app.test_client().get("/api/pat-status").get_json()
    assert body["configured"] is True
    assert body["valid"] is True


def test_pat_status_falls_through_to_pat_without_obo_token():
    app = _get_app()
    saved = os.environ.pop("DATABRICKS_TOKEN", None)
    try:
        with mock.patch.object(app, "_agent_auth_mode", return_value="obo"), \
             mock.patch.object(type(app.obo_manager), "has_token",
                               new_callable=mock.PropertyMock, return_value=False):
            body = app.app.test_client().get("/api/pat-status").get_json()
        assert body["configured"] is False
    finally:
        if saved is not None:
            os.environ["DATABRICKS_TOKEN"] = saved


def test_configure_pat_still_usable_in_obo_mode():
    # OBO mode must not block the PAT fallback. Empty token → normal 400, not a
    # mode/owner/409 rejection.
    app = _get_app()
    with mock.patch.object(app, "_agent_auth_mode", return_value="obo"), \
         mock.patch.object(app, "_is_databricks_apps", return_value=False), \
         mock.patch.object(type(app.pat_rotator), "token",
                           new_callable=mock.PropertyMock, return_value=None):
        resp = app.app.test_client().post("/api/configure-pat", json={"token": ""})
    assert resp.status_code == 400
