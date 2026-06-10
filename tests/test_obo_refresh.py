"""The OBO keepalive endpoint re-captures the forwarded token."""
from unittest import mock


def _get_app():
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
    return app_module


def test_refresh_recaptures():
    app = _get_app()
    with mock.patch.object(app, "_capture_obo") as cap:
        client = app.app.test_client()
        resp = client.get("/api/obo-refresh", headers={"x-forwarded-access-token": "t9"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    cap.assert_called_once()


def test_refresh_is_whitelisted_from_auth():
    # Must not 403 even without an owner identity — it's a keepalive.
    app = _get_app()
    with mock.patch.object(app, "_capture_obo"):
        resp = app.app.test_client().get("/api/obo-refresh")
    assert resp.status_code == 200
