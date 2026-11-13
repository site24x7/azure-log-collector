"""Delete Collector: remove everything this collector set up, then its own resource group.

Requested from Site24x7 (POST /api/delete-collector), run as a queued job by
ScanWorker under the scan lock:

1. Log collection is switched off and every diagnostic setting of this collector
   is removed, its storage emptied (shared/collection.py). If anything is left,
   the job stops: the group is never deleted while a setting still sends to it.
2. The resource group must hold only this collector's resources. A group chosen
   on the deployment form may hold the customer's own: then nothing is deleted
   and the resources found are reported.
3. The collector's locks (s247-lock-*) are removed. That needs the built-in
   Locks Contributor role on the group, which the template grants; a collector
   deployed before it reports so and the admin removes them.
4. Site24x7 is told the collector is being deleted, then the group delete is
   started (not awaited: it removes this Function App too).

Left for an admin, and listed in the result: the Reader and Monitoring
Contributor grants on the monitored subscriptions, the subscription-level
deployment record, and an Entra ID diagnostic setting a tenant admin made.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DELETE_ACTION = "delete_collector"
# Locks the template and setup.sh create; other locks are never touched.
OWN_LOCK_PREFIX = "s247-lock-"
# Created in the group by Azure itself for Application Insights (smart detection).
AZURE_CREATED_TYPES = frozenset({
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.insights/actiongroups",
})
MAX_REPORTED = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resource_group() -> str:
    return os.environ.get("RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg"))


def _hosting_subscription() -> str:
    from shared.subscriptions import configured_subscription_ids
    ids = configured_subscription_ids()
    return ids[0] if ids else ""


def request_delete() -> Optional[Dict]:
    """Record the request and return the queue message (None when it could not be saved)."""
    from shared.config_store import update_collection_state
    if update_collection_state({"delete": {"state": "pending", "requested_at": _now()}}) is None:
        return None
    return {"requested_at": _now(), "source": "site24x7", "action": DELETE_ACTION}


def foreign_resources(resources) -> List[str]:
    """Names of resources in the group that are not this collector's (empty: all are its own)."""
    from shared.region_manager import collector_id, is_own_account

    me = collector_id()
    suffix = os.environ.get("DIAG_STORAGE_SUFFIX", "").strip().lower()
    foreign = []
    for r in resources:
        rtype = (r.type or "").lower()
        name = r.name or ""
        tags = r.tags or {}
        if rtype in AZURE_CREATED_TYPES:
            continue
        own = False
        if tags.get("managed-by") == "s247-diag-logs":
            purpose = tags.get("purpose", "")
            owner = (tags.get("s247-collector") or "").strip().lower()
            if rtype == "microsoft.storage/storageaccounts" and purpose in ("diag-logs-regional", "diag-logs-tenant"):
                own = is_own_account(name, tags, purpose, r.location or "")
            elif owner:
                own = owner == me
            else:
                # Deployed by the template for this collector: its names carry the deployment suffix.
                own = bool(suffix) and suffix in name.lower() or (rtype == "microsoft.web/sites" and name.lower() == me)
        if not own:
            foreign.append(f"{name} ({r.type})")
    return foreign


def _remove_locks(credential, subscription_id: str, resource_group: str) -> Dict:
    """Remove the collector's locks in the group; the result says which could not be."""
    from azure.mgmt.resource.locks import ManagementLockClient

    client = ManagementLockClient(credential, subscription_id)
    removed, failed, others = 0, [], []
    for lock in client.management_locks.list_at_resource_group_level(resource_group):
        if not (lock.name or "").startswith(OWN_LOCK_PREFIX):
            others.append(lock.name)
            continue
        scope = lock.id.rsplit("/providers/Microsoft.Authorization/locks/", 1)[0]
        try:
            client.management_locks.delete_by_scope(scope, lock.name)
            removed += 1
        except Exception as e:
            failed.append(f"{lock.name}: {getattr(e, 'status_code', None) or type(e).__name__}")
    return {"removed": removed, "failed": failed, "others": others}


def _save(patch: Dict) -> None:
    from shared.config_store import update_collection_state
    update_collection_state({"delete": dict(patch, updated_at=_now())})


def delete_collector() -> Dict:
    """Run the deletion (the caller holds the scan lock). Returns the final state."""
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource import ResourceManagementClient
    from shared import collection
    from shared.config_store import update_collection_state

    subscription = _hosting_subscription()
    resource_group = _resource_group()
    _save({"state": "removing_settings"})
    # 1. Off, then every setting removed and the storage emptied (as the Log Collection switch).
    if collection.request_change(False) is None:
        return _finish({"state": "failed", "reason": "state_unsaved"})
    stop = collection.stop_collection()
    if stop.get("state") != "done":
        return _finish({"state": "failed", "reason": "settings_remain", "errors": stop.get("errors", 0),
                        "unresolved": stop.get("unresolved", 0)})
    try:
        credential = DefaultAzureCredential()
        resources_client = ResourceManagementClient(credential, subscription)
        # 2. Only this collector's resources in the group.
        foreign = foreign_resources(resources_client.resources.list_by_resource_group(resource_group))
        if foreign:
            return _finish({"state": "failed", "reason": "foreign_resources",
                            "foreign": foreign[:MAX_REPORTED], "foreign_count": len(foreign)})
        # 3. The collector's locks.
        locks = _remove_locks(credential, subscription, resource_group)
        if locks["failed"] or locks["others"]:
            return _finish({"state": "failed", "reason": "locks", "locks_failed": locks["failed"][:MAX_REPORTED],
                            "other_locks": locks["others"][:MAX_REPORTED]})
        # 4. Tell Site24x7 first: after the delete starts this app may stop at any moment.
        _save({"state": "deleting", "resource_group": resource_group})
        try:
            from shared.registration import register_with_site24x7
            register_with_site24x7(timeout=30)
        except Exception:
            pass
        resources_client.resource_groups.begin_delete(resource_group)
        logger.warning("Delete Collector: deletion of resource group %s started", resource_group)
        return _finish({"state": "deleting", "resource_group": resource_group}, report=False)
    except Exception as e:
        logger.error("Delete Collector failed: %s", type(e).__name__)
        return _finish({"state": "failed", "reason": "azure_error",
                        "error": str(getattr(e, "status_code", "") or type(e).__name__)})


def _finish(outcome: Dict, report: bool = True) -> Dict:
    outcome["finished_at"] = _now()
    try:
        _save(outcome)
    except Exception:
        pass
    try:
        from shared.debug_logger import log_audit, log_event
        log_event("info" if outcome["state"] == "deleting" else "warning", "DeleteCollector",
                  f"Delete Collector: {outcome['state']} {outcome.get('reason', '')}".strip(), outcome)
        log_audit("delete_collector", "DeleteCollector", outcome)
    except Exception:
        pass
    if report:
        try:
            from shared.registration import register_with_site24x7
            register_with_site24x7(timeout=30)
        except Exception:
            pass
    return outcome


def mark_abandoned() -> None:
    try:
        _save({"state": "failed", "reason": "abandoned"})
    except Exception:
        pass
