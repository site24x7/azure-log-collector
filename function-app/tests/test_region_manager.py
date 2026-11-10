"""Tests for shared/region_manager.py — Azure SDK calls mocked."""

from unittest.mock import patch, MagicMock

import pytest

from shared.region_manager import (
    RegionManager,
    _sanitize_region,
    _storage_account_name,
    STORAGE_PREFIX,
    LOGS_CONTAINER,
    IDLE_RETENTION_DAYS,
    RETENTION_RULE_NAME,
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

    def test_own_suffix_wins_over_leftover_account_in_same_region(self, rm):
        # A failed earlier install (suffix qt4xn2) left a tagged account behind;
        # the new deployment (suffix oct14) must use its own, whatever the order.
        def acct(name):
            a = MagicMock()
            a.tags = {"managed-by": "s247-diag-logs", "purpose": "diag-logs-regional"}
            a.primary_location = "westeurope"
            a.name = name
            return a

        for order in (["s247diagwesteuropeoct14", "s247diagwesteuropeqt4xn2"],
                      ["s247diagwesteuropeqt4xn2", "s247diagwesteuropeoct14"]):
            with patch("shared.region_manager.StorageManagementClient") as mock_cls:
                mock_cls.return_value.storage_accounts.list_by_resource_group.return_value = \
                    [acct(n) for n in order]
                assert rm.get_provisioned_regions("my-rg", suffix="oct14") == \
                    {"westeurope": "s247diagwesteuropeoct14"}
                assert rm.get_provisioned_regions("my-rg", suffix="") == \
                    {"westeurope": "s247diagwesteuropeoct14"}  # deterministic fallback

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
            with patch.object(rm, "set_retention", return_value=True) as keep:
                result = rm.ensure_tenant_storage_account("my-rg", "abc")
            assert result["name"] == "s247diagtenantabc"
            # No create call when it already exists
            mock_cls.return_value.storage_accounts.begin_create.assert_not_called()
            # back on the normal retention after being idle
            keep.assert_called_once_with("my-rg", "s247diagtenantabc", 7)

    def test_ensure_creates_when_missing(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls, \
             patch("shared.region_manager.ResourceManagementClient") as mock_rc, \
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

    def test_get_subscription_name(self, rm):
        with patch("azure.mgmt.resource.SubscriptionClient") as mock_sc:
            mock_sc.return_value.subscriptions.get.return_value = MagicMock(
                display_name="Prod Subscription")
            assert rm.get_subscription_name("sub-123") == "Prod Subscription"

    def test_get_subscription_name_best_effort(self, rm):
        with patch("azure.mgmt.resource.SubscriptionClient") as mock_sc:
            mock_sc.return_value.subscriptions.get.side_effect = Exception("no access")
            assert rm.get_subscription_name("sub-123") == ""

    def test_idle_noop_when_absent(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls, \
             patch.object(rm, "set_retention") as keep:
            mock_cls.return_value.storage_accounts.list_by_resource_group.return_value = [
                self._regional_acct()
            ]
            # No tenant SA present → nothing to idle
            assert rm.idle_tenant_storage_account("my-rg") is False
        keep.assert_not_called()

    def test_idle_keeps_the_account_with_short_retention(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls, \
             patch.object(rm, "set_retention", return_value=True) as keep:
            mock_cls.return_value.storage_accounts.list_by_resource_group.return_value = [
                self._tenant_acct()
            ]
            assert rm.idle_tenant_storage_account("my-rg") is True
        keep.assert_called_once_with("my-rg", "s247diagtenantabc", IDLE_RETENTION_DAYS)
        mock_cls.return_value.storage_accounts.delete.assert_not_called()


class TestProvisionStorageAccount:
    def test_creates_account_and_container(self, rm):
        mock_sa = MagicMock()
        mock_sa.id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/sa"

        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_cls.return_value.storage_accounts.begin_create.return_value.result.return_value = mock_sa
            with patch.object(rm, "apply_lifecycle_policy", return_value=True) as alp:
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


def _policy(days, name=RETENTION_RULE_NAME, enabled=True):
    rule = MagicMock(enabled=enabled)
    rule.name = name
    rule.definition.actions.base_blob.delete.days_after_modification_greater_than = float(days)
    return MagicMock(policy=MagicMock(rules=[rule]))


class TestSetRetention:
    def test_writes_only_on_a_change(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            policies = mock_cls.return_value.management_policies
            policies.get.return_value = _policy(7)
            assert rm.set_retention("rg", "sa", 7) is True
            policies.create_or_update.assert_not_called()
            assert rm.set_retention("rg", "sa", IDLE_RETENTION_DAYS) is True
            days = (policies.create_or_update.call_args.kwargs["properties"]["policy"]["rules"][0]
                    ["definition"]["actions"]["baseBlob"]["delete"]["daysAfterModificationGreaterThan"])
            assert days == IDLE_RETENTION_DAYS

    @pytest.mark.parametrize("current", [
        Exception("no policy yet"), _policy(7, name="someone-elses-rule"), _policy(1, enabled=False)])
    def test_writes_when_our_rule_is_missing(self, rm, current):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            policies = mock_cls.return_value.management_policies
            if isinstance(current, Exception):
                policies.get.side_effect = current
            else:
                policies.get.return_value = current
            assert rm.set_retention("rg", "sa", IDLE_RETENTION_DAYS) is True
            policies.create_or_update.assert_called_once()

    def test_failure_is_reported_not_raised(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            policies = mock_cls.return_value.management_policies
            policies.get.side_effect = Exception("no policy yet")
            policies.create_or_update.side_effect = Exception("not allowed")
            assert rm.set_retention("rg", "sa", 1) is False


class TestReconcileRegions:
    def test_adds_new_and_idles_unused_without_deleting(self, rm):
        active = {"eastus", "westus"}
        provisioned = {"eastus": "s247diageastusabc", "centralus": "s247diagcentralusabc"}

        with patch.object(rm, "provision_storage_account") as mock_prov, \
             patch.object(rm, "set_retention", return_value=True) as keep, \
             patch("shared.region_manager.StorageManagementClient") as mock_cls:
            mock_prov.return_value = {"storage_account_name": "new", "storage_account_id": "id", "container_name": LOGS_CONTAINER, "region": "westus"}
            result = rm.reconcile_regions("rg", active, provisioned, "abc")
        assert len(result["added"]) == 1
        assert result["idle"] == ["centralus"]
        assert result["errors"] == []
        assert sorted(c.args for c in keep.call_args_list) == [
            ("rg", "s247diagcentralusabc", IDLE_RETENTION_DAYS),
            ("rg", "s247diageastusabc", 7),       # active again: normal retention back
        ]
        mock_cls.return_value.storage_accounts.delete.assert_not_called()

    def test_no_changes_needed(self, rm):
        with patch.object(rm, "set_retention", return_value=True):
            result = rm.reconcile_regions("rg", {"eastus"}, {"eastus": "s247diageastusabc"}, "abc")
        assert result == {"added": [], "idle": [], "errors": []}

    def test_retention_failure_is_an_error(self, rm):
        with patch.object(rm, "set_retention", return_value=False):
            result = rm.reconcile_regions("rg", set(), {"centralus": "s247diagcentralusabc"}, "abc")
        assert result["idle"] == []
        assert result["errors"] == [{"region": "centralus", "error": "retention not set"}]


def test_the_collector_never_deletes_a_storage_account_or_uses_locks():
    # Its Contributor role cannot change locks, and a deleted name can be taken by anyone.
    from pathlib import Path
    import shared.region_manager as rm_mod
    source = Path(rm_mod.__file__).read_text()
    assert "storage_accounts.delete" not in source
    assert "ManagementLockClient" not in source and "management_locks" not in source


class TestStorageNameFallback:
    """A generated name taken elsewhere in Azure falls back to a random one."""

    @staticmethod
    def _taken(*names):
        def check(params):
            result = MagicMock()
            result.name_available = params["name"] not in names
            return result
        return check

    def test_regional_uses_random_name_when_generated_name_is_taken(self, rm):
        from azure.core.exceptions import ResourceNotFoundError
        mock_sa = MagicMock(id="/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/x")
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            ops = mock_cls.return_value.storage_accounts
            ops.check_name_availability.side_effect = self._taken("s247diageastusabc")
            ops.get_properties.side_effect = ResourceNotFoundError("Not found")
            ops.begin_create.return_value.result.return_value = mock_sa
            with patch.object(rm, "apply_lifecycle_policy", return_value=True):
                result = rm.provision_storage_account("rg", "eastus", "abc")
        name = ops.begin_create.call_args.kwargs["account_name"]
        assert name != "s247diageastusabc"
        assert name.startswith(STORAGE_PREFIX) and len(name) <= 24 and name.isalnum() and name == name.lower()
        assert result["storage_account_name"] == name
        assert result["storage_account_id"] == mock_sa.id
        tags = ops.begin_create.call_args.kwargs["parameters"]["tags"]
        assert tags["region"] == "eastus" and tags["purpose"] == "diag-logs-regional"

    def test_tenant_uses_random_name_when_generated_name_is_taken(self, rm):
        from azure.core.exceptions import ResourceNotFoundError
        with patch("shared.region_manager.StorageManagementClient") as mock_cls, \
             patch.object(rm, "get_tenant_storage_account", return_value=None), \
             patch.object(rm, "_resource_group_location", return_value="eastus"), \
             patch.object(rm, "apply_lifecycle_policy", return_value=True):
            ops = mock_cls.return_value.storage_accounts
            ops.check_name_availability.side_effect = self._taken("s247diagtenantabc")
            ops.get_properties.side_effect = ResourceNotFoundError("Not found")
            ops.begin_create.return_value.result.return_value = MagicMock(id="tenant-id")
            result = rm.ensure_tenant_storage_account("rg", "abc")
        name = ops.begin_create.call_args.kwargs["account_name"]
        assert name != "s247diagtenantabc" and name.startswith("s247dt") and len(name) <= 24
        assert result["name"] == name

    def test_other_ownership_errors_do_not_fall_back(self, rm):
        with patch("shared.region_manager.StorageManagementClient") as mock_cls:
            ops = mock_cls.return_value.storage_accounts
            ops.check_name_availability.return_value.name_available = False
            ops.get_properties.side_effect = RuntimeError("Read permission denied")
            result = rm.provision_storage_account("rg", "eastus", "abc")
        assert result["storage_account_id"] == ""
        ops.begin_create.assert_not_called()

    def test_reconcile_counts_a_failed_region_as_an_error(self, rm):
        with patch.object(rm, "provision_storage_account",
                          return_value={"region": "eastus", "storage_account_id": "", "error": "quota"}):
            result = rm.reconcile_regions("rg", {"eastus"}, {}, "abc")
        assert result["added"] == []
        assert result["errors"] == [{"region": "eastus", "error": "quota"}]
