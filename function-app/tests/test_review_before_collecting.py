"""New deployments only discover resources until an admin starts collecting."""

import json
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

import DiagSettingsManager
import StartCollecting
from shared import config_store, registration

RESOURCES = [
    {"id": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/a",
     "location": "westeurope", "type": "Microsoft.Web/sites", "resource_group": "rg"},
    {"id": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/b",
     "location": "eastus", "type": "Microsoft.KeyVault/vaults", "resource_group": "rg"},
]


class TestIsCollectionActive:
    def test_existing_installs_without_the_setting_keep_collecting(self, monkeypatch):
        monkeypatch.delenv("REVIEW_BEFORE_COLLECTING", raising=False)
        assert config_store.is_collection_active() is True

    def test_new_deployment_waits_until_approved(self, monkeypatch):
        monkeypatch.setenv("REVIEW_BEFORE_COLLECTING", "true")
        with patch.object(config_store, "_read_blob", return_value=None):
            assert config_store.is_collection_active() is False
        with patch.object(config_store, "_read_blob", return_value=json.dumps({"approved": True})):
            assert config_store.is_collection_active() is True

    def test_unreadable_approval_stays_read_only(self, monkeypatch):
        monkeypatch.setenv("REVIEW_BEFORE_COLLECTING", "true")
        with patch.object(config_store, "_read_blob", side_effect=RuntimeError("throttled")):
            assert config_store.is_collection_active() is False


def test_review_scan_changes_nothing_and_reports_the_plan(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "s1")
    region_mgr = MagicMock()
    azure_mgr = MagicMock()
    azure_mgr.return_value.get_all_resources.return_value = RESOURCES
    with patch("shared.azure_manager.AzureManager", azure_mgr), \
         patch("shared.region_manager.RegionManager", return_value=region_mgr), \
         patch("shared.site24x7_client.Site24x7Client"), \
         patch("shared.ignore_list.load_ignore_list", return_value={}), \
         patch("shared.config_store.get_configured_resources", return_value={}), \
         patch("shared.config_store.clear_cache"), \
         patch("shared.config_store.is_collection_active", return_value=False), \
         patch("shared.config_store.save_scan_state") as save_state, \
         patch("shared.debug_logger.log_event"), \
         patch.object(DiagSettingsManager, "_refresh_supported_types", return_value={}), \
         patch.object(DiagSettingsManager, "_update_phase"), \
         patch.object(DiagSettingsManager, "_save_early_scan_state"):
        result = DiagSettingsManager.run_scan()

    assert result["review_pending"] is True
    assert (result["active_resources"], result["planned_regions"], result["planned_resource_types"]) == (2, 2, 2)
    # Nothing created: no storage accounts, no diagnostic settings.
    region_mgr.reconcile_regions.assert_not_called()
    azure_mgr.return_value.create_diagnostic_setting.assert_not_called()
    state = save_state.call_args.args[0]
    assert state["review_pending"] is True and state["in_progress"] is False


def _start(active, approved=True):
    queue = MagicMock()
    with patch("shared.config_store.is_collection_active", return_value=active), \
         patch("shared.config_store.approve_collection", return_value=approved) as approve, \
         patch("shared.debug_logger.log_audit"), \
         patch("shared.registration.register_with_site24x7"):
        resp = StartCollecting.main(func.HttpRequest(method="POST", url="/api/start-collecting", body=b"", headers={}), queue)
    return resp, approve, queue


def test_start_collecting_records_approval_and_queues_a_scan():
    resp, approve, queue = _start(active=False)
    assert resp.status_code == 202
    approve.assert_called_once()
    queue.set.assert_called_once()


def test_start_collecting_when_already_collecting_does_nothing():
    resp, approve, queue = _start(active=True)
    assert resp.status_code == 200
    approve.assert_not_called()
    queue.set.assert_not_called()


def test_failed_approval_is_reported_and_starts_nothing():
    resp, _, queue = _start(active=False, approved=False)
    assert resp.status_code == 500
    queue.set.assert_not_called()


def test_registration_reports_review_mode(monkeypatch):
    with patch("shared.config_store.get_scan_state", return_value={"planned_regions": 3, "planned_resource_types": 7}), \
         patch("shared.config_store.get_configured_resources", return_value={}), \
         patch("shared.config_store.is_collection_active", return_value=False), \
         patch("shared.debug_logger.get_processing_stats", return_value=[]):
        status = registration.build_status_summary()
    assert (status["review_pending"], status["planned_regions"], status["planned_resource_types"]) == (True, 3, 7)
