"""Tests for shared/region_manager.py — Azure SDK calls mocked."""

from unittest.mock import patch, MagicMock

import pytest

from shared.region_manager import (
    RegionManager,
    _sanitize_region,
    _storage_account_name,
    STORAGE_PREFIX,
    LOGS_CONTAINER,
    SAFE_DELETE_MAX_AGE_DAYS_DEFAULT,
)


# ─── Helper functions ───────────────────────────────────────────────────────


class TestSanitizeRegion:
    def test_removes_special_chars(self):
        assert _sanitize_region("East-US 2") == "eastus2"

    def test_lowercase(self):
        assert _sanitize_region("WestUS") == "westus"

    def test_already_clean(self):
        assert _sanitize_region("eastus") == "eastus"


class TestStorageAccountName:
    def test_format(self):
        name = _storage_account_name("eastus", "abc123")
        assert name == "s247diageastusabc123"
        assert name.startswith(STORAGE_PREFIX)

    def test_truncated_to_24(self):
        name = _storage_account_name("southcentralus", "abcdef123456")
        assert len(name) <= 24

    def test_sanitizes_region(self):
        name = _storage_account_name("East US 2", "suf")
        assert "eastus2" in name


# ─── RegionManager ──────────────────────────────────────────────────────────


@pytest.fixture
def rm():
    with patch("shared.region_manager.DefaultAzureCredential"):
        return RegionManager("sub-123")


class TestGetStorageNameForRegion:
    def test_static_method(self):
        name = RegionManager.get_storage_name_for_region("eastus", "abc")
        assert name == "s247diageastusabc"


class TestGetActiveRegions:
    def test_extracts_regions(self, rm):
        resources = [
            {"location": "eastus"},
            {"location": "westus"},
            {"location": "EastUS"},  # duplicate (case-insensitive)
        ]
        regions = rm.get_active_regions(resources)
        assert regions == {"eastus", "westus"}

    def test_empty_resources(self, rm):
        assert rm.get_active_regions([]) == set()

    def test_missing_location(self, rm):
        resources = [{"name": "r1"}, {"location": ""}]
        assert rm.get_active_regions(resources) == set()


class TestGetProvisionedRegions:
    def test_finds_tagged_accounts(self, rm):
        acct = MagicMock()
        acct.tags = {"managed-by": "s247-diag-logs", "purpose": "diag-logs-regional"}
        acct.primary_location = "eastus"
        acct.name = "s247diageastusabc"

        other_acct = MagicMock()
        other_acct.tags = {"managed-by": "other"}
        other_acct.primary_location = "westus"
        other_acct.name = "otheraccount"

        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.list_by_resource_group.return_value = [acct, other_acct]
            result = rm.get_provisioned_regions("my-rg")
            assert result == {"eastus": "s247diageastusabc"}

    def test_api_error(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.list_by_resource_group.side_effect = Exception("Boom")
            result = rm.get_provisioned_regions("my-rg")
            assert result == {}


class TestTenantStorageAccount:
    def _tenant_acct(self, name="s247diagtenantabc"):
        a = MagicMock()
        a.tags = {"managed-by": "s247-diag-logs", "purpose": "diag-logs-tenant"}
        a.primary_location = "eastus"
        a.name = name
        return a

    def _regional_acct(self):
        a = MagicMock()
        a.tags = {"managed-by": "s247-diag-logs", "purpose": "diag-logs-regional"}
        a.primary_location = "westus"
        a.name = "s247diagwestusx"
        return a

    def test_get_finds_tenant_account_ignoring_regional(self, rm):
        accts = [self._regional_acct(), self._tenant_acct()]
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.list_by_resource_group.return_value = accts
            result = rm.get_tenant_storage_account("my-rg")
            assert result["name"] == "s247diagtenantabc"
            assert result["id"].endswith("/storageAccounts/s247diagtenantabc")

    def test_get_empty_when_no_tenant_account(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.list_by_resource_group.return_value = [
                self._regional_acct()
            ]
            assert rm.get_tenant_storage_account("my-rg") == {}

    def test_ensure_returns_existing_without_creating(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.list_by_resource_group.return_value = [
                self._tenant_acct()
            ]
            result = rm.ensure_tenant_storage_account("my-rg", "abc")
            assert result["name"] == "s247diagtenantabc"
            # No create call when it already exists
            mock_cls.return_value.storage_accounts.begin_create.assert_not_called()

    def test_ensure_creates_when_missing(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls, \
             patch("shared.region_manager.ResourceManagementClient") as mock_rc, \
             patch.object(rm, "apply_lock"), \
             patch.object(rm, "apply_lifecycle_policy"):
            sc = mock_cls.return_value
            sc.storage_accounts.list_by_resource_group.return_value = []  # none yet
            mock_rc.return_value.resource_groups.get.return_value = MagicMock(location="eastus")
            created = MagicMock()
            created.id = "/subscriptions/sub-123/resourceGroups/my-rg/providers/Microsoft.Storage/storageAccounts/s247diagtenantabc"
            sc.storage_accounts.begin_create.return_value.result.return_value = created

            result = rm.ensure_tenant_storage_account("my-rg", "abc")
            assert result["name"] == "s247diagtenantabc"
            assert result["region"] == "eastus"
            assert result["id"].endswith("/storageAccounts/s247diagtenantabc")
            # created with the tenant tag (no region tag)
            _, kwargs = sc.storage_accounts.begin_create.call_args
            tags = kwargs["parameters"]["tags"]
            assert tags["purpose"] == "diag-logs-tenant"
            assert "region" not in tags


class TestProvisionStorageAccount:
    def test_creates_account_and_container(self, rm):
        mock_sa = MagicMock()
        mock_sa.id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/sa"

        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.begin_create.return_value.result.return_value = mock_sa
            with patch.object(rm, "apply_lock", return_value=True), \
                 patch.object(rm, "apply_lifecycle_policy", return_value=True) as alp:
                result = rm.provision_storage_account("rg", "eastus", "abc")
                assert result["storage_account_id"] == mock_sa.id
                assert result["region"] == "eastus"
                assert result["container_name"] == LOGS_CONTAINER
                mock_cls.return_value.blob_containers.create.assert_called_once()
                alp.assert_called_once()

    def test_creation_failure(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.begin_create.side_effect = Exception("Quota exceeded")
            result = rm.provision_storage_account("rg", "eastus", "abc")
            assert result["storage_account_id"] == ""


class TestApplyLifecyclePolicy:
    def test_applies_with_default_retention(self, rm):
        client = MagicMock()
        ok = rm.apply_lifecycle_policy(client, "rg", "sa1")
        assert ok is True
        client.management_policies.create_or_update.assert_called_once()
        kwargs = client.management_policies.create_or_update.call_args.kwargs
        rules = kwargs["properties"]["policy"]["rules"]
        assert rules[0]["enabled"] is True
        assert rules[0]["definition"]["filters"]["prefixMatch"] == ["insights-logs-"]
        # Default retention is 7 days
        assert (rules[0]["definition"]["actions"]["baseBlob"]["delete"]
                ["daysAfterModificationGreaterThan"] == 7)

    def test_respects_custom_retention(self, rm):
        client = MagicMock()
        rm.apply_lifecycle_policy(client, "rg", "sa1", retention_days=30)
        kwargs = client.management_policies.create_or_update.call_args.kwargs
        days = (kwargs["properties"]["policy"]["rules"][0]["definition"]
                ["actions"]["baseBlob"]["delete"]["daysAfterModificationGreaterThan"])
        assert days == 30

    def test_swallows_failures(self, rm):
        client = MagicMock()
        client.management_policies.create_or_update.side_effect = Exception("not allowed")
        # Must NOT raise
        assert rm.apply_lifecycle_policy(client, "rg", "sa1") is False


class TestDeprovisionStorageAccount:
    def test_removes_lock_and_deletes(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            with patch.object(rm, "remove_lock", return_value=True):
                with patch.object(rm, "_has_recent_blobs", return_value=False):
                    assert rm.deprovision_storage_account("rg", "eastus", "sa-name") is True
                    mock_cls.return_value.storage_accounts.delete.assert_called_once()

    def test_delete_failure(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.delete.side_effect = Exception("Locked")
            with patch.object(rm, "remove_lock", return_value=True):
                with patch.object(rm, "apply_lock", return_value=True) as mock_apply:
                    with patch.object(rm, "_has_recent_blobs", return_value=False):
                        assert rm.deprovision_storage_account("rg", "eastus", "sa-name") is False
                        # Lock must be restored after failure
                        mock_apply.assert_called_once()

    def test_skips_if_recent_blobs(self, rm):
        with patch("shared.region_manager.StorageManagementClient"):
            with patch.object(rm, "_has_recent_blobs", return_value=True):
                assert rm.deprovision_storage_account("rg", "eastus", "sa-name") is False


class TestHasRecentBlobs:
    def test_no_recent_blobs(self, rm):
        from datetime import datetime, timedelta, timezone
        mock_keys = MagicMock()
        mock_keys.keys = [MagicMock(value="key123")]
        mock_storage = MagicMock()
        mock_storage.storage_accounts.list_keys.return_value = mock_keys

        old_blob = MagicMock()
        old_blob.last_modified = datetime.now(timezone.utc) - timedelta(days=30)
        old_blob.name = "old.json"

        mock_container_client = MagicMock()
        mock_container_client.list_blobs.return_value = [old_blob]

        with patch("azure.storage.blob.BlobServiceClient") as mock_bs:
            mock_bs.from_connection_string.return_value.list_containers.return_value = [
                {"name": "insights-logs-audit"}
            ]
            mock_bs.from_connection_string.return_value.get_container_client.return_value = mock_container_client
            assert rm._has_recent_blobs("rg", "sa-name", mock_storage) is False

    def test_has_recent_blobs(self, rm):
        from datetime import datetime, timezone
        mock_keys = MagicMock()
        mock_keys.keys = [MagicMock(value="key123")]
        mock_storage = MagicMock()
        mock_storage.storage_accounts.list_keys.return_value = mock_keys

        recent_blob = MagicMock()
        recent_blob.last_modified = datetime.now(timezone.utc)
        recent_blob.name = "recent.json"

        mock_container_client = MagicMock()
        mock_container_client.list_blobs.return_value = [recent_blob]

        with patch("azure.storage.blob.BlobServiceClient") as mock_bs:
            mock_bs.from_connection_string.return_value.list_containers.return_value = [
                {"name": "insights-logs-audit"}
            ]
            mock_bs.from_connection_string.return_value.get_container_client.return_value = mock_container_client
            assert rm._has_recent_blobs("rg", "sa-name", mock_storage) is True

    def test_error_assumes_recent(self, rm):
        mock_storage = MagicMock()
        mock_storage.storage_accounts.list_keys.side_effect = Exception("Access denied")
        assert rm._has_recent_blobs("rg", "sa-name", mock_storage) is True


class TestReconcileRegions:
    def test_adds_new_removes_old(self, rm):
        active = {"eastus", "westus"}
        provisioned = {"eastus": "s247diageastusabc", "centralus": "s247diagcentralusabc"}

        with patch.object(rm, "provision_storage_account") as mock_prov:
            mock_prov.return_value = {"storage_account_name": "new", "storage_account_id": "id", "container_name": LOGS_CONTAINER, "region": "westus"}
            with patch.object(rm, "deprovision_storage_account") as mock_deprov:
                mock_deprov.return_value = True
                result = rm.reconcile_regions("rg", active, provisioned, "abc")
                assert len(result["added"]) == 1
                assert len(result["removed"]) == 1
                assert len(result["errors"]) == 0

    def test_no_changes_needed(self, rm):
        active = {"eastus"}
        provisioned = {"eastus": "s247diageastusabc"}
        result = rm.reconcile_regions("rg", active, provisioned, "abc")
        assert result == {"added": [], "removed": [], "errors": []}


class TestApplyLock:
    def test_applies_lock(self, rm):
        with patch("shared.region_manager.ManagementLockClient") as mock_lock_cls:
            # Override the module-level ManagementLockClient
            import shared.region_manager as rm_mod
            original = rm_mod.ManagementLockClient
            rm_mod.ManagementLockClient = mock_lock_cls
            try:
                result = rm.apply_lock("rg", "sa-name", "Microsoft.Storage/storageAccounts")
                assert result is True
            finally:
                rm_mod.ManagementLockClient = original

    def test_invalid_resource_type(self, rm):
        import shared.region_manager as rm_mod
        original = rm_mod.ManagementLockClient
        rm_mod.ManagementLockClient = MagicMock()
        try:
            assert rm.apply_lock("rg", "sa", "InvalidType") is False
        finally:
            rm_mod.ManagementLockClient = original


class TestRemoveLock:
    def test_removes_lock(self, rm):
        import shared.region_manager as rm_mod
        original = rm_mod.ManagementLockClient
        rm_mod.ManagementLockClient = MagicMock()
        try:
            result = rm.remove_lock("rg", "lock-name")
            assert result is True
        finally:
            rm_mod.ManagementLockClient = original

    def test_lock_not_found(self, rm):
        import shared.region_manager as rm_mod
        original = rm_mod.ManagementLockClient
        mock_cls = MagicMock()
        mock_cls.return_value.management_locks.delete_at_resource_group_level.side_effect = Exception("ResourceNotFoundError: not found")
        rm_mod.ManagementLockClient = mock_cls
        try:
            result = rm.remove_lock("rg", "missing-lock")
            assert result is True
        finally:
            rm_mod.ManagementLockClient = original
