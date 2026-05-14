"""Blob-backed configuration store for log type configs and settings.

Stores logtype configs, supported Azure log types, and disabled log types
in Azure Blob Storage under the 'config' container. Provides cached reads
and atomic writes.
"""

import json
import logging
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from azure.core import MatchConditions
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

CONTAINER_NAME = "config"
LOGTYPE_CONFIGS_PREFIX = "logtype-configs/"
SUPPORTED_TYPES_BLOB = "azure-log-types.json"
DISABLED_TYPES_BLOB = "disabled-logtypes.json"
CONFIGURED_RESOURCES_BLOB = "configured-resources.json"
CATEGORY_RESOURCE_TYPES_BLOB = "category-resource-types.json"
SCAN_STATE_BLOB = "scan-state.json"

# Sentinel for negative caching (config does not exist in blob)
_MISSING = object()

# In-memory caches (refreshed per function invocation cycle)
_cache = {
    "supported_types": None,
    "disabled_types": None,
    "logtype_configs": {},
    "configured_resources": None,
}


def _get_service_client() -> Optional[BlobServiceClient]:
    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if not conn_str:
        logger.error("AzureWebJobsStorage environment variable is not set")
        return None
    return BlobServiceClient.from_connection_string(conn_str)


def _ensure_container(service_client: BlobServiceClient) -> None:
    container_client = service_client.get_container_client(CONTAINER_NAME)
    if not container_client.exists():
        container_client.create_container()
        logger.info("Created blob container '%s'", CONTAINER_NAME)


def _read_blob(blob_path: str) -> Optional[str]:
    text, _etag = _read_blob_with_etag(blob_path)
    return text


def _read_blob_with_etag(blob_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, etag). (None, None) if missing or error."""
    service_client = _get_service_client()
    if not service_client:
        return None, None
    try:
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME, blob=blob_path
        )
        stream = blob_client.download_blob()
        etag = getattr(stream.properties, "etag", None)
        return stream.readall().decode("utf-8"), etag
    except Exception as e:
        if "BlobNotFound" in str(e) or "not found" in str(e).lower():
            logger.debug("Blob not found: %s", blob_path)
        else:
            logger.error("Failed to read blob %s: %s", blob_path, e)
        return None, None


def _write_blob(blob_path: str, data: str) -> bool:
    service_client = _get_service_client()
    if not service_client:
        return False
    try:
        _ensure_container(service_client)
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME, blob=blob_path
        )
        blob_client.upload_blob(data, overwrite=True)
        return True
    except Exception as e:
        logger.error("Failed to write blob %s: %s", blob_path, e)
        return False


def _write_blob_conditional(
    blob_path: str, data: str, etag: Optional[str]
) -> Tuple[bool, bool]:
    """Conditional write.

    Returns ``(success, conflict)``. If ``etag`` is ``None``, uses IfNoneMatch=*
    so the write only succeeds if the blob does not exist. If ``etag`` is
    provided, uses IfMatch to ensure no one else wrote between our read and
    our write. ``conflict=True`` means the pre-condition failed and the caller
    should retry (re-read, re-apply mutation, re-write).
    """
    service_client = _get_service_client()
    if not service_client:
        return False, False
    try:
        _ensure_container(service_client)
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME, blob=blob_path
        )
        if etag:
            blob_client.upload_blob(
                data,
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        else:
            blob_client.upload_blob(
                data,
                overwrite=True,
                match_condition=MatchConditions.IfMissing,
            )
        return True, False
    except Exception as e:
        msg = str(e)
        if (
            "ConditionNotMet" in msg
            or "BlobAlreadyExists" in msg
            or "412" in msg
            or "409" in msg
        ):
            return False, True
        logger.error("Conditional write failed for %s: %s", blob_path, e)
        return False, False


def _rmw_blob(
    blob_path: str,
    mutate: Callable[[Optional[Dict]], Optional[Dict]],
    max_retries: int = 5,
    default: Optional[Dict] = None,
) -> Optional[Dict]:
    """Read-modify-write a JSON blob with ETag-based optimistic concurrency.

    ``mutate`` receives the current parsed JSON (or ``default`` if the blob is
    absent) and must return the new value to persist. Returning ``None`` aborts
    the write (no-op). Retries on ETag conflict up to ``max_retries``.
    Returns the persisted value, or ``None`` on failure/abort.
    """
    for attempt in range(max_retries):
        raw, etag = _read_blob_with_etag(blob_path)
        if raw is not None:
            try:
                current = json.loads(raw)
            except json.JSONDecodeError:
                logger.error("Corrupt JSON in %s — treating as empty", blob_path)
                current = default
        else:
            current = default

        new_value = mutate(current)
        if new_value is None:
            return None  # caller aborted

        serialized = json.dumps(new_value, indent=2)
        success, conflict = _write_blob_conditional(blob_path, serialized, etag)
        if success:
            return new_value
        if not conflict:
            return None
        # Conflict — retry with fresh read. Short jitter to reduce thrash.
        time.sleep(0.05 * (attempt + 1))
    logger.warning(
        "RMW gave up on %s after %d retries (concurrent contention)",
        blob_path,
        max_retries,
    )
    return None


def _delete_blob(blob_path: str) -> bool:
    service_client = _get_service_client()
    if not service_client:
        return False
    try:
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME, blob=blob_path
        )
        blob_client.delete_blob()
        return True
    except Exception as e:
        if "BlobNotFound" not in str(e):
            logger.error("Failed to delete blob %s: %s", blob_path, e)
        return False


# ─── Supported Azure Log Types ───────────────────────────────────────────────


def get_supported_log_types() -> Dict:
    """Get supported Azure log types (cached in memory, persisted in blob)."""
    if _cache["supported_types"] is not None:
        return _cache["supported_types"]

    data = _read_blob(SUPPORTED_TYPES_BLOB)
    if data:
        _cache["supported_types"] = json.loads(data)
        return _cache["supported_types"]
    return {}


def save_supported_log_types(types_data: Dict) -> bool:
    """Save supported Azure log types to blob and update cache."""
    if _write_blob(SUPPORTED_TYPES_BLOB, json.dumps(types_data, indent=2)):
        _cache["supported_types"] = types_data
        return True
    return False


def is_supported_log_type(category: str) -> bool:
    """Check if a log category matches a supported Azure log type."""
    supported = get_supported_log_types()
    if not supported:
        return False
    normalized = category.replace("-", "").replace("_", "").replace(" ", "").lower()
    return normalized in supported


# ─── Log Type Configs (sourceConfig per category) ────────────────────────────


def _normalize_category(category: str) -> str:
    """Normalize category name to lowercase for consistent blob naming."""
    return category.replace("-", "").replace("_", "").replace(" ", "").lower()


def get_logtype_config(category: str) -> Optional[Dict]:
    """Get the sourceConfig for a specific log category."""
    config_key = f"S247_{_normalize_category(category)}"

    if config_key in _cache["logtype_configs"]:
        cached = _cache["logtype_configs"][config_key]
        # _MISSING sentinel means we already checked and it doesn't exist
        return None if cached is _MISSING else cached

    blob_path = f"{LOGTYPE_CONFIGS_PREFIX}{config_key}.json"
    data = _read_blob(blob_path)
    if data:
        config = json.loads(data)
        _cache["logtype_configs"][config_key] = config
        return config
    # Negative cache — avoid repeated blob reads for missing configs
    _cache["logtype_configs"][config_key] = _MISSING
    return None


def save_logtype_config(category: str, config: Dict) -> bool:
    """Save sourceConfig for a log category."""
    config_key = f"S247_{_normalize_category(category)}"
    blob_path = f"{LOGTYPE_CONFIGS_PREFIX}{config_key}.json"
    if _write_blob(blob_path, json.dumps(config, indent=2)):
        _cache["logtype_configs"][config_key] = config
        return True
    return False


def delete_logtype_config(category: str) -> bool:
    """Delete sourceConfig for a log category."""
    config_key = f"S247_{_normalize_category(category)}"
    blob_path = f"{LOGTYPE_CONFIGS_PREFIX}{config_key}.json"
    if _delete_blob(blob_path):
        _cache["logtype_configs"].pop(config_key, None)
        return True
    return False


def get_all_logtype_configs() -> Dict[str, Dict]:
    """List all stored logtype configs."""
    service_client = _get_service_client()
    if not service_client:
        return {}

    configs = {}
    try:
        container_client = service_client.get_container_client(CONTAINER_NAME)
        for blob in container_client.list_blobs(
            name_starts_with=LOGTYPE_CONFIGS_PREFIX
        ):
            if blob.name.endswith(".json"):
                key = blob.name.replace(LOGTYPE_CONFIGS_PREFIX, "").replace(
                    ".json", ""
                )
                data = _read_blob(blob.name)
                if data:
                    configs[key] = json.loads(data)
    except Exception as e:
        logger.error("Failed to list logtype configs: %s", e)
    return configs


# ─── Disabled Log Types ──────────────────────────────────────────────────────


def get_disabled_log_types() -> List[str]:
    """Get list of disabled log type categories."""
    if _cache["disabled_types"] is not None:
        return list(_cache["disabled_types"])

    data = _read_blob(DISABLED_TYPES_BLOB)
    if data:
        _cache["disabled_types"] = json.loads(data)
        return list(_cache["disabled_types"])
    return []


def save_disabled_log_types(disabled: List[str]) -> bool:
    """Save disabled log types list."""
    if _write_blob(DISABLED_TYPES_BLOB, json.dumps(disabled, indent=2)):
        _cache["disabled_types"] = disabled
        return True
    return False


def disable_log_type(category: str) -> bool:
    """Add a category to the disabled list (concurrency-safe)."""
    def mutate(current):
        disabled = current if isinstance(current, list) else []
        if category.lower() in [d.lower() for d in disabled]:
            return None  # already present — abort write
        return disabled + [category]

    result = _rmw_blob(DISABLED_TYPES_BLOB, mutate, default=[])
    if result is not None:
        _cache["disabled_types"] = result
        return True
    # None may mean abort-no-op (already present) OR failure. Check cache.
    return True


def enable_log_type(category: str) -> bool:
    """Remove a category from the disabled list (concurrency-safe)."""
    def mutate(current):
        disabled = current if isinstance(current, list) else []
        updated = [d for d in disabled if d.lower() != category.lower()]
        if len(updated) == len(disabled):
            return None  # not present — abort
        return updated

    result = _rmw_blob(DISABLED_TYPES_BLOB, mutate, default=[])
    if result is not None:
        _cache["disabled_types"] = result
    return True


def is_log_type_disabled(category: str) -> bool:
    """Check if a category is disabled."""
    disabled = get_disabled_log_types()
    return category.lower() in [d.lower() for d in disabled]


# ─── Configured Resources Tracking ──────────────────────────────────────────


def get_configured_resources() -> Dict:
    """Get the map of configured resources and their log type details.

    Structure: { resource_id: { "categories": [...], "storage_account": "...", "configured_at": "..." } }
    """
    if _cache["configured_resources"] is not None:
        return _cache["configured_resources"]

    data = _read_blob(CONFIGURED_RESOURCES_BLOB)
    if data:
        _cache["configured_resources"] = json.loads(data)
        return _cache["configured_resources"]
    return {}


def save_configured_resources(resources: Dict) -> bool:
    """Save configured resources map."""
    if _write_blob(CONFIGURED_RESOURCES_BLOB, json.dumps(resources, indent=2)):
        _cache["configured_resources"] = resources
        return True
    return False


# ─── Category → Resource Types mapping (from all discovered resources) ────────


def get_category_resource_types() -> Dict:
    """Get category → resource_types mapping built from all discovered resources."""
    data = _read_blob(CATEGORY_RESOURCE_TYPES_BLOB)
    if data:
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            logger.error("Failed to parse category-resource-types blob")
    return {}


def save_category_resource_types(mapping: Dict) -> bool:
    """Save category → resource_types mapping to blob."""
    return _write_blob(CATEGORY_RESOURCE_TYPES_BLOB, json.dumps(mapping, indent=2))


def mark_resource_configured(
    resource_id: str, categories: List[str], storage_account: str
) -> bool:
    """Mark a resource as configured (concurrency-safe RMW)."""
    from datetime import datetime, timezone

    entry = {
        "categories": categories,
        "storage_account": storage_account,
        "configured_at": datetime.now(timezone.utc).isoformat(),
    }

    def mutate(current):
        configured = current if isinstance(current, dict) else {}
        configured[resource_id] = entry
        return configured

    result = _rmw_blob(CONFIGURED_RESOURCES_BLOB, mutate, default={})
    if result is not None:
        _cache["configured_resources"] = result
        return True
    return False


def unmark_resource_configured(resource_id: str) -> bool:
    """Remove a resource from the configured tracking (concurrency-safe RMW)."""
    def mutate(current):
        configured = current if isinstance(current, dict) else {}
        if resource_id not in configured:
            return None  # not present — abort
        configured = dict(configured)
        del configured[resource_id]
        return configured

    result = _rmw_blob(CONFIGURED_RESOURCES_BLOB, mutate, default={})
    if result is not None:
        _cache["configured_resources"] = result
    return True


def clear_cache():
    """Clear all in-memory caches. Call at the start of each function invocation."""
    _cache["supported_types"] = None
    _cache["disabled_types"] = None
    _cache["logtype_configs"] = {}
    _cache["configured_resources"] = None


# ------------------------------------------------------------------
# Scan state (blob-backed, not app settings)
# ------------------------------------------------------------------

def save_scan_state(state: Dict) -> bool:
    """Save scan state (last scan time, stats) to blob storage.

    Full overwrite — callers replacing the entire state at scan start/end.
    For partial updates (e.g. clearing ``in_progress``), use
    :func:`update_scan_state` which is concurrency-safe.
    """
    return _write_blob(SCAN_STATE_BLOB, json.dumps(state, indent=2))


def update_scan_state(patch: Dict) -> Optional[Dict]:
    """Merge ``patch`` into the current scan state (concurrency-safe RMW)."""
    def mutate(current):
        state = dict(current) if isinstance(current, dict) else {}
        state.update(patch)
        return state

    return _rmw_blob(SCAN_STATE_BLOB, mutate, default={})


def try_acquire_scan_lock(ttl_seconds: int = 900) -> bool:
    """Atomically mark a scan as in-progress. Returns ``True`` if the caller
    acquired the lock, ``False`` if another scan is already running.

    A stale ``in_progress`` flag older than ``ttl_seconds`` is treated as a
    crashed scan and overwritten so a wedged flag can't permanently block
    future scans.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    acquired = {"acquired": False}

    def mutate(current):
        state = dict(current) if isinstance(current, dict) else {}
        if state.get("in_progress"):
            started = state.get("scan_started_at") or state.get("last_scan_time")
            if started:
                try:
                    started_dt = datetime.fromisoformat(
                        started.replace("Z", "+00:00")
                    )
                    age = (now - started_dt).total_seconds()
                    if age < ttl_seconds:
                        return None  # active scan — abort
                except Exception:
                    pass
        state["in_progress"] = True
        state["scan_started_at"] = now.isoformat()
        acquired["acquired"] = True
        return state

    _rmw_blob(SCAN_STATE_BLOB, mutate, default={})
    return acquired["acquired"]


def get_scan_state() -> Dict:
    """Load scan state from blob storage."""
    raw = _read_blob(SCAN_STATE_BLOB)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Failed to parse scan state blob")
    return {}
