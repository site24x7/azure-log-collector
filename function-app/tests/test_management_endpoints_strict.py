"""Management endpoints that Site24x7 will call: strict input, honest failures, safe order."""

import json
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

import RemoveDiagSettings
import StopProcessing
import UpdateDisabledLogTypes
import UpdateGeneralLogType
import UpdateSettings
from shared.azure_manager import AzureManager

RES = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/a"


def _req(body, method="PUT"):
    return func.HttpRequest(method=method, url="/api/x", body=json.dumps(body).encode(), headers={})


# ── strict booleans / integers ──────────────────────────────────────────────

@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_processing_toggle_needs_a_json_boolean(value):
    with patch("shared.azure_manager.AzureManager") as mgr:
        resp = StopProcessing.main(_req({"enabled": value}))
    assert resp.status_code == 400
    mgr.return_value.update_app_setting.assert_not_called()


@pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 0}, [True]])
def test_general_logtype_toggle_needs_a_json_boolean(body):
    with patch("shared.azure_manager.AzureManager") as mgr:
        resp = UpdateGeneralLogType.main(_req(body))
    assert resp.status_code == 400
    mgr.return_value.update_app_setting.assert_not_called()


@pytest.mark.parametrize("body", [
    {"key": "AUTO_SCAN_ENABLED", "value": "no"},
    {"key": "AUTO_SCAN_ENABLED", "value": 1},
    {"key": "SAFE_DELETE_MAX_AGE_DAYS", "value": "14"},
    {"key": "SAFE_DELETE_MAX_AGE_DAYS", "value": True},
    {"key": ["AUTO_SCAN_ENABLED"], "value": True},
    ["AUTO_SCAN_ENABLED", True],
])
def test_settings_need_exact_types(body):
    with patch("shared.azure_manager.AzureManager") as mgr:
        resp = UpdateSettings.main(_req(body))
    assert resp.status_code == 400
    mgr.return_value.update_app_setting.assert_not_called()


def test_settings_accept_real_values():
    with patch("shared.azure_manager.AzureManager") as mgr, patch("shared.debug_logger.log_audit"):
        mgr.return_value.update_app_setting.return_value = True
        assert UpdateSettings.main(_req({"key": "AUTO_SCAN_ENABLED", "value": False})).status_code == 200
        assert UpdateSettings.main(_req({"key": "SAFE_DELETE_MAX_AGE_DAYS", "value": 14})).status_code == 200


# ── disabled log types ───────────────────────────────────────────────────────

def _disable(configured, disabled_ok=True, deleted=True):
    mgr = MagicMock()
    mgr.return_value.delete_own_settings.return_value = deleted
    with patch("shared.config_store.disable_log_type", return_value=disabled_ok), \
         patch("shared.config_store.delete_logtype_config"), \
         patch("shared.config_store.get_configured_resources", return_value=configured), \
         patch("shared.config_store.unmark_resource_configured", return_value=True) as unmark, \
         patch("shared.config_store.get_disabled_log_types", return_value=["AuditEvent"]), \
         patch("shared.azure_manager.AzureManager", mgr):
        resp = UpdateDisabledLogTypes.main(_req({"action": "disable", "category": "AuditEvent"}, "POST"))
    return resp, mgr.return_value.delete_own_settings, unmark


def test_failed_disable_is_reported_and_removes_nothing():
    resp, delete, _ = _disable({RES: {"categories": ["AuditEvent"]}}, disabled_ok=False)
    assert resp.status_code == 500
    delete.assert_not_called()


def test_resource_with_other_categories_keeps_its_setting_and_record():
    resp, delete, unmark = _disable({RES: {"categories": ["AuditEvent", "AppServiceHTTPLogs"]}})
    assert resp.status_code == 200
    delete.assert_not_called()
    unmark.assert_not_called()   # re-enabling needs no rediscovery


def test_only_category_removes_setting_and_record():
    resp, delete, unmark = _disable({RES: {"categories": ["AuditEvent"]}})
    assert json.loads(resp.get_body())["diag_settings_removed"] == 1
    unmark.assert_called_once_with(RES)


def test_failed_enable_is_reported():
    with patch("shared.config_store.enable_log_type", return_value=False), \
         patch("shared.azure_manager.AzureManager"):
        resp = UpdateDisabledLogTypes.main(_req({"action": "enable", "category": "AuditEvent"}, "POST"))
    assert resp.status_code == 500


# ── remove all diagnostic settings ───────────────────────────────────────────

def _remove(scan_running=False, auto_scan_off=True, summary=None):
    mgr = MagicMock()
    mgr.return_value.update_app_setting.return_value = auto_scan_off
    mgr.return_value.remove_all_diagnostic_settings.return_value = summary or {
        "removed": 1, "skipped": 0, "errors": 0, "unresolved": 0, "details": [], "resolved_ids": [RES]}
    with patch.dict("os.environ", {"SUBSCRIPTION_IDS": "s1"}), \
         patch("shared.config_store.get_scan_state", return_value={"in_progress": scan_running}), \
         patch("shared.config_store.get_configured_resources", return_value={RES: {}}), \
         patch("shared.config_store.unmark_resource_configured", return_value=True) as unmark, \
         patch("shared.azure_manager.AzureManager", mgr), \
         patch("shared.debug_logger.log_audit"):
        resp = RemoveDiagSettings.main(_req({}, "POST"))
    return resp, mgr.return_value, unmark


def test_removal_refused_while_a_scan_runs():
    resp, mgr, _ = _remove(scan_running=True)
    assert resp.status_code == 409
    mgr.remove_all_diagnostic_settings.assert_not_called()


def test_auto_scan_is_turned_off_before_anything_is_removed():
    resp, mgr, _ = _remove(auto_scan_off=False)
    assert resp.status_code == 500
    mgr.remove_all_diagnostic_settings.assert_not_called()


def test_only_resolved_records_are_cleared():
    resp, _, unmark = _remove(summary={"removed": 1, "skipped": 0, "errors": 1, "unresolved": 2,
                                       "details": [], "resolved_ids": [RES]})
    unmark.assert_called_once_with(RES)
    body = json.loads(resp.get_body())
    assert body["tracking_cleared"] is False and "resolved_ids" not in body


def test_failed_lookup_counts_as_error_not_absent():
    mgr = AzureManager.__new__(AzureManager)
    mgr.get_all_resources = lambda subs: [{"id": RES, "location": "westeurope"}]

    def lookup(rid, name=None, raise_errors=False):
        raise RuntimeError("throttled")
    mgr.get_diagnostic_setting = lookup
    summary = mgr.remove_all_diagnostic_settings(["s1"], {RES: {}, RES + "-gone": {}})
    assert (summary["errors"], summary["resolved_ids"], summary["unresolved"]) == (1, [], 1)
