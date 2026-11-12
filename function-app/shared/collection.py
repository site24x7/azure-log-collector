"""Switching log collection off (Site24x7's Log Collection switch) and on again.

Off: forwarding stops at once, then a queued job (ScanWorker) removes this
collector's diagnostic settings from every resource and clears the logs already
in its storage accounts. The accounts themselves stay: the resource group lock
forbids deleting them, and a storage name released by a delete can be taken by
anyone in Azure. They are kept idle (one day retention), which costs next to
nothing. While collection is off, no scan sets anything up again.

On: forwarding resumes and a scan is queued, which sets everything up again.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

STOP_ACTION = "stop_collection"
START_SOURCE = "collection"
# Containers Azure Monitor writes diagnostic logs to.
LOG_CONTAINER_PREFIX = "insights-logs-"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_change(enabled: bool) -> Optional[Dict]:
    """Record the switch and return the queue message to send (None when the state could not be saved).

    Switching off again while it is already off queues the removal again, so a
    removal that left something behind can be retried."""
    from shared.config_store import save_processing_enabled, update_collection_state

    if enabled:
        state = update_collection_state({"enabled": True, "changed_at": _now(), "stop": None})
        if state is None or not save_processing_enabled(True):
            return None
        return {"requested_at": _now(), "source": START_SOURCE}
    # Forwarding stops first: nothing more is sent, whatever happens next.
    if not save_processing_enabled(False):
        return None
    state = update_collection_state({"enabled": False, "changed_at": _now(),
                                     "stop": {"state": "pending", "requested_at": _now()}})
    if state is None:
        return None
    return {"requested_at": _now(), "source": "site24x7", "action": STOP_ACTION}


def _clear_storage(region_mgr, resource_group: str) -> Dict[str, int]:
    """Delete the log containers of this collector's storage accounts and keep the accounts idle."""
    from azure.mgmt.storage import StorageManagementClient
    from azure.storage.blob import BlobServiceClient
    from shared.region_manager import IDLE_RETENTION_DAYS, REGIONAL_PURPOSE, TENANT_STORAGE_PURPOSE, is_own_account

    counts = {"accounts": 0, "containers_cleared": 0, "storage_errors": 0}
    client = StorageManagementClient(region_mgr.credential, region_mgr.subscription_id)
    for acct in client.storage_accounts.list_by_resource_group(resource_group):
        purpose = (acct.tags or {}).get("purpose", "")
        if purpose not in (REGIONAL_PURPOSE, TENANT_STORAGE_PURPOSE) or \
                not is_own_account(acct.name, acct.tags, purpose, acct.primary_location or ""):
            continue
        counts["accounts"] += 1
        if not region_mgr.set_retention(resource_group, acct.name, IDLE_RETENTION_DAYS):
            counts["storage_errors"] += 1
        try:
            key = client.storage_accounts.list_keys(resource_group, acct.name).keys[0].value
            service = BlobServiceClient(account_url=f"https://{acct.name}.blob.core.windows.net",
                                        credential={"account_name": acct.name, "account_key": key})
            for container in service.list_containers(name_starts_with=LOG_CONTAINER_PREFIX):
                service.delete_container(container["name"])
                counts["containers_cleared"] += 1
        except Exception as e:
            counts["storage_errors"] += 1
            logger.warning("Could not clear the logs of storage account %s: %s", acct.name, type(e).__name__)
    return counts


def stop_collection() -> Dict:
    """Remove this collector's diagnostic settings everywhere and clear its stored logs.

    The caller holds the scan lock. Records the outcome in the collection state
    (``stop``) and returns it."""
    from shared.azure_manager import AzureManager
    from shared.config_store import get_configured_resources, unmark_resource_configured, update_collection_state
    from shared.region_manager import RegionManager
    from shared.subscriptions import monitored_subscriptions

    update_collection_state({"stop": {"state": "running", "started_at": _now()}})
    outcome: Dict = {"state": "failed", "finished_at": ""}
    try:
        subscription_ids, _names, list_error = monitored_subscriptions()
        if not subscription_ids:
            raise RuntimeError("no subscriptions configured")
        result = AzureManager().remove_all_diagnostic_settings(subscription_ids, get_configured_resources())
        not_cleared = sum(1 for rid in result.get("resolved_ids", []) if not unmark_resource_configured(rid))
        resource_group = os.environ.get("RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg"))
        storage = _clear_storage(RegionManager(subscription_ids[0]), resource_group)
        complete = not (result["errors"] or result["unresolved"] or not_cleared or list_error)
        outcome = {
            "state": "done" if complete else "partial",
            "removed": result["removed"], "errors": result["errors"] + not_cleared,
            "unresolved": result["unresolved"], **storage,
        }
    except Exception as e:
        logger.error("Stopping log collection failed: %s", type(e).__name__)
        outcome = {"state": "failed", "error": type(e).__name__}
    outcome["finished_at"] = _now()
    update_collection_state({"stop": outcome})
    try:
        from shared.debug_logger import log_audit, log_event
        log_event("info" if outcome["state"] == "done" else "warning", "LogCollection",
                  f"Log collection switched off: {outcome['state']}", outcome)
        log_audit("stop_collection", "LogCollection", outcome)
    except Exception:
        pass
    return outcome


def mark_stop_abandoned() -> None:
    """The queued removal was given up after its retries: say so instead of 'pending'."""
    try:
        from shared.config_store import update_collection_state
        update_collection_state({"stop": {"state": "failed", "error": "abandoned", "finished_at": _now()}})
    except Exception:
        pass


def is_stop_request(body: str) -> bool:
    try:
        return json.loads(body).get("action") == STOP_ACTION
    except Exception:
        return False
