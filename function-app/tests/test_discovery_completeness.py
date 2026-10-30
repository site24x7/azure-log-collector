"""A partial resource discovery must never remove settings or storage."""

from unittest.mock import MagicMock, patch

from shared.azure_manager import AzureManager
from shared.region_manager import RegionManager


def _manager():
    mgr = AzureManager.__new__(AzureManager)
    mgr._diag_support_cache = {}
    mgr._monitor_clients = {}
    mgr.discovery_errors = []
    mgr.credential = MagicMock()
    return mgr


def test_failed_subscription_listing_marks_discovery_incomplete():
    mgr = _manager()
    with patch("shared.azure_manager.ResourceManagementClient", side_effect=RuntimeError("throttled")):
        assert mgr.get_all_resources(["s1"]) == []
    assert mgr.discovery_complete is False


def test_failed_support_check_is_not_cached_as_no_logs():
    mgr = _manager()
    client = MagicMock()
    client.diagnostic_settings_category.list.side_effect = RuntimeError("429")
    mgr._get_monitor_client = lambda sub: client
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/a"
    assert mgr.supports_diagnostic_logs(rid, "Microsoft.Web/sites") is False
    assert "microsoft.web/sites" not in mgr._diag_support_cache   # asked again next time
    assert mgr.discovery_complete is False


def test_incomplete_discovery_does_not_remove_storage_accounts():
    region_mgr = RegionManager.__new__(RegionManager)
    region_mgr.deprovision_storage_account = MagicMock(return_value=True)
    region_mgr.provision_storage_account = MagicMock(return_value={"region": "eastus"})
    summary = region_mgr.reconcile_regions("rg", {"eastus"}, {"westeurope": "s247diagwesteu"}, "sfx",
                                           allow_remove=False)
    region_mgr.deprovision_storage_account.assert_not_called()
    assert summary["removed"] == []
    region_mgr.reconcile_regions("rg", {"eastus"}, {"westeurope": "s247diagwesteu"}, "sfx")
    region_mgr.deprovision_storage_account.assert_called_once()


def test_subscription_list_error_marks_scan_incomplete_before_region_cleanup():
    from contextlib import ExitStack

    import pytest
    import DiagSettingsManager

    mgr = _manager()
    mgr.get_all_resources = MagicMock(return_value=[])
    regions = MagicMock()
    regions.get_active_regions.return_value = set()
    regions.get_provisioned_regions.return_value = {}
    # Stop after verifying the region cleanup gate; no later scan phases run.
    class ReachedReconciliation(Exception):
        pass

    regions.reconcile_regions.side_effect = ReachedReconciliation
    with ExitStack() as stack:
        for target, value in [
            ("shared.subscriptions.monitored_subscriptions", (["s1"], {}, "subscription list: RuntimeError")),
            ("shared.azure_manager.AzureManager", mgr),
            ("shared.region_manager.RegionManager", regions),
            ("shared.ignore_list.load_ignore_list", {}),
            ("shared.config_store.get_configured_resources", {}),
            ("shared.config_store.get_resource_inventory", {"subscriptions": ["s1"]}),
            ("DiagSettingsManager._refresh_supported_types", {}),
        ]:
            stack.enter_context(patch(target, return_value=value))
        for target in ("shared.site24x7_client.Site24x7Client", "shared.debug_logger.log_event",
                       "shared.config_store.clear_cache", "DiagSettingsManager._update_phase",
                       "DiagSettingsManager._save_early_scan_state"):
            stack.enter_context(patch(target))
        inventory = stack.enter_context(patch("shared.config_store.save_resource_inventory"))
        with pytest.raises(ReachedReconciliation):
            DiagSettingsManager.run_scan()
    assert mgr.discovery_errors == ["subscription list: RuntimeError"]
    assert mgr.discovery_complete is False
    assert regions.reconcile_regions.call_args.kwargs["allow_remove"] is False
    inventory.assert_not_called()  # keep the last complete choices


def _http_error(status, code):
    from azure.core.exceptions import HttpResponseError
    error = HttpResponseError(message=code)
    error.status_code = status
    error.error = MagicMock(code=code)
    return error


def test_type_without_diagnostic_settings_is_cached_and_keeps_discovery_complete():
    # Azure answers 400 ResourceTypeNotSupported for types such as network watchers
    mgr = _manager()
    client = MagicMock()
    client.diagnostic_settings_category.list.side_effect = _http_error(400, "ResourceTypeNotSupported")
    mgr._get_monitor_client = lambda sub: client
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Network/networkWatchers/w"
    assert mgr.supports_diagnostic_logs(rid, "Microsoft.Network/networkWatchers") is False
    assert mgr._diag_support_cache["microsoft.network/networkwatchers"] is False
    assert mgr.discovery_complete is True


def test_other_http_errors_still_mark_discovery_incomplete():
    mgr = _manager()
    client = MagicMock()
    client.diagnostic_settings_category.list.side_effect = _http_error(400, "BadRequest")
    mgr._get_monitor_client = lambda sub: client
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/a"
    assert mgr.supports_diagnostic_logs(rid, "Microsoft.Web/sites") is False
    assert "microsoft.web/sites" not in mgr._diag_support_cache
    assert mgr.discovery_complete is False
