"""Tests for shared/azure_manager.py — all Azure SDK calls mocked."""

from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from shared.azure_manager import (
    AzureManager,
    _extract_subscription_id,
    _extract_resource_group,
    DIAGNOSTIC_SETTING_NAME,
)


# ─── Helper extraction functions ────────────────────────────────────────────


class TestExtractSubscriptionId:
    def test_full_id(self):
        rid = "/subscriptions/abc-123/resourceGroups/rg/providers/M/T/R"
        assert _extract_subscription_id(rid) == "abc-123"

    def test_no_subscription(self):
        assert _extract_subscription_id("/providers/M/T/R") == ""

    def test_empty(self):
        assert _extract_subscription_id("") == ""


class TestExtractResourceGroup:
    def test_full_id(self):
        rid = "/subscriptions/s/resourceGroups/my-rg/providers/M/T/R"
        assert _extract_resource_group(rid) == "my-rg"

    def test_no_rg(self):
        assert _extract_resource_group("/subscriptions/s") == ""


# ─── AzureManager ───────────────────────────────────────────────────────────


@pytest.fixture
def manager():
    with patch("shared.azure_manager.DefaultAzureCredential"):
        return AzureManager()


class TestSupportsDiagnosticLogs:
    def test_supported(self, manager):
        mock_cat = MagicMock()
        mock_cat.category_type = "Logs"
        mock_result = MagicMock()
        mock_result.value = [mock_cat]

        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings_category.list.return_value = mock_result
            assert manager.supports_diagnostic_logs(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R",
                "Microsoft.Compute/virtualMachines"
            ) is True

    def test_not_supported(self, manager):
        mock_cat = MagicMock()
        mock_cat.category_type = "Metrics"
        mock_result = MagicMock()
        mock_result.value = [mock_cat]

        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings_category.list.return_value = mock_result
            assert manager.supports_diagnostic_logs(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R",
                "Microsoft.Compute/disks"
            ) is False

    def test_caches_result(self, manager):
        mock_cat = MagicMock()
        mock_cat.category_type = "Logs"
        mock_result = MagicMock()
        mock_result.value = [mock_cat]

        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings_category.list.return_value = mock_result
            manager.supports_diagnostic_logs(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R",
                "SameType"
            )
            manager.supports_diagnostic_logs(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R2",
                "SameType"
            )
            # Should only call the API once (cached by type)
            assert mock_cls.return_value.diagnostic_settings_category.list.call_count == 1

    def test_bad_resource_id(self, manager):
        assert manager.supports_diagnostic_logs("no-sub-id", "SomeType") is False

    def test_api_exception(self, manager):
        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings_category.list.side_effect = Exception("API error")
            assert manager.supports_diagnostic_logs(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R",
                "ErrorType"
            ) is False


class TestGetAllResources:
    def test_returns_supported_resources(self, manager):
        mock_resource = MagicMock()
        mock_resource.id = "/subscriptions/s/resourceGroups/rg/providers/M/T/R"
        mock_resource.name = "R"
        mock_resource.type = "M/T"
        mock_resource.location = "eastus"
        mock_resource.tags = {"env": "prod"}

        with patch("shared.azure_manager.ResourceManagementClient") as mock_rmc:
            mock_rmc.return_value.resources.list.return_value = [mock_resource]
            with patch.object(manager, "supports_diagnostic_logs", return_value=True):
                result = manager.get_all_resources(["sub1"])
                assert len(result) == 1
                assert result[0]["name"] == "R"
                assert result[0]["location"] == "eastus"

    def test_skips_unsupported(self, manager):
        mock_resource = MagicMock()
        mock_resource.id = "/subscriptions/s/resourceGroups/rg/providers/M/T/R"
        mock_resource.name = "R"
        mock_resource.type = "M/T"
        mock_resource.location = "eastus"
        mock_resource.tags = None

        with patch("shared.azure_manager.ResourceManagementClient") as mock_rmc:
            mock_rmc.return_value.resources.list.return_value = [mock_resource]
            with patch.object(manager, "supports_diagnostic_logs", return_value=False):
                result = manager.get_all_resources(["sub1"])
                assert len(result) == 0

    def test_handles_api_error(self, manager):
        with patch("shared.azure_manager.ResourceManagementClient") as mock_rmc:
            mock_rmc.return_value.resources.list.side_effect = Exception("Boom")
            result = manager.get_all_resources(["sub1"])
            assert result == []


class TestGetDiagnosticCategories:
    def test_returns_log_categories_only(self, manager):
        log_cat = MagicMock()
        log_cat.name = "AuditEvent"
        log_cat.category_type = "Logs"
        metric_cat = MagicMock()
        metric_cat.name = "AllMetrics"
        metric_cat.category_type = "Metrics"
        mock_result = MagicMock()
        mock_result.value = [log_cat, metric_cat]

        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings_category.list.return_value = mock_result
            result = manager.get_diagnostic_categories(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R"
            )
            assert result == ["AuditEvent"]

    def test_bad_resource_id(self, manager):
        assert manager.get_diagnostic_categories("bad-id") == []


class TestDiagnosticSettingsCrud:
    def test_get_setting_found(self, manager):
        mock_log1 = MagicMock()
        mock_log1.enabled = True
        mock_log1.category = "AuditEvent"
        mock_log1.category_group = None
        mock_log2 = MagicMock()
        mock_log2.enabled = True
        mock_log2.category = "SignInLogs"
        mock_log2.category_group = None
        mock_setting = MagicMock()
        mock_setting.id = "setting-id"
        mock_setting.name = DIAGNOSTIC_SETTING_NAME
        mock_setting.storage_account_id = "sa-id"
        mock_setting.logs = [mock_log1, mock_log2]

        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings.get.return_value = mock_setting
            result = manager.get_diagnostic_setting(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R"
            )
            assert result["name"] == DIAGNOSTIC_SETTING_NAME
            assert result["storage_account_id"] == "sa-id"
            assert result["categories"] == ["AuditEvent", "SignInLogs"]

    def test_get_setting_not_found(self, manager):
        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings.get.side_effect = Exception("ResourceNotFoundError")
            assert manager.get_diagnostic_setting(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R"
            ) is None

    def test_create_setting(self, manager):
        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            result = manager.create_diagnostic_setting(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R",
                "sa-id"
            )
            assert result is True
            call_args = mock_cls.return_value.diagnostic_settings.create_or_update.call_args
            # Default: allLogs category group when no categories specified
            params = call_args.kwargs.get("parameters") or call_args[1].get("parameters")
            assert params["logs"][0]["category_group"] == "allLogs"

    def test_create_setting_with_categories(self, manager):
        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            result = manager.create_diagnostic_setting(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R",
                "sa-id",
                categories=["AuditEvent", "SignInLogs"]
            )
            assert result is True
            call_args = mock_cls.return_value.diagnostic_settings.create_or_update.call_args
            params = call_args.kwargs.get("parameters") or call_args[1].get("parameters")
            assert len(params["logs"]) == 2
            assert params["logs"][0] == {"category": "AuditEvent", "enabled": True}
            assert params["logs"][1] == {"category": "SignInLogs", "enabled": True}

    def test_create_setting_failure(self, manager):
        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings.create_or_update.side_effect = Exception("Forbidden")
            result = manager.create_diagnostic_setting(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R",
                "sa-id"
            )
            assert result is False

    def test_delete_setting(self, manager):
        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            result = manager.delete_diagnostic_setting(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R"
            )
            assert result is True

    def test_delete_setting_failure(self, manager):
        with patch("shared.azure_manager.MonitorManagementClient") as mock_cls:
            mock_cls.return_value.diagnostic_settings.delete.side_effect = Exception("Forbidden")
            result = manager.delete_diagnostic_setting(
                "/subscriptions/s/resourceGroups/rg/providers/M/T/R"
            )
            assert result is False


class TestListResourceGroups:
    def test_returns_names(self, manager):
        rg1 = MagicMock()
        rg1.name = "rg-1"
        rg2 = MagicMock()
        rg2.name = "rg-2"

        with patch("shared.azure_manager.ResourceManagementClient") as mock_cls:
            mock_cls.return_value.resource_groups.list.return_value = [rg1, rg2]
            result = manager.list_resource_groups("sub1")
            assert result == ["rg-1", "rg-2"]


class TestListLocations:
    def test_returns_unique_sorted(self, manager):
        r1 = MagicMock()
        r1.location = "eastus"
        r2 = MagicMock()
        r2.location = "westus"
        r3 = MagicMock()
        r3.location = "eastus"  # duplicate

        with patch("shared.azure_manager.ResourceManagementClient") as mock_cls:
            mock_cls.return_value.resources.list.return_value = [r1, r2, r3]
            result = manager.list_locations("sub1")
            assert result == ["eastus", "westus"]


class TestRemoveAllDiagnosticSettings:
    def test_removes_and_counts(self, manager):
        with patch.object(manager, "get_all_resources") as mock_get:
            mock_get.return_value = [
                {"id": "/sub/rg/r1"},
                {"id": "/sub/rg/r2"},
                {"id": "/sub/rg/r3"},
            ]
            with patch.object(manager, "get_diagnostic_setting") as mock_exists:
                mock_exists.side_effect = [
                    {"name": "s247-diag-logs"},  # r1 has setting
                    None,                         # r2 doesn't
                    {"name": "s247-diag-logs"},  # r3 has setting
                ]
                with patch.object(manager, "delete_diagnostic_setting") as mock_del:
                    mock_del.return_value = True
                    result = manager.remove_all_diagnostic_settings(["sub1"])
                    assert result["removed"] == 2
                    assert result["skipped"] == 1
                    assert result["errors"] == 0
