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
    with patch("shared.config_store.save_processing_enabled", return_value=False), \
         patch("shared.registration.register_with_site24x7") as register:
        resp = StopProcessing.main(_req({"enabled": False}))
    assert resp.status_code == 500
    register.assert_not_called()


def test_stop_processing_saves_shared_flag_without_arm_or_registration():
    with patch("shared.config_store.save_processing_enabled", return_value=True) as save, \
         patch("shared.azure_manager.AzureManager") as arm, \
         patch("shared.registration.register_with_site24x7") as register:
        resp = StopProcessing.main(_req({"enabled": False}))
    assert resp.status_code == 200
    assert json.loads(resp.get_body())["enabled"] is False
    save.assert_called_once_with(False)
    arm.assert_not_called()
    register.assert_not_called()


def test_general_logtype_reports_a_failed_save():
    with patch("shared.azure_manager.AzureManager") as mgr:
        mgr.return_value.update_app_setting.return_value = False
        resp = UpdateGeneralLogType.main(_req({"enabled": True}))
    assert resp.status_code == 500
