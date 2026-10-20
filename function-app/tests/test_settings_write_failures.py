"""A setting that Azure did not save must not be reported as saved (Site24x7 relays these answers)."""

import json
from unittest.mock import MagicMock, patch

import azure.functions as func

import StopProcessing
import UpdateGeneralLogType


def _req(body):
    return func.HttpRequest(method="PUT", url="/api/x", body=json.dumps(body).encode(), headers={})


def test_stop_processing_reports_a_failed_save(monkeypatch):
    monkeypatch.setenv("PROCESSING_ENABLED", "true")
    with patch("shared.azure_manager.AzureManager") as mgr, \
         patch("shared.registration.register_with_site24x7") as register:
        mgr.return_value.update_app_setting.return_value = False
        resp = StopProcessing.main(_req({"enabled": False}))
    assert resp.status_code == 500
    assert StopProcessing.os.environ["PROCESSING_ENABLED"] == "true"  # local state unchanged
    assert not register.called


def test_stop_processing_succeeds_when_saved_and_reports_the_new_state():
    with patch("shared.azure_manager.AzureManager") as mgr, \
         patch("shared.registration.register_with_site24x7") as register:
        mgr.return_value.update_app_setting.return_value = True
        resp = StopProcessing.main(_req({"enabled": False}))
    assert resp.status_code == 200
    assert register.called  # Site24x7 sees "paused" without waiting for the next scan


def test_general_logtype_reports_a_failed_save():
    with patch("shared.azure_manager.AzureManager") as mgr:
        mgr.return_value.update_app_setting.return_value = False
        resp = UpdateGeneralLogType.main(_req({"enabled": True}))
    assert resp.status_code == 500
