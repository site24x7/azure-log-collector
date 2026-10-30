"""Azure SDK operations for resource discovery and diagnostic settings management.

All operations use real Azure SDK calls with DefaultAzureCredential
(Managed Identity in Azure, ``az login`` locally).
"""

import logging
import os
from typing import Optional, Dict, List, Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.monitor import MonitorManagementClient

logger = logging.getLogger(__name__)

# Name used before settings were per collector; still the name without a suffix.
LEGACY_SETTING_NAME = "s247-diag-logs"
DIAGNOSTIC_SETTING_NAME = LEGACY_SETTING_NAME


def collector_setting_name() -> str:
    """This collector's diagnostic-setting name: s247-diag-logs-<suffix>.

    Each collector owns its own setting, so two collectors watching the same
    resource each receive the logs instead of overwriting or deleting each
    other's setting. Without a suffix the legacy name is kept.
    """
    suffix = os.environ.get("DIAG_STORAGE_SUFFIX", "").strip().lower()
    return f"{LEGACY_SETTING_NAME}-{suffix}" if suffix else LEGACY_SETTING_NAME


def _storage_name(storage_account_id: str) -> str:
    return (storage_account_id or "").rstrip("/").rsplit("/", 1)[-1].lower()



def _type_not_supported(error: Exception) -> bool:
    """Whether Azure answered that the resource type has no diagnostic settings
    (400 ResourceTypeNotSupported), as opposed to a failure to answer."""
    code = getattr(getattr(error, "error", None), "code", None) or ""
    return getattr(error, "status_code", None) == 400 and code == "ResourceTypeNotSupported"

class AzureManager:
    """Manage Azure resources, diagnostic categories, and diagnostic settings."""

    def __init__(self):
        self.credential = DefaultAzureCredential()
        self._diag_support_cache: Dict[str, bool] = {}
        self._monitor_clients: Dict[str, MonitorManagementClient] = {}
        # What the last get_all_resources() could not read. While it is not
        # empty the list is partial, and nothing may be removed on its basis.
        self.discovery_errors: List[str] = []

    @property
    def discovery_complete(self) -> bool:
        return not self.discovery_errors

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
        except Exception as e:
            if _type_not_supported(e):
                # Azure's definite answer for types without diagnostic settings
                # (network watchers, for one): no logs, like an empty category list.
                self._diag_support_cache[rtype] = False
                logger.debug(f"Diagnostic support for {rtype}: not supported")
                return False
            # Unknown, not "no logs": not cached, and the discovery is incomplete
            # (else every resource of this type would look gone and be cleaned up).
            self.discovery_errors.append(f"support check for {rtype}: {type(e).__name__}")
            return False

    def get_all_resources(self, subscription_ids: List[str]) -> List[Dict]:
        """List resources that support diagnostic logs across subscriptions.

        Checks one resource per type against the Monitor API, caches the
        result, and skips unsupported types. Returns only resources where
        diagnostic settings can be applied.
        """
        all_resources: List[Dict] = []
        self.discovery_errors = []
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
                self.discovery_errors.append(f"subscription {sub_id}: {type(e).__name__}")
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
        self, resource_id: str, setting_name: Optional[str] = None, raise_errors: bool = False
    ) -> Optional[Dict]:
        """Check if a diagnostic setting exists on a resource.

        Returns a dict with the setting details including enabled categories,
        or ``None`` if not found. With ``raise_errors`` a failed lookup (other
        than "not found") raises instead of looking like "no setting".
        """
        setting_name = setting_name or collector_setting_name()
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
                if raise_errors:
                    raise
            return None

    def _foreign_setting(self, resource_id: str, setting_name: str) -> Optional[bool]:
        """Whether a setting with this name exists but is not this collector's:
        it sends somewhere other than a storage account in the collector's own
        resource group (a customer or another tool reused the name). None when
        the lookup failed, False when there is none or it is ours."""
        try:
            existing = self.get_diagnostic_setting(resource_id, setting_name, raise_errors=True)
        except Exception:
            return None
        if not existing:
            return False
        own_rg = os.environ.get("RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg"))
        destination = (existing.get("storage_account_id") or "").lower()
        return f"/resourcegroups/{own_rg.lower()}/providers/microsoft.storage/" not in destination

    def create_diagnostic_setting(
        self,
        resource_id: str,
        storage_account_id: str,
        categories: Optional[List[str]] = None,
        setting_name: Optional[str] = None,
    ) -> bool:
        """Create or update a diagnostic setting on a resource.

        If ``categories`` is provided, enables only those specific log
        categories.  If ``None``, falls back to the ``allLogs`` category
        group for backward compatibility.
        """
        setting_name = setting_name or collector_setting_name()
        sub_id = _extract_subscription_id(resource_id)
        if not sub_id:
            return False
        # Never take over a setting of the same name that someone else made.
        foreign = self._foreign_setting(resource_id, setting_name)
        if foreign is not False:
            logger.error("Not writing '%s' on %s: %s", setting_name, resource_id,
                         "lookup failed" if foreign is None else "a setting with this name sends elsewhere")
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
        setting_name: Optional[str] = None,
    ) -> bool:
        """Delete this collector's diagnostic setting (or ``setting_name``) from a resource.

        A setting of that name that sends elsewhere is not this collector's and
        is left in place (returns True: nothing of ours is there)."""
        setting_name = setting_name or collector_setting_name()
        sub_id = _extract_subscription_id(resource_id)
        if not sub_id:
            return False
        foreign = self._foreign_setting(resource_id, setting_name)
        if foreign is None:
            return False
        if foreign:
            logger.warning("Leaving '%s' on %s: it sends elsewhere, not to this collector", setting_name, resource_id)
            return True

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

    def remove_legacy_setting_if_own(self, resource_id: str, own_storage_name: str,
                                     raise_errors: bool = False) -> Optional[bool]:
        """Delete the old shared-name setting when it sends to this collector's storage.

        Needed before this collector writes its own setting: Azure refuses a
        second setting on the same resource that sends the same category to the
        same storage account. A legacy setting pointing elsewhere belongs to
        another collector and is left alone.

        Returns None when there is nothing of ours to remove, True when it was
        removed and False when the delete failed (the caller must not assume
        it is gone). With ``raise_errors`` a failed lookup raises instead of
        looking like "nothing to remove".
        """
        if collector_setting_name() == LEGACY_SETTING_NAME or not own_storage_name:
            return None
        legacy = self.get_diagnostic_setting(resource_id, LEGACY_SETTING_NAME, raise_errors=raise_errors)
        own_names = {own_storage_name.lower()} if isinstance(own_storage_name, str) else \
            {n.lower() for n in own_storage_name}
        if not legacy or _storage_name(legacy.get("storage_account_id", "")) not in own_names:
            return None
        return self.delete_diagnostic_setting(resource_id, LEGACY_SETTING_NAME)

    def delete_own_settings(self, resource_id: str, record: Optional[Dict] = None) -> bool:
        """Delete this collector's setting, and the legacy-named one it created earlier.

        ``record`` is the collector's configured-resources entry; an entry
        without ``setting_name`` was written under the legacy name. Returns
        True only when every setting of ours is gone, so callers keep the
        record (and retry) when a delete failed.
        """
        ok = self.delete_diagnostic_setting(resource_id)
        if record is not None and \
                record.get("setting_name", LEGACY_SETTING_NAME) == LEGACY_SETTING_NAME:
            try:
                legacy = self.remove_legacy_setting_if_own(
                    resource_id, record.get("storage_account") or "", raise_errors=True)
            except Exception:
                return False  # lookup failed: the legacy setting may still export
            ok = ok and legacy is not False
        return ok

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
            # The settings API replaces the whole map, so two writers can undo
            # each other: write, read back, and write again if ours was lost.
            # An unchanged value is not written at all (every write restarts the app).
            for attempt in range(3):
                current = client.web_apps.list_application_settings(
                    resource_group_name=resource_group,
                    name=func_app_name,
                )
                settings = dict(current.properties) if current.properties else {}
                if settings.get(key) == value:
                    if attempt:
                        logger.info(f"Updated app setting '{key}' on '{func_app_name}'")
                    return True
                settings[key] = value
                client.web_apps.update_application_settings(
                    resource_group_name=resource_group,
                    name=func_app_name,
                    app_settings={"properties": settings},
                )
            current = client.web_apps.list_application_settings(
                resource_group_name=resource_group, name=func_app_name)
            if (current.properties or {}).get(key) == value:
                logger.info(f"Updated app setting '{key}' on '{func_app_name}'")
                return True
            logger.error(f"App setting '{key}' did not stick (concurrent changes)")
            return False
        except Exception as e:
            logger.error(f"Failed to update app setting '{key}': {e}")
            return False

    # ------------------------------------------------------------------
    # Bulk diagnostic settings removal
    # ------------------------------------------------------------------

    def remove_all_diagnostic_settings(
        self, subscription_ids: List[str], records: Optional[Dict[str, Dict]] = None
    ) -> Dict[str, Any]:
        """Remove this collector's diagnostic settings from ALL resources.

        Only this collector's own setting is removed, plus a legacy-named one
        that sends to this collector's regional storage; settings of other
        collectors are left in place. ``records`` (the configured-resources
        map) gives the storage a resource was actually configured with; without
        a record the regional name this collector would use is assumed.
        Returns a summary dict with counts.
        """
        from shared.storage_names import legacy_regional_storage_name, regional_storage_name

        summary: Dict[str, Any] = {"removed": 0, "skipped": 0, "errors": 0, "unresolved": 0,
                                   "details": [], "resolved_ids": []}
        suffix = os.environ.get("DIAG_STORAGE_SUFFIX", "").strip().lower()
        seed = os.environ.get("DIAG_SEED_REGION", "")
        all_resources = self.get_all_resources(subscription_ids)

        for resource in all_resources:
            resource_id = resource.get("id", "")
            try:
                removed = False
                if self.get_diagnostic_setting(resource_id, raise_errors=True):
                    removed = self.delete_diagnostic_setting(resource_id)
                    if not removed:
                        summary["errors"] += 1
                        summary["details"].append({"id": resource_id, "status": "error"})
                        continue
                record = (records or {}).get(resource_id) or {}
                own = record.get("storage_account", "")
                if suffix and not own and resource.get("location"):
                    try:
                        own = {regional_storage_name(resource["location"], suffix, seed),
                               legacy_regional_storage_name(resource["location"], suffix, seed)}
                    except ValueError:
                        own = ""
                legacy = self.remove_legacy_setting_if_own(resource_id, own, raise_errors=True)
                if legacy is False:
                    summary["errors"] += 1
                    summary["details"].append({"id": resource_id, "status": "error"})
                    continue
                removed = removed or bool(legacy)
                # Removed now or confirmed absent: its tracking record can go.
                summary["resolved_ids"].append(resource_id)
                if removed:
                    summary["removed"] += 1
                    summary["details"].append({"id": resource_id, "status": "removed"})
                else:
                    summary["skipped"] += 1
            except Exception as e:
                summary["errors"] += 1
                summary["details"].append({"id": resource_id, "status": "error", "message": str(e)})

        # Tracked resources the discovery did not return (deleted, or discovery
        # was incomplete): their setting state is unknown, keep their records.
        discovered = {r.get("id", "") for r in all_resources}
        summary["unresolved"] = sum(1 for rid in (records or {}) if rid not in discovered)

        logger.info(
            f"Bulk diagnostic settings removal: removed={summary['removed']}, "
            f"skipped={summary['skipped']}, errors={summary['errors']}, "
            f"unresolved={summary['unresolved']}"
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
