"""Azure SDK operations for resource discovery and diagnostic settings management.

All operations use real Azure SDK calls with DefaultAzureCredential
(Managed Identity in Azure, ``az login`` locally).
"""

import logging
from typing import Optional, Dict, List, Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.monitor import MonitorManagementClient

logger = logging.getLogger(__name__)

DIAGNOSTIC_SETTING_NAME = "s247-diag-logs"


class AzureManager:
    """Manage Azure resources, diagnostic categories, and diagnostic settings."""

    def __init__(self):
        self.credential = DefaultAzureCredential()
        self._diag_support_cache: Dict[str, bool] = {}
        self._monitor_clients: Dict[str, MonitorManagementClient] = {}

    def _get_monitor_client(self, subscription_id: str) -> MonitorManagementClient:
        """Get or create a cached MonitorManagementClient for a subscription."""
        if subscription_id not in self._monitor_clients:
            self._monitor_clients[subscription_id] = MonitorManagementClient(
                self.credential, subscription_id,
                connection_timeout=10,
                read_timeout=30,
            )
        return self._monitor_clients[subscription_id]

    # ------------------------------------------------------------------
    # Resource discovery
    # ------------------------------------------------------------------

    def supports_diagnostic_logs(self, resource_id: str, resource_type: str) -> bool:
        """Check if a resource type supports diagnostic log categories.

        Results are cached per resource type to avoid repeated API calls.
        """
        rtype = resource_type.lower()
        if rtype in self._diag_support_cache:
            return self._diag_support_cache[rtype]

        sub_id = _extract_subscription_id(resource_id)
        if not sub_id:
            return False

        try:
            client = self._get_monitor_client(sub_id)
            result = client.diagnostic_settings_category.list(resource_id)
            has_logs = any(
                cat.category_type and cat.category_type.lower() == "logs"
                for cat in result.value
            )
            self._diag_support_cache[rtype] = has_logs
            logger.debug(f"Diagnostic support for {rtype}: {has_logs}")
            return has_logs
        except Exception:
            self._diag_support_cache[rtype] = False
            return False

    def get_all_resources(self, subscription_ids: List[str]) -> List[Dict]:
        """List resources that support diagnostic logs across subscriptions.

        Checks one resource per type against the Monitor API, caches the
        result, and skips unsupported types. Returns only resources where
        diagnostic settings can be applied.
        """
        all_resources: List[Dict] = []
        for sub_id in subscription_ids:
            try:
                client = ResourceManagementClient(self.credential, sub_id)
                for resource in client.resources.list():
                    r_dict = {
                        "id": resource.id,
                        "name": resource.name,
                        "type": resource.type,
                        "location": resource.location,
                        "resource_group": _extract_resource_group(resource.id),
                        "subscription_id": sub_id,
                        "tags": resource.tags or {},
                    }
                    if self.supports_diagnostic_logs(resource.id, resource.type):
                        all_resources.append(r_dict)
                logger.info(
                    f"Listed resources in subscription {sub_id}: "
                    f"{len(all_resources)} diagnostic-capable resources so far"
                )
            except Exception as e:
                logger.error(
                    f"Failed to list resources for subscription {sub_id}: {e}"
                )
        return all_resources

    # ------------------------------------------------------------------
    # Diagnostic categories
    # ------------------------------------------------------------------

    def get_diagnostic_categories(self, resource_id: str) -> List[str]:
        """Get supported diagnostic log categories for a resource."""
        sub_id = _extract_subscription_id(resource_id)
        if not sub_id:
            logger.error(
                f"Cannot extract subscription from resource_id: {resource_id}"
            )
            return []

        try:
            client = self._get_monitor_client(sub_id)
            result = client.diagnostic_settings_category.list(resource_id)
            categories = [
                cat.name
                for cat in result.value
                if cat.category_type and cat.category_type.lower() == "logs"
            ]
            logger.info(
                f"Diagnostic categories for {resource_id}: {categories}"
            )
            return categories
        except Exception as e:
            logger.error(
                f"Failed to get diagnostic categories for {resource_id}: {e}"
            )
            return []

    # ------------------------------------------------------------------
    # Diagnostic settings CRUD
    # ------------------------------------------------------------------

    def get_diagnostic_setting(
        self, resource_id: str, setting_name: str = DIAGNOSTIC_SETTING_NAME
    ) -> Optional[Dict]:
        """Check if a diagnostic setting exists on a resource.

        Returns a dict with the setting details including enabled categories,
        or ``None`` if not found.
        """
        sub_id = _extract_subscription_id(resource_id)
        if not sub_id:
            return None

        try:
            client = self._get_monitor_client(sub_id)
            setting = client.diagnostic_settings.get(resource_id, setting_name)
            # Extract enabled log categories from the setting
            enabled_categories = []
            if setting.logs:
                for log in setting.logs:
                    if log.enabled:
                        if hasattr(log, "category") and log.category:
                            enabled_categories.append(log.category)
                        elif hasattr(log, "category_group") and log.category_group:
                            enabled_categories.append(f"group:{log.category_group}")
            return {
                "id": setting.id,
                "name": setting.name,
                "storage_account_id": setting.storage_account_id,
                "categories": enabled_categories,
            }
        except Exception as e:
            # ResourceNotFoundError is expected when the setting doesn't exist
            if "NotFound" in str(type(e).__name__) or "not found" in str(e).lower():
                logger.debug(
                    f"Diagnostic setting '{setting_name}' not found on {resource_id}"
                )
            else:
                logger.error(
                    f"Error checking diagnostic setting on {resource_id}: {e}"
                )
            return None

    def create_diagnostic_setting(
        self,
        resource_id: str,
        storage_account_id: str,
        categories: Optional[List[str]] = None,
        setting_name: str = DIAGNOSTIC_SETTING_NAME,
    ) -> bool:
        """Create or update a diagnostic setting on a resource.

        If ``categories`` is provided, enables only those specific log
        categories.  If ``None``, falls back to the ``allLogs`` category
        group for backward compatibility.
        """
        sub_id = _extract_subscription_id(resource_id)
        if not sub_id:
            return False

        try:
            client = self._get_monitor_client(sub_id)
            if categories is not None:
                logs = [
                    {"category": cat, "enabled": True}
                    for cat in categories
                ]
            else:
                logs = [{"category_group": "allLogs", "enabled": True}]
            params = {
                "storage_account_id": storage_account_id,
                "logs": logs,
                "metrics": [],
            }
            client.diagnostic_settings.create_or_update(
                resource_uri=resource_id,
                name=setting_name,
                parameters=params,
            )
            logger.info(
                f"Created diagnostic setting '{setting_name}' on {resource_id} "
                f"with {len(logs)} log entries"
            )
            return True
        except Exception as e:
            logger.error(
                f"Failed to create diagnostic setting on {resource_id}: {e}"
            )
            return False

    def delete_diagnostic_setting(
        self,
        resource_id: str,
        setting_name: str = DIAGNOSTIC_SETTING_NAME,
    ) -> bool:
        """Delete the s247-diag-logs diagnostic setting from a resource."""
        sub_id = _extract_subscription_id(resource_id)
        if not sub_id:
            return False

        try:
            client = self._get_monitor_client(sub_id)
            client.diagnostic_settings.delete(resource_id, setting_name)
            logger.info(
                f"Deleted diagnostic setting '{setting_name}' from {resource_id}"
            )
            return True
        except Exception as e:
            logger.error(
                f"Failed to delete diagnostic setting on {resource_id}: {e}"
            )
            return False

    # ------------------------------------------------------------------
    # Resource group & location helpers
    # ------------------------------------------------------------------

    def list_resource_groups(self, subscription_id: str) -> List[str]:
        """List all resource group names in a subscription."""
        try:
            client = ResourceManagementClient(self.credential, subscription_id)
            return [rg.name for rg in client.resource_groups.list()]
        except Exception as e:
            logger.error(
                f"Failed to list resource groups for {subscription_id}: {e}"
            )
            return []

    def list_locations(self, subscription_id: str) -> List[str]:
        """List all Azure locations that contain at least one resource."""
        try:
            client = ResourceManagementClient(self.credential, subscription_id)
            locations = set()
            for resource in client.resources.list():
                if resource.location:
                    locations.add(resource.location)
            return sorted(locations)
        except Exception as e:
            logger.error(
                f"Failed to list locations for {subscription_id}: {e}"
            )
            return []

    # ------------------------------------------------------------------
    # App settings management
    # ------------------------------------------------------------------

    def update_app_setting(self, key: str, value: str) -> bool:
        """Update a single app setting on the Function App.

        Reads current settings, merges the new key, and writes back.
        Uses WEBSITE_SITE_NAME (auto-populated by Azure) for the function app name.
        """
        import os
        from azure.mgmt.web import WebSiteManagementClient

        resource_group = os.environ.get("RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg"))
        func_app_name = os.environ.get("WEBSITE_SITE_NAME", os.environ.get("FUNCTION_APP_NAME", ""))
        sub_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()

        if not sub_id:
            logger.error("No subscription ID available for app setting update")
            return False
        if not func_app_name:
            logger.error("No function app name available (WEBSITE_SITE_NAME not set)")
            return False

        try:
            client = WebSiteManagementClient(self.credential, sub_id)
            current = client.web_apps.list_application_settings(
                resource_group_name=resource_group,
                name=func_app_name,
            )
            settings = dict(current.properties) if current.properties else {}
            settings[key] = value
            client.web_apps.update_application_settings(
                resource_group_name=resource_group,
                name=func_app_name,
                app_settings={"properties": settings},
            )
            logger.info(f"Updated app setting '{key}' on '{func_app_name}'")
            return True
        except Exception as e:
            logger.error(f"Failed to update app setting '{key}': {e}")
            return False

    # ------------------------------------------------------------------
    # Bulk diagnostic settings removal
    # ------------------------------------------------------------------

    def remove_all_diagnostic_settings(
        self, subscription_ids: List[str]
    ) -> Dict[str, Any]:
        """Remove s247-diag-logs diagnostic settings from ALL resources.

        Returns a summary dict with counts and errors.
        """
        summary: Dict[str, Any] = {"removed": 0, "skipped": 0, "errors": 0, "details": []}
        all_resources = self.get_all_resources(subscription_ids)

        for resource in all_resources:
            resource_id = resource.get("id", "")
            existing = self.get_diagnostic_setting(resource_id)
            if not existing:
                summary["skipped"] += 1
                continue
            try:
                success = self.delete_diagnostic_setting(resource_id)
                if success:
                    summary["removed"] += 1
                    summary["details"].append({"id": resource_id, "status": "removed"})
                else:
                    summary["errors"] += 1
                    summary["details"].append({"id": resource_id, "status": "error"})
            except Exception as e:
                summary["errors"] += 1
                summary["details"].append({"id": resource_id, "status": "error", "message": str(e)})

        logger.info(
            f"Bulk diagnostic settings removal: removed={summary['removed']}, "
            f"skipped={summary['skipped']}, errors={summary['errors']}"
        )
        return summary


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _extract_subscription_id(resource_id: str) -> str:
    """Extract subscription ID from a fully-qualified Azure resource ID."""
    parts = resource_id.strip("/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == "subscriptions" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _extract_resource_group(resource_id: str) -> str:
    """Extract resource group name from a fully-qualified Azure resource ID."""
    parts = resource_id.strip("/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""
