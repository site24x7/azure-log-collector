"""Report collector identity after scans, restarts and key changes.

Current operational health is also available through authenticated GET status,
independently of whether the collector can register with Site24x7. There is no
periodic full-registration heartbeat. Keys and raw backend response bodies are
never included in the operational snapshot.
"""

import hashlib
import json
import secrets
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_ARM = "https://management.azure.com"
_ARM_API_VERSION = "2022-03-01"
_PROCESSING_RUNS_FOR_STATUS = 12

# How often a worker re-reads its function key to notice a rotation.
KEY_CHECK_INTERVAL_SEC = 15 * 60
# Per worker process: hash of the key Site24x7 last accepted, and when the key
# was last checked (or a registration last attempted).
_registered_key_hash: Optional[str] = None
_last_key_check = 0.0


# ARM IDs are case-insensitive, but Site24x7 validates the canonical spelling
# (resourceGroups, Microsoft.Web/sites) case-sensitively, so normalise it here.
_SITE_ID = re.compile(
    r"/subscriptions/([0-9a-fA-F-]{36})/resourcegroups/([-\w._()]{1,90})"
    r"/providers/microsoft\.web/sites/([-\w]{1,60})", re.IGNORECASE | re.ASCII)


def _canonical_site_id(resource_id: str) -> str:
    match = _SITE_ID.fullmatch(resource_id)
    if not match:
        return ""
    subscription, resource_group, site = match.groups()
    return (f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Web/sites/{site}")


def _site_resource_id() -> str:
    # The deployment subscription is independent of monitored subscriptions.
    explicit = _canonical_site_id(os.environ.get("COLLECTOR_RESOURCE_ID", "").strip())
    if explicit:
        return explicit
    sub_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()
    resource_group = os.environ.get(
        "RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg")
    )
    func_app_name = os.environ.get("WEBSITE_SITE_NAME", "")
    if not (sub_id and func_app_name):
        return ""
    return (f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Web/sites/{func_app_name}")


# The functions Site24x7 calls, and the dashboard (with the functions its page
# calls), which Site24x7 opens for its admins. Site24x7 gets its own function
# key, named "site24x7", on these only: never a host key, and the customer can
# revoke it without touching their keys.
SITE24X7_FUNCTIONS = (
    "GetStatus", "TriggerScan", "StopProcessing",
    "GetIgnoreList", "UpdateIgnoreList", "GetDisabledLogTypes", "UpdateDisabledLogTypes",
    "UpdateEntraLogTypes", "GetGeneralLogType", "UpdateGeneralLogType", "UpdateSettings",
    "GetConfiguredResources", "CheckUpdate", "RemoveDiagSettings",
    "Dashboard", "GetDebugInfo", "HealthCheck",
)
SITE24X7_KEY_NAME = "site24x7"


def site24x7_functions() -> List[str]:
    """SITE24X7_FUNCTIONS minus any the customer listed in SITE24X7_KEY_EXCLUDE
    (e.g. "RemoveDiagSettings,CheckUpdate" keeps those actions out of Site24x7;
    "*" gives Site24x7 no key at all, and the collector then stops reporting)."""
    raw = os.environ.get("SITE24X7_KEY_EXCLUDE", "")
    excluded = {f.strip().lower() for f in raw.split(",") if f.strip()}
    if "*" in excluded:
        return []
    return [f for f in SITE24X7_FUNCTIONS if f.lower() not in excluded]


def _arm(method: str, url: str, token: str, body: Optional[Dict] = None):
    headers = {"Authorization": f"Bearer {token}"}
    if body is None:
        headers["Content-Length"] = "0"
    return requests.request(method, f"{_ARM}{url}?api-version={_ARM_API_VERSION}",
                            headers=headers, json=body, timeout=30, allow_redirects=False)


def get_function_key(resource_id: str) -> Optional[str]:
    """The key Site24x7 uses: the same "site24x7" function key on every function
    in site24x7_functions(), created (or re-created after a rotation that left
    the functions with different values) through the managed identity (RG
    Contributor). On functions the customer excluded the key is overwritten
    with a value nobody holds (the resource group lock forbids deleting keys).
    Returns None when it cannot be read or set, or when every function is excluded."""
    from azure.identity import DefaultAzureCredential

    token = DefaultAzureCredential().get_token(f"{_ARM}/.default").token
    wanted = site24x7_functions()
    for function in SITE24X7_FUNCTIONS:
        if function not in wanted:
            _revoke(resource_id, function, token)
    if not wanted:
        return None
    values = {}
    for function in wanted:
        resp = _arm("POST", f"{resource_id}/functions/{function}/listkeys", token)
        if not resp.ok:
            _record("warning", f"Could not read the Site24x7 function key (ARM listkeys HTTP {resp.status_code})",
                    {"stage": "listKeys", "function": function, "http_status": resp.status_code,
                     "hint": "the app's managed identity needs Contributor on its resource group"})
            return None
        keys = resp.json() or {}
        values[function] = keys.get(SITE24X7_KEY_NAME) if isinstance(keys, dict) else None
    distinct = {v for v in values.values() if v}
    key = distinct.pop() if len(distinct) == 1 and all(values.values()) else None
    if not key:
        # Missing somewhere, or rotated on some functions only: one new value for all.
        key = secrets.token_urlsafe(40)
        for function in wanted:
            resp = _arm("PUT", f"{resource_id}/functions/{function}/keys/{SITE24X7_KEY_NAME}", token,
                        {"properties": {"name": SITE24X7_KEY_NAME, "value": key}})
            if not resp.ok:
                _record("warning", f"Could not set the Site24x7 function key (ARM HTTP {resp.status_code})",
                        {"stage": "setKey", "function": function, "http_status": resp.status_code})
                return None
        logger.info("Registration: Site24x7 function key set on %d functions", len(wanted))
    return key


def _revoke(resource_id: str, function: str, token: str) -> None:
    """Take Site24x7's key away from one function: overwrite it with a random
    value nobody holds (a delete is refused while the resource group is locked)."""
    resp = _arm("POST", f"{resource_id}/functions/{function}/listkeys", token)
    keys = resp.json() if resp.ok else {}
    if isinstance(keys, dict) and keys.get(SITE24X7_KEY_NAME):
        _arm("PUT", f"{resource_id}/functions/{function}/keys/{SITE24X7_KEY_NAME}", token,
             {"properties": {"name": SITE24X7_KEY_NAME, "value": secrets.token_urlsafe(40)}})


def build_status_summary(include_names: bool = True) -> Dict:
    """Read-only status shown to every Site24x7 user; contains no secrets."""
    from shared.config_store import get_scan_state, get_configured_resources, get_processing_enabled
    from shared.debug_logger import get_processing_stats

    scan = get_scan_state() or {}
    runs = get_processing_stats(limit=_PROCESSING_RUNS_FOR_STATUS) or []
    errors = scan.get("errors", 0)
    summary = {
        "last_scan_time": scan.get("last_scan_time", ""),
        "scan_in_progress": bool(scan.get("in_progress", False)),
        "total_resources": scan.get("total_resources", 0),
        "active_resources": scan.get("active_resources", 0),
        "ignored_resources": scan.get("ignored_resources", 0),
        "scan_errors": len(errors) if isinstance(errors, list) else errors,
        "s247_reachable": scan.get("s247_reachable"),
        "processing_enabled": get_processing_enabled(),
        "auto_scan_enabled": os.environ.get("AUTO_SCAN_ENABLED", "true").lower() == "true",
        "last_processing_time": runs[0].get("timestamp", "") if runs else "",
        "recent_uploaded": sum(int(r.get("uploaded", 0) or 0) for r in runs),
        "recent_dropped": sum(int(r.get("dropped", 0) or 0) for r in runs),
        # Blobs / blocks too large to read in these runs (their records are not counted).
        "recent_skipped_parts": sum(int(r.get("skipped_parts", 0) or 0) for r in runs),
        # The latest run's uploads were refused for the Device Key (HTTP 401/403).
        "upload_refused": bool(runs[0].get("upload_refused", False)) if runs else False,
        # Records the latest run could not send; they are re-sent on the next run,
        # so this goes back to 0 as soon as uploads work again.
        "pending_records": int(runs[0].get("retrying", 0) or 0) if runs else 0,
        # The recent counts cover these runs; the oldest one's time says since when.
        "recent_since": runs[-1].get("timestamp", "") if runs else "",
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "s247_errors": safe_scan_errors(scan.get("s247_errors")),
        "planned_regions": int(scan.get("planned_regions", 0) or 0),
        "planned_resource_types": int(scan.get("planned_resource_types", 0) or 0),
    }
    # The configured subscriptions, then the others the last scan found the identity can see.
    from shared.subscriptions import configured_subscription_ids
    from shared.config_store import get_resource_inventory
    subscriptions = configured_subscription_ids()
    try:
        seen = (get_resource_inventory() or {}).get("subscriptions") or []
    except Exception:
        seen = []
    for raw in seen if isinstance(seen, list) else []:
        try:
            subscription = str(uuid.UUID(str(raw).strip()))
        except (ValueError, AttributeError):
            continue
        if subscription not in subscriptions:
            subscriptions.append(subscription)
    summary["subscription_ids"] = subscriptions[:50]
    summary["subscriptions_truncated"] = len(subscriptions) > 50
    if "regions_count" in scan:
        summary["regions_count"] = scan["regions_count"]
    try:
        configured = get_configured_resources()
        if isinstance(configured, dict):
            summary["configured_resources"] = len(configured)
    except Exception:
        pass  # absence of this count must not block registration
    # Last: names fill only what the other fields leave of the status budget.
    # Only actual attempts establish upload health; an empty/paused run does not clear an error.
    for run in get_processing_stats(limit=50) or []:
        if run.get("upload_status") in ("ok", "failed", "refused", "rejected"):
            summary["upload_status"] = run["upload_status"]
            summary["upload_status_time"] = run.get("upload_status_time", run.get("timestamp", ""))
            code = run.get("upload_http_status")
            if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
                summary["upload_http_status"] = code
            break
    if include_names:
        _add_subscription_names(summary, _subscription_names(summary["subscription_ids"]))
    return summary


def safe_scan_errors(errors):
    """Closed diagnostic contract: never expose raw backend messages or response bodies."""
    result = []
    for error in errors[:5] if isinstance(errors, list) else []:
        if not isinstance(error, dict) or error.get("phase") not in ("catalog", "preflight", "logtype_creation"):
            continue
        safe = {"phase": error["phase"]}
        code = error.get("http_status")
        if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
            safe["http_status"] = code
        result.append(safe)
    return result


_NAME_MAX = 64
# Site24x7 stores the status in a 4000-character column and rejects longer ones.
_STATUS_BUDGET = 3800
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _subscription_names(subscription_ids) -> Dict[str, str]:
    """Display names of the monitored subscriptions, as the Azure portal shows them.

    One ARM list call (the managed identity has Reader on each monitored
    subscription). Best effort: on any failure the page falls back to the IDs.
    """
    # Only with a managed identity (i.e. on Azure); elsewhere the credential
    # chain would spend a long time failing.
    if not subscription_ids or not (os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT")):
        return {}
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resource import SubscriptionClient

        wanted = set(subscription_ids)
        names = {}
        for sub in SubscriptionClient(DefaultAzureCredential()).subscriptions.list():
            sub_id = (getattr(sub, "subscription_id", "") or "").lower()
            name = _CONTROL_CHARS.sub("", getattr(sub, "display_name", "") or "").strip()[:_NAME_MAX]
            if sub_id in wanted and name:
                names[sub_id] = name
        return names
    except Exception as e:
        logger.warning("Registration: could not read subscription names (%s)", type(e).__name__)
        return {}


def _add_subscription_names(summary: Dict, names: Dict[str, str]) -> None:
    """Add names in subscription order while the status stays within Site24x7's limit."""
    kept: Dict[str, str] = {}
    for sub_id in summary.get("subscription_ids", []):
        if sub_id not in names:
            continue
        kept[sub_id] = names[sub_id]
        trial = dict(summary, subscription_names=kept)
        if len(json.dumps(trial, separators=(",", ":"))) > _STATUS_BUDGET:
            del kept[sub_id]
            break
    if kept:
        summary["subscription_names"] = kept


def build_registration() -> Optional[Dict[str, str]]:
    """Fields for /applog/azure/register_collector, or None when not on Azure."""
    from shared.updater import get_local_version

    resource_id = _site_resource_id()
    host = os.environ.get("WEBSITE_HOSTNAME", "")
    if not (resource_id and host):
        logger.info("Registration: not running in an Azure Function App — skipped")
        return None
    function_key = get_function_key(resource_id)
    if not function_key:
        return None
    parts = resource_id.split("/")
    return {
        "functionKey": function_key,
        "resourceUrl": f"https://{host}",
        "resourceId": resource_id,
        "functionAppName": parts[8],
        "subscriptionId": parts[2],
        "resourceGroup": parts[4],
        "region": os.environ.get("REGION_NAME", os.environ.get("DIAG_SEED_REGION", "")),
        "collectorVersion": get_local_version() or "",
        "status": json.dumps(build_status_summary(), separators=(",", ":")),
    }


def register_with_site24x7(timeout: float = 60) -> bool:
    """Best effort: a failure here must never fail or delay the scan.

    ``timeout`` bounds the call to Site24x7; callers answering an HTTP request
    pass a short one so they still answer in time.
    """
    try:
        fields = build_registration()
        if not fields:
            return False
        from shared.site24x7_client import Site24x7Client

        ok = Site24x7Client().register_collector(fields, timeout=timeout)
        if ok:
            global _registered_key_hash
            _registered_key_hash = _key_hash(fields["functionKey"])
            logger.info("Registration: Site24x7 updated")
        else:
            _record("warning", "Site24x7 did not accept the collector registration",
                    {"stage": "register_collector", "resource_url": fields["resourceUrl"],
                     "hint": "check the Device Key and that Site24x7 is reachable"})
        return ok
    except Exception as e:  # never surface the key: record the type only
        _record("warning", f"Collector registration skipped after {type(e).__name__}",
                {"stage": "registration", "error_type": type(e).__name__})
        return False


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def sync_registration(timeout: float = 30) -> bool:
    """Register when this worker has not yet (first run after a restart, a
    redeploy or a new instance) or when the function key has changed since the
    last registration. The key is read from ARM at most every
    KEY_CHECK_INTERVAL_SEC, and a failed attempt waits that long too, so a
    Site24x7 outage does not slow every processing run. Never raises."""
    global _last_key_check
    now = time.monotonic()
    if _last_key_check and now - _last_key_check < KEY_CHECK_INTERVAL_SEC:
        return False
    _last_key_check = now
    try:
        if _registered_key_hash is None:
            return register_with_site24x7(timeout=timeout)
        resource_id = _site_resource_id()
        key = get_function_key(resource_id) if resource_id else None
        if key and _key_hash(key) != _registered_key_hash:
            logger.info("Registration: function key changed, updating Site24x7")
            return register_with_site24x7(timeout=timeout)
    except Exception as e:  # never surface the key: record the type only
        _record("warning", f"Registration check skipped after {type(e).__name__}",
                {"stage": "sync_registration", "error_type": type(e).__name__})
    return False


def _record(level: str, message: str, details: Dict) -> None:
    """Function log + the customer-downloadable debug log (both secret-free)."""
    logger.warning("Registration: %s", message)
    try:
        from shared.debug_logger import log_event
        log_event(level, "Registration", message, details)
    except Exception:
        pass
