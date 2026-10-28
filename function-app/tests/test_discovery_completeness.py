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
