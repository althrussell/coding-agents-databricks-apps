"""OBO user-token capture + pump into agent configs."""
import os
from unittest import mock


class TestCapture:
    def test_update_from_headers_stores_token(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            changed = m.update_from_headers({"x-forwarded-access-token": "tok-1"})
        assert changed is True
        assert m.token == "tok-1"
        pump.assert_called_once_with("tok-1")

    def test_missing_header_is_noop(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            changed = m.update_from_headers({"x-forwarded-email": "a@x.com"})
        assert changed is False
        assert m.token is None
        pump.assert_not_called()

    def test_same_token_does_not_repump(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            m.update_from_headers({"x-forwarded-access-token": "tok-1"})
            m.update_from_headers({"x-forwarded-access-token": "tok-1"})
        assert pump.call_count == 1  # only re-pump on change

    def test_new_token_repumps(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            m.update_from_headers({"x-forwarded-access-token": "tok-1"})
            m.update_from_headers({"x-forwarded-access-token": "tok-2"})
        assert pump.call_count == 2
        assert m.token == "tok-2"


class TestPump:
    def test_pump_updates_env_and_clis(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch("obo_auth.update_cli_tokens") as upd, \
             mock.patch.object(m, "_write_databrickscfg") as wcfg:
            m._pump("tok-x")
        assert os.environ["DATABRICKS_TOKEN"] == "tok-x"
        upd.assert_called_once_with("tok-x")
        wcfg.assert_called_once_with("tok-x")


class TestState:
    def test_has_token_false_initially(self):
        from obo_auth import OBOTokenManager
        assert OBOTokenManager().has_token is False

    def test_has_token_true_after_capture(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump"):
            m.update_from_headers({"x-forwarded-access-token": "t"})
        assert m.has_token is True
