"""Register this collector with Site24x7 after every discovery scan.

Site24x7 keeps one row per collector (account + Function App URL) so the
Deploy Diagnostic Logs page can show every collector's read-only status to
all users, and let admins open this Function App's dashboard. The function
key is read fresh from ARM on each run, so a rotated key re-syncs on the next
scan. Keys travel only in the POST body and are never logged.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

_ARM = "https://management.azure.com"
_ARM_API_VERSION = "2022-03-01"
_PROCESSING_RUNS_FOR_STATUS = 12


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


def get_function_key(resource_id: str) -> Optional[str]:
    """The app's ``default`` function key, via its managed identity (RG Contributor)."""
    from azure.identity import DefaultAzureCredential

    token = DefaultAzureCredential().get_token(f"{_ARM}/.default")
    resp = requests.post(
        f"{_ARM}{resource_id}/host/default/listKeys?api-version={_ARM_API_VERSION}",
        headers={"Authorization": f"Bearer {token.token}", "Content-Length": "0"},
        timeout=30,
    )
    if not resp.ok:
        _record("warning", f"Could not read the function key (ARM listKeys HTTP {resp.status_code})",
                {"stage": "listKeys", "http_status": resp.status_code,
                 "hint": "the app's managed identity needs Contributor on its resource group"})
        return None
    return (resp.json().get("functionKeys") or {}).get("default") or None


def build_status_summary() -> Dict:
    """Read-only status shown to every Site24x7 user; contains no secrets."""
    from shared.config_store import get_scan_state, get_configured_resources
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
        "processing_enabled": os.environ.get("PROCESSING_ENABLED", "true").lower() == "true",
        "auto_scan_enabled": os.environ.get("AUTO_SCAN_ENABLED", "true").lower() == "true",
        "last_processing_time": runs[0].get("timestamp", "") if runs else "",
        "recent_uploaded": sum(int(r.get("uploaded", 0) or 0) for r in runs),
        "recent_dropped": sum(int(r.get("dropped", 0) or 0) for r in runs),
        # Records the latest run could not send; they are re-sent on the next run,
        # so this goes back to 0 as soon as uploads work again.
        "pending_records": int(runs[0].get("retrying", 0) or 0) if runs else 0,
        # The recent counts cover these runs; the oldest one's time says since when.
        "recent_since": runs[-1].get("timestamp", "") if runs else "",
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }
    subscriptions = []
    for raw in os.environ.get("SUBSCRIPTION_IDS", "").split(","):
        try:
            subscription = str(uuid.UUID(raw.strip()))
        except (ValueError, AttributeError):
            continue
        if subscription not in subscriptions:
            subscriptions.append(subscription)
    summary["subscription_ids"] = subscriptions[:50]
    summary["subscriptions_truncated"] = len(subscriptions) > 50
    _add_subscription_names(summary, _subscription_names(summary["subscription_ids"]))
    if "regions_count" in scan:
        summary["regions_count"] = scan["regions_count"]
    try:
        configured = get_configured_resources()
        if isinstance(configured, dict):
            summary["configured_resources"] = len(configured)
    except Exception:
        pass  # absence of this count must not block registration
    return summary


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


def register_with_site24x7() -> bool:
    """Best effort: a failure here must never fail or delay the scan."""
    try:
        fields = build_registration()
        if not fields:
            return False
        from shared.site24x7_client import Site24x7Client

        ok = Site24x7Client().register_collector(fields)
        if ok:
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


def _record(level: str, message: str, details: Dict) -> None:
    """Function log + the customer-downloadable debug log (both secret-free)."""
    logger.warning("Registration: %s", message)
    try:
        from shared.debug_logger import log_event
        log_event(level, "Registration", message, details)
    except Exception:
        pass
