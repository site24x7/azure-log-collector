"""Collectors sharing a resource group never use each other's storage accounts."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared.region_manager import RegionManager, is_own_account
from shared.storage_names import legacy_regional_storage_name

REGIONAL = "diag-logs-regional"
BASE = {"managed-by": "s247-diag-logs", "purpose": REGIONAL}


@pytest.fixture(autouse=True)
def me(monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", "s247-diag-func-alpha")
    monkeypatch.setenv("DIAG_STORAGE_SUFFIX", "alpha")


def test_account_tagged_for_another_collector_is_not_ours():
    assert not is_own_account("s247diageastusalpha", dict(BASE, **{"s247-collector": "s247-diag-func-beta"}),
                              REGIONAL, "eastus")
    assert is_own_account("anything", dict(BASE, **{"s247-collector": "S247-DIAG-FUNC-ALPHA"}), REGIONAL, "eastus")


def test_untagged_account_is_ours_only_by_our_generated_name():
    assert is_own_account("s247diageastusalpha", BASE, REGIONAL, "eastus")
    assert not is_own_account("s247diageastusbeta", BASE, REGIONAL, "eastus")
    # A name earlier versions cut off at 24 characters is still recognised.
    legacy = legacy_regional_storage_name("switzerlandnorth", "alpha")
    assert is_own_account(legacy, BASE, REGIONAL, "switzerlandnorth")


def test_untagged_account_without_suffix_is_ours(monkeypatch):
    monkeypatch.setenv("DIAG_STORAGE_SUFFIX", "")
    assert is_own_account("s247diageastus", BASE, REGIONAL, "eastus")


def test_wrong_purpose_or_owner_tag_is_never_ours():
    assert not is_own_account("s247diageastusalpha", {"purpose": REGIONAL}, REGIONAL, "eastus")
    assert not is_own_account("s247diageastusalpha", BASE, "diag-logs-tenant", "eastus")


def test_provisioned_regions_skip_other_collectors_and_tag_our_old_accounts():
    accounts = [
        SimpleNamespace(name="s247diageastusbeta", primary_location="eastus",
                        tags=dict(BASE, **{"s247-collector": "s247-diag-func-beta"})),
        SimpleNamespace(name="s247diageastusalpha", primary_location="eastus", tags=dict(BASE)),
        SimpleNamespace(name="s247diagwestusbeta", primary_location="westus", tags=dict(BASE)),
    ]
    rm = RegionManager.__new__(RegionManager)
    rm.subscription_id = "sub"
    rm.credential = MagicMock()
    client = MagicMock()
    client.storage_accounts.list_by_resource_group.return_value = accounts
    with patch("shared.region_manager.StorageManagementClient", return_value=client):
        regions = rm.get_provisioned_regions("rg")
    assert regions == {"eastus": "s247diageastusalpha"}          # beta's accounts are left alone
    name, update = client.storage_accounts.update.call_args.args[1:3]
    assert name == "s247diageastusalpha" and update["tags"]["s247-collector"] == "s247-diag-func-alpha"
