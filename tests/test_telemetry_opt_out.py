"""Tests for the telemetry opt-out path (CODA_TELEMETRY_DISABLED).

Enterprise procurement teams (NAB, Coles, etc.) require an inventory of
every outbound data flow. The opt-out lets operators ship CoDA with no
disclosed telemetry, which is the only way to pass third-party-risk
review for regulated workspaces.
"""

from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CODA_TELEMETRY_DISABLED", raising=False)


def test_telemetry_disabled_default_false():
    from telemetry import _telemetry_disabled

    assert _telemetry_disabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
def test_telemetry_disabled_truthy_values(value, monkeypatch):
    monkeypatch.setenv("CODA_TELEMETRY_DISABLED", value)
    from telemetry import _telemetry_disabled

    assert _telemetry_disabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "maybe"])
def test_telemetry_disabled_falsy_values(value, monkeypatch):
    monkeypatch.setenv("CODA_TELEMETRY_DISABLED", value)
    from telemetry import _telemetry_disabled

    assert _telemetry_disabled() is False


def test_log_telemetry_noop_when_disabled(monkeypatch):
    """When opt-out is set, log_telemetry must not spawn the background thread."""
    monkeypatch.setenv("CODA_TELEMETRY_DISABLED", "true")
    from telemetry import log_telemetry

    with mock.patch("telemetry.threading.Thread") as mock_thread:
        log_telemetry("test_event", "1")
        mock_thread.assert_not_called()


def test_log_telemetry_fires_when_enabled(monkeypatch):
    """Default (opt-out unset) must still spawn the telemetry thread."""
    from telemetry import log_telemetry

    with mock.patch("telemetry.threading.Thread") as mock_thread:
        mock_thread.return_value.start = mock.Mock()
        log_telemetry("test_event", "1")
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()
