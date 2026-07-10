"""Per-region Storage Account lifecycle management with resource locks.

Provisions and deprovisions Storage Accounts per-region so Azure diagnostic
settings can stream logs to a same-region destination. Each storage account
gets an ``insights-logs`` container for blob-based log collection.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set

from azure.identity import DefaultAzureCredential
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.resource import ResourceManagementClient

# ManagementLockClient may not be available in all azure-mgmt-resource versions
try:
    from azure.mgmt.resource.locks import ManagementLockClient
except ImportError:
    ManagementLockClient = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

STORAGE_PREFIX = "s247diag"
LOGS_CONTAINER = "insights-logs"
LOCK_PREFIX = "s247-lock-sa"
# Default: don't delete storage accounts with blobs newer than this.
# Overridable via SAFE_DELETE_MAX_AGE_DAYS env var.
SAFE_DELETE_MAX_AGE_DAYS_DEFAULT = 7
# Lifecycle policy: blobs in insights-logs-* containers are deleted after this many
# days. The processor reads incrementally via block-list checkpoints and never
# deletes blobs itself, so retention is enforced here. Override via
# BLOB_RETENTION_DAYS env var.
BLOB_RETENTION_DAYS_DEFAULT = 7


def _get_blob_retention_days() -> int:
    """Return blob retention (lifecycle) days from env var or default."""
    import os
    try:
        return int(os.environ.get("BLOB_RETENTION_DAYS", BLOB_RETENTION_DAYS_DEFAULT))
    except (ValueError, TypeError):
        return BLOB_RETENTION_DAYS_DEFAULT


def _get_safe_delete_days() -> int:
    """Return safe-delete threshold from env var or default."""
    import os
    try:
        return int(os.environ.get("SAFE_DELETE_MAX_AGE_DAYS", SAFE_DELETE_MAX_AGE_DAYS_DEFAULT))
    except (ValueError, TypeError):
        return SAFE_DELETE_MAX_AGE_DAYS_DEFAULT


def _sanitize_region(region: str) -> str:
    """Remove non-alphanumeric characters and lowercase."""
    return re.sub(r"[^a-z0-9]", "", region.lower())


def _storage_account_name(region: str, suffix: str) -> str:
    """Build per-region storage account name.

    Azure storage account names: 3-24 chars, lowercase alphanumeric only.
    Format: s247diag{region}{suffix}  (e.g., s247diageastus<suffix>)
    """
    name = f"{STORAGE_PREFIX}{_sanitize_region(region)}{suffix}"
    return name[:24]


class RegionManager:
    """Manage per-region Storage Accounts for diagnostic log collection."""

    def __init__(self, subscription_id: str):
        self.subscription_id = subscription_id
        self.credential = DefaultAzureCredential()

    # ------------------------------------------------------------------
    # Region analysis
    # ------------------------------------------------------------------

    @staticmethod
    def get_storage_name_for_region(region: str, suffix: str) -> str:
        """Return the storage account name for a given region."""
        return _storage_account_name(region, suffix)

    def get_active_regions(self, resources: List[Dict]) -> Set[str]:
        """Extract unique regions from a list of resources."""
        regions: Set[str] = set()
        for r in resources:
            location = r.get("location", "")
            if location:
                regions.add(location.lower())
        logger.info(f"Active regions: {regions}")
        return regions

    def get_provisioned_regions(self, resource_group: str) -> Dict[str, str]:
        """List per-region storage accounts in the RG.

        Returns a dict mapping region → storage account name.
        Identifies our accounts by the ``managed-by: s247-diag-logs`` tag.
        """
        region_map: Dict[str, str] = {}
        try:
            client = StorageManagementClient(
                self.credential, self.subscription_id
            )
            for acct in client.storage_accounts.list_by_resource_group(resource_group):
                tags = acct.tags or {}
                if tags.get("managed-by") == "s247-diag-logs" and tags.get("purpose") == "diag-logs-regional":
                    if acct.primary_location:
                        region_map[acct.primary_location.lower()] = acct.name
            logger.info(
                f"Provisioned diag storage accounts in '{resource_group}': {region_map}"
            )
        except Exception as e:
            logger.error(
                f"Failed to list storage accounts in '{resource_group}': {e}"
            )
        return region_map

    def get_primary_storage_account(self, resource_group: str) -> Dict[str, str]:
        """Return a stable "primary" regional storage account for non-regional
        log sources (e.g. tenant-scoped Entra ID logs, which have no region).

        Tenant/subscription-scoped diagnostic settings must target *some*
        storage account; there's no natural region for them. We deterministically
        pick the first provisioned regional SA (sorted by region name) so the
        target is stable across scans. BlobLogProcessor already polls all
        ``diag-logs-regional`` accounts, so logs landing here are processed.

        Returns ``{"region", "name", "id"}`` or ``{}`` if none are provisioned
        yet (e.g. before the first scan discovers any resource regions).
        """
        region_map = self.get_provisioned_regions(resource_group)
        if not region_map:
            return {}
        region = sorted(region_map.keys())[0]
        name = region_map[region]
        sa_id = (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Storage/storageAccounts/{name}"
        )
        return {"region": region, "name": name, "id": sa_id}

    # ------------------------------------------------------------------
    # Provision / deprovision
    # ------------------------------------------------------------------

    def provision_storage_account(
        self, resource_group: str, region: str, suffix: str
    ) -> Dict:
        """Create a per-region Storage Account with an insights-logs container.

        Returns a dict with ``storage_account_name``, ``storage_account_id``,
        ``container_name``, and ``region``.
        """
        sa_name = _storage_account_name(region, suffix)
        result: Dict = {
            "storage_account_name": sa_name,
            "storage_account_id": "",
            "container_name": LOGS_CONTAINER,
            "region": region,
        }

        storage_client = StorageManagementClient(
            self.credential, self.subscription_id
        )

        # 1. Create storage account
        try:
            poller = storage_client.storage_accounts.begin_create(
                resource_group_name=resource_group,
                account_name=sa_name,
                parameters={
                    "location": region,
                    "sku": {"name": "Standard_LRS"},
                    "kind": "StorageV2",
                    "properties": {
                        "minimum_tls_version": "TLS1_2",
                        "allow_blob_public_access": False,
                    },
                    "tags": {
                        "managed-by": "s247-diag-logs",
                        "purpose": "diag-logs-regional",
                        "region": region,
                    },
                },
            )
            sa_resource = poller.result()
            result["storage_account_id"] = sa_resource.id or ""
            logger.info(f"Created storage account '{sa_name}' in {region}")
        except Exception as e:
            logger.error(f"Failed to create storage account '{sa_name}': {e}")
            return result

        # 2. Create insights-logs container (diagnostic settings will create
        #    their own containers like insights-logs-{category}, but we
        #    ensure at least the base one exists for validation)
        try:
            storage_client.blob_containers.create(
                resource_group_name=resource_group,
                account_name=sa_name,
                container_name=LOGS_CONTAINER,
                blob_container={},
            )
            logger.info(f"Created container '{LOGS_CONTAINER}' in '{sa_name}'")
        except Exception as e:
            # Container may already exist — that's fine
            if "already exists" in str(e).lower() or "Conflict" in str(type(e).__name__):
                logger.debug(f"Container '{LOGS_CONTAINER}' already exists in '{sa_name}'")
            else:
                logger.warning(f"Failed to create container in '{sa_name}': {e}")

        # 3. Apply resource lock
        self.apply_lock(
            resource_group=resource_group,
            resource_name=sa_name,
            resource_type="Microsoft.Storage/storageAccounts",
        )

        # 4. Apply lifecycle (management) policy: delete blobs in insights-logs-*
        #    after BLOB_RETENTION_DAYS. The function reads blobs incrementally via
        #    block-list checkpoints and never deletes them itself, so this rule is
        #    the sole retention mechanism.
        self.apply_lifecycle_policy(
            storage_client=storage_client,
            resource_group=resource_group,
            sa_name=sa_name,
        )

        return result

    def apply_lifecycle_policy(
        self,
        storage_client: StorageManagementClient,
        resource_group: str,
        sa_name: str,
        retention_days: int = None,
    ) -> bool:
        """Apply a delete-after-N-days lifecycle rule to ``insights-logs-*`` containers.

        Best-effort: returns False on failure but never raises. Idempotent —
        ``create_or_update`` overwrites any prior policy with the same name.
        """
        days = retention_days if retention_days is not None else _get_blob_retention_days()
        policy = {
            "policy": {
                "rules": [
                    {
                        "name": "s247-diag-logs-retention",
                        "enabled": True,
                        "type": "Lifecycle",
                        "definition": {
                            "filters": {
                                "blobTypes": ["appendBlob", "blockBlob"],
                                "prefixMatch": ["insights-logs-"],
                            },
                            "actions": {
                                "baseBlob": {
                                    "delete": {"daysAfterModificationGreaterThan": days}
                                }
                            },
                        },
                    }
                ]
            }
        }
        try:
            storage_client.management_policies.create_or_update(
                resource_group_name=resource_group,
                account_name=sa_name,
                management_policy_name="default",
                properties=policy,
            )
            logger.info(
                "Applied lifecycle policy (delete after %d days) on '%s'",
                days, sa_name,
            )
            return True
        except Exception as e:
            logger.warning(
                "Failed to apply lifecycle policy on '%s' (blobs will not auto-expire): %s",
                sa_name, e,
            )
            return False

    def deprovision_storage_account(
        self, resource_group: str, region: str, sa_name: str
    ) -> bool:
        """Remove lock and delete the per-region storage account.

        Checks for unprocessed blobs first. If any blob in an
        ``insights-logs-*`` container is newer than the configured
        ``SAFE_DELETE_MAX_AGE_DAYS`` (default 7), the deletion is skipped
        to avoid data loss.
        """
        safe_days = _get_safe_delete_days()
        storage_client = StorageManagementClient(
            self.credential, self.subscription_id
        )

        # Safety check: look for recent blobs before deleting
        if self._has_recent_blobs(resource_group, sa_name, storage_client, safe_days):
            logger.warning(
                "Storage account '%s' in %s has unprocessed blobs newer than "
                "%d days — skipping deletion to avoid data loss",
                sa_name, region, safe_days,
            )
            return False

        lock_nm = f"s247-lock-{_sanitize_region(sa_name)}"

        # Order:
        #   1. Remove lock so delete can proceed
        #   2. Attempt delete
        #   3. If delete fails, restore the lock so the SA isn't left
        #      unprotected with unprocessed logs inside it.
        self.remove_lock(resource_group, lock_nm)
        try:
            storage_client.storage_accounts.delete(
                resource_group_name=resource_group,
                account_name=sa_name,
            )
            logger.info(f"Deleted storage account '{sa_name}' in {region}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete storage account '{sa_name}': {e}")
            # Rollback: re-apply the lock so the SA is not left unprotected.
            try:
                self.apply_lock(
                    resource_group=resource_group,
                    resource_name=sa_name,
                    resource_type="Microsoft.Storage/storageAccounts",
                )
                logger.warning(
                    "Re-applied lock on '%s' after delete failure", sa_name
                )
            except Exception as relock_err:
                logger.error(
                    "Could not restore lock on '%s' after delete failure — "
                    "storage account is UNPROTECTED: %s",
                    sa_name, relock_err,
                )
            return False

    def _has_recent_blobs(
        self, resource_group: str, sa_name: str,
        storage_client: StorageManagementClient,
        safe_days: int = SAFE_DELETE_MAX_AGE_DAYS_DEFAULT,
    ) -> bool:
        """Check if a storage account has blobs newer than the safe-delete cutoff."""
        from azure.storage.blob import BlobServiceClient

        cutoff = datetime.now(timezone.utc) - timedelta(days=safe_days)
        try:
            keys = storage_client.storage_accounts.list_keys(resource_group, sa_name)
            acct_key = keys.keys[0].value
            conn_str = (
                f"DefaultEndpointsProtocol=https;AccountName={sa_name};"
                f"AccountKey={acct_key};EndpointSuffix=core.windows.net"
            )
            blob_service = BlobServiceClient.from_connection_string(conn_str)

            for container in blob_service.list_containers():
                cname = container["name"]
                if not cname.startswith("insights-logs"):
                    continue
                container_client = blob_service.get_container_client(cname)
                for blob in container_client.list_blobs():
                    if blob.last_modified and blob.last_modified > cutoff:
                        logger.debug(
                            "Found recent blob %s/%s (modified %s) in '%s'",
                            cname, blob.name, blob.last_modified, sa_name,
                        )
                        return True
            return False
        except Exception as e:
            # If we can't check, err on the side of caution
            logger.warning(
                "Failed to check blobs in '%s', assuming recent blobs exist: %s",
                sa_name, e,
            )
            return True

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile_regions(
        self,
        resource_group: str,
        active_regions: Set[str],
        provisioned_map: Dict[str, str],
        suffix: str,
    ) -> Dict:
        """Add/remove per-region storage accounts to match active regions.

        Returns a summary dict with ``added``, ``removed``, and ``errors``.
        """
        provisioned_regions = set(provisioned_map.keys())
        to_add = active_regions - provisioned_regions
        to_remove = provisioned_regions - active_regions

        summary: Dict = {"added": [], "removed": [], "errors": []}

        for region in to_add:
            try:
                info = self.provision_storage_account(resource_group, region, suffix)
                summary["added"].append(info)
                logger.info(f"Provisioned storage account for region {region}")
            except Exception as e:
                summary["errors"].append({"region": region, "error": str(e)})
                logger.error(f"Error provisioning storage for region {region}: {e}")

        for region in to_remove:
            sa_name = provisioned_map.get(region, "")
            if not sa_name:
                continue
            try:
                success = self.deprovision_storage_account(resource_group, region, sa_name)
                if success:
                    summary["removed"].append(region)
                else:
                    summary["errors"].append(
                        {"region": region, "error": "deprovision returned False"}
                    )
            except Exception as e:
                summary["errors"].append({"region": region, "error": str(e)})
                logger.error(f"Error deprovisioning storage for region {region}: {e}")

        logger.info(
            f"Reconciliation complete — added: {len(summary['added'])}, "
            f"removed: {len(summary['removed'])}, "
            f"errors: {len(summary['errors'])}"
        )
        return summary

    # ------------------------------------------------------------------
    # Resource locks
    # ------------------------------------------------------------------

    def apply_lock(
        self,
        resource_group: str,
        resource_name: str,
        resource_type: str,
    ) -> bool:
        """Apply a CanNotDelete management lock to a resource."""
        if ManagementLockClient is None:
            logger.warning("ManagementLockClient unavailable — skipping lock")
            return False
        lock_client = ManagementLockClient(
            self.credential, self.subscription_id
        )
        parts = resource_type.split("/", 1)
        if len(parts) != 2:
            logger.error(f"Invalid resource_type format: {resource_type}")
            return False

        provider_namespace = parts[0]
        resource_type_short = parts[1]
        lock_nm = f"s247-lock-{_sanitize_region(resource_name)}"

        try:
            lock_client.management_locks.create_or_update_at_resource_level(
                resource_group_name=resource_group,
                resource_provider_namespace=provider_namespace,
                parent_resource_path="",
                resource_type=resource_type_short,
                resource_name=resource_name,
                lock_name=lock_nm,
                parameters={
                    "level": "CanNotDelete",
                    "notes": "Managed by s247-diag-logs — do not remove manually.",
                },
            )
            logger.info(f"Applied lock '{lock_nm}' on {resource_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to apply lock on {resource_name}: {e}")
            return False

    def remove_lock(self, resource_group: str, lock_name: str) -> bool:
        """Remove a management lock by name from a resource group."""
        if ManagementLockClient is None:
            logger.warning("ManagementLockClient unavailable — skipping lock removal")
            return False
        lock_client = ManagementLockClient(
            self.credential, self.subscription_id
        )
        try:
            lock_client.management_locks.delete_at_resource_group_level(
                resource_group_name=resource_group,
                lock_name=lock_name,
            )
            logger.info(f"Removed lock '{lock_name}' from RG '{resource_group}'")
            return True
        except Exception as e:
            if "NotFound" in str(type(e).__name__) or "not found" in str(e).lower():
                logger.debug(f"Lock '{lock_name}' not found — nothing to remove")
                return True
            logger.error(f"Failed to remove lock '{lock_name}': {e}")
            return False
