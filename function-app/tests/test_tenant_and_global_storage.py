"""Tenant (Entra) storage never goes idle on unreadable settings; global resources use the deployment's region."""
from unittest.mock import MagicMock, patch

import pytest

import DiagSettingsManager as dsm
from shared.region_manager import RegionManager
from shared.storage_names import storage_region


def _region_mgr():
    mgr = MagicMock()
    mgr.get_tenant_storage_account.return_value = {"name": "s247diagtenantx", "id": "/sa/tenant"}
    mgr.ensure_tenant_storage_account.return_value = {"name": "s247diagtenantx", "id": "/sa/tenant"}
    mgr.get_subscription_name.return_value = "Production"
    return mgr


def test_unreadable_entra_settings_leave_the_tenant_account_as_it_is():
    mgr = _region_mgr()
    with patch("shared.config_store.get_entra_logtype_states", side_effect=ValueError("corrupt")) as read:
        account, _, unreadable = dsm._tenant_storage(mgr, "rg", "sfx", "sub")
    assert read.call_args.kwargs == {"strict": True}
    assert unreadable is True and account["id"] == "/sa/tenant"
    mgr.idle_tenant_storage_account.assert_not_called()
    mgr.ensure_tenant_storage_account.assert_not_called()


def test_no_entra_type_enabled_idles_the_tenant_account():
    mgr = _region_mgr()
    with patch("shared.config_store.get_entra_logtype_states", return_value={"auditlogs": {"enabled": False}}):
        assert dsm._tenant_storage(mgr, "rg", "sfx", "sub") == ({}, "", False)
    mgr.idle_tenant_storage_account.assert_called_once_with("rg")


def test_an_enabled_entra_type_keeps_the_tenant_account_in_use():
    mgr = _region_mgr()
    with patch("shared.config_store.get_entra_logtype_states", return_value={"auditlogs": {"enabled": True}}):
        account, name, unreadable = dsm._tenant_storage(mgr, "rg", "sfx", "sub")
    assert (account["id"], name, unreadable) == ("/sa/tenant", "Production", False)
    mgr.idle_tenant_storage_account.assert_not_called()


@pytest.mark.parametrize("location, seed, region", [
    ("global", "centralindia", "centralindia"),
    ("Global", "Central India", "centralindia"),
    ("EastUS", "centralindia", "eastus"),
    ("global", "", ""),
    ("", "centralindia", ""),
])
def test_storage_region(location, seed, region, monkeypatch):
    monkeypatch.setenv("DIAG_SEED_REGION", seed)
    assert storage_region(location) == region


def test_global_resources_use_the_deployment_region_storage(monkeypatch):
    monkeypatch.setenv("DIAG_SEED_REGION", "westeurope")
    regions = RegionManager.__new__(RegionManager)
    resources = [{"location": "global"}, {"location": "EastUS"}, {"location": ""}]
    assert regions.get_active_regions(resources) == {"westeurope", "eastus"}


def test_scan_maps_global_resources_to_the_deployment_region():
    from pathlib import Path
    source = Path(dsm.__file__).read_text()
    # Both lookups of a resource's storage account go through storage_region.
    assert source.count("provisioned_regions.get(storage_region(location)") == 2
    assert "provisioned_regions.get(location" not in source
