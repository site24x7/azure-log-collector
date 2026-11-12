"""Switching log collection off (Site24x7's Log Collection switch) and on again.

Off: forwarding stops at once, then a queued job (ScanWorker) removes this
collector's diagnostic settings from every resource and empties its storage
accounts: every container is deleted, with the processing checkpoints that
pointed into them. Azure Monitor can still write for a while after a setting is
removed, so the accounts are swept again every SWEEP_INTERVAL_MINUTES for
SWEEP_WINDOW_HOURS. The accounts themselves stay, empty: the resource group
lock forbids deleting them, and an empty account costs nothing. While
collection is off, no scan sets anything up again.

On: forwarding resumes and a scan is queued, which sets everything up again.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

STOP_ACTION = "stop_collection"
START_SOURCE = "collection"
# After the removal, late writes of Azure Monitor are swept this often, for this long.
SWEEP_INTERVAL_MINUTES = 30
SWEEP_WINDOW_HOURS = 24


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
    """Empty this collector's storage accounts: every container goes, and the
    processing checkpoints that pointed into them. The accounts stay, idle."""
    from azure.mgmt.storage import StorageManagementClient
    from azure.storage.blob import BlobServiceClient
    from shared.region_manager import IDLE_RETENTION_DAYS, REGIONAL_PURPOSE, TENANT_STORAGE_PURPOSE, is_own_account

    counts = {"accounts": 0, "containers_cleared": 0, "storage_errors": 0}
    cleared = []
    client = StorageManagementClient(region_mgr.credential, region_mgr.subscription_id)
    for acct in client.storage_accounts.list_by_resource_group(resource_group):
        purpose = (acct.tags or {}).get("purpose", "")
        # Only the accounts that hold collected logs, and only this collector's:
        # never the Function App's own storage (its code, keys and state).
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
            for container in service.list_containers():
                service.delete_container(container["name"])
                counts["containers_cleared"] += 1
            cleared.append(acct.name)
        except Exception as e:
            counts["storage_errors"] += 1
            logger.warning("Could not empty storage account %s: %s", acct.name, type(e).__name__)
    if cleared and not _drop_checkpoints(cleared):
        counts["storage_errors"] += 1
    return counts


def _drop_checkpoints(accounts) -> bool:
    """Forget the processing checkpoints of blobs in these accounts (they are gone)."""
    from azure.core import MatchConditions
    from azure.storage.blob import BlobServiceClient
    from BlobLogProcessor import CHECKPOINT_BLOB, CHECKPOINT_CONTAINER, _load_checkpoints_with_etag
    from shared.config_store import is_conflict

    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if not conn_str:
        return True
    prefixes = tuple(f"{name}/" for name in accounts)
    blob = BlobServiceClient.from_connection_string(conn_str).get_container_client(
        CHECKPOINT_CONTAINER).get_blob_client(CHECKPOINT_BLOB)
    for _ in range(5):
        try:
            checkpoints, etag = _load_checkpoints_with_etag(conn_str)
            kept = {k: v for k, v in checkpoints.items() if not str(k).startswith(prefixes)}
            if len(kept) == len(checkpoints):
                return True
            if etag:
                blob.upload_blob(json.dumps(kept), overwrite=True, etag=etag,
                                 match_condition=MatchConditions.IfNotModified)
            else:
                blob.upload_blob(json.dumps(kept), overwrite=True)
            return True
        except Exception as e:
            if is_conflict(e):
                continue
            logger.warning("Could not drop the checkpoints of the emptied accounts: %s", type(e).__name__)
            return False
    return False


def _clear_all() -> Dict[str, int]:
    from shared.region_manager import RegionManager
    from shared.subscriptions import configured_subscription_ids

    subscription_ids = configured_subscription_ids()
    if not subscription_ids:
        raise RuntimeError("no subscriptions configured")
    resource_group = os.environ.get("RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg"))
    return _clear_storage(RegionManager(subscription_ids[0]), resource_group)


def sweep_if_due(now: Optional[datetime] = None) -> Optional[Dict]:
    """While collection is off: empty the accounts again when late logs may have
    arrived (every SWEEP_INTERVAL_MINUTES, for SWEEP_WINDOW_HOURS after the
    removal finished). Called by the processor's timer; returns what was cleared,
    or None when no sweep was due. Never raises."""
    from shared.config_store import get_collection_state, update_collection_state

    now = now or datetime.now(timezone.utc)
    try:
        state = get_collection_state(strict=True)
        stop = state.get("stop") if isinstance(state.get("stop"), dict) else {}
        if state.get("enabled") is not False or stop.get("state") not in ("done", "partial"):
            return None
        finished = datetime.fromisoformat(str(stop.get("finished_at", "")))
        last = datetime.fromisoformat(str(stop.get("swept_at") or stop.get("finished_at")))
        if now - finished > timedelta(hours=SWEEP_WINDOW_HOURS) or \
                now - last < timedelta(minutes=SWEEP_INTERVAL_MINUTES):
            return None
        # Claimed first, so workers starting together do not all sweep.
        if update_collection_state({"stop": dict(stop, swept_at=now.isoformat())}) is None:
            return None
        counts = _clear_all()
        if counts["containers_cleared"]:
            logger.info("Late logs swept from the emptied storage accounts: %d containers", counts["containers_cleared"])
        return counts
    except Exception as e:
        logger.warning("Storage sweep skipped: %s", type(e).__name__)
        return None


def stop_collection() -> Dict:
    """Remove this collector's diagnostic settings everywhere and clear its stored logs.

    The caller holds the scan lock. Records the outcome in the collection state
    (``stop``) and returns it."""
    from shared.azure_manager import AzureManager
    from shared.config_store import get_configured_resources, unmark_resource_configured, update_collection_state
    from shared.subscriptions import monitored_subscriptions

    update_collection_state({"stop": {"state": "running", "started_at": _now()}})
    outcome: Dict = {"state": "failed", "finished_at": ""}
    try:
        subscription_ids, _names, list_error = monitored_subscriptions()
        if not subscription_ids:
            raise RuntimeError("no subscriptions configured")
        result = AzureManager().remove_all_diagnostic_settings(subscription_ids, get_configured_resources())
        not_cleared = sum(1 for rid in result.get("resolved_ids", []) if not unmark_resource_configured(rid))
        storage = _clear_all()
        complete = not (result["errors"] or result["unresolved"] or not_cleared or list_error
                        or storage["storage_errors"])
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
