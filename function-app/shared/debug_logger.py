"""Persistent debug log — ring buffer stored in blob storage.

Captures errors, warnings, and key operational events with timestamps.
Used by the Debug API and Dashboard to show recent issues without
requiring Azure Portal / Log Analytics access.

Events are stored as a JSON array in blob storage, capped at MAX_EVENTS.
Processing stats (BlobLogProcessor runs) are stored separately.
"""

import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

logger = logging.getLogger(__name__)

MAX_EVENTS = 200
EVENTS_BLOB = "debug-events.json"
PROCESSING_STATS_BLOB = "processing-stats.json"
MAX_PROCESSING_RUNS = 50
CONTAINER_NAME = "config"
_RMW_MAX_ATTEMPTS = 5


def _get_blob_client(blob_name: str):
    """Get a blob client for the given blob name."""
    from azure.storage.blob import BlobServiceClient

    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if not conn_str:
        return None
    service = BlobServiceClient.from_connection_string(conn_str)
    container = service.get_container_client(CONTAINER_NAME)
    try:
        container.create_container()
    except Exception:
        pass
    return container.get_blob_client(blob_name)


def _read_with_etag(client) -> Tuple[List[Dict], Optional[str]]:
    """Return (list, etag). ([], None) when missing."""
    try:
        downloader = client.download_blob()
        data = downloader.readall().decode("utf-8")
        etag = downloader.properties.etag if downloader.properties else None
        return json.loads(data), etag
    except ResourceNotFoundError:
        return [], None
    except Exception:
        return [], None


def _append_json_array_rmw(
    blob_name: str,
    new_item: Dict,
    cap: int,
) -> bool:
    """Append ``new_item`` to a JSON-array blob using ETag-based RMW.

    Retries on 412 (concurrent writer won the race) up to ``_RMW_MAX_ATTEMPTS``
    times. Trims the array to the last ``cap`` entries on every write.
    """
    client = _get_blob_client(blob_name)
    if not client:
        return False

    for attempt in range(_RMW_MAX_ATTEMPTS):
        items, etag = _read_with_etag(client)
        items.append(new_item)
        if len(items) > cap:
            items = items[-cap:]
        payload = json.dumps(items, indent=2)

        try:
            if etag:
                client.upload_blob(
                    payload,
                    overwrite=True,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            else:
                # Blob didn't exist when we read — create it, but fail if
                # another writer created it in the meantime.
                client.upload_blob(
                    payload,
                    overwrite=False,
                )
            return True
        except ResourceModifiedError:
            # Another writer got in before us — retry with fresh state
            time.sleep(0.05 * (2 ** attempt))
            continue
        except Exception as e:
            # Includes the race where overwrite=False fails because blob now exists
            if "BlobAlreadyExists" in str(e):
                time.sleep(0.05 * (2 ** attempt))
                continue
            logger.error("debug_logger: Failed to append to %s: %s", blob_name, e)
            return False

    logger.warning(
        "debug_logger: Gave up after %d attempts appending to %s (concurrent writers)",
        _RMW_MAX_ATTEMPTS, blob_name,
    )
    return False


def _read_events() -> List[Dict]:
    """Read existing events from blob."""
    try:
        client = _get_blob_client(EVENTS_BLOB)
        if not client:
            return []
        data = client.download_blob().readall().decode("utf-8")
        return json.loads(data)
    except Exception:
        return []


def _write_events(events: List[Dict]) -> None:
    """Write events to blob."""
    try:
        client = _get_blob_client(EVENTS_BLOB)
        if client:
            client.upload_blob(json.dumps(events, indent=2), overwrite=True)
    except Exception as e:
        logger.error("debug_logger: Failed to write events: %s", e)


def log_event(
    level: str,
    component: str,
    message: str,
    details: Optional[Dict] = None,
) -> None:
    """Log a debug event to persistent storage.

    Args:
        level: "error", "warning", "info"
        component: Module name (e.g., "BlobLogProcessor", "DiagSettingsManager")
        message: Short description of what happened
        details: Optional dict with additional context (stack trace, resource IDs, etc.)
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": component,
        "message": message,
    }
    if details:
        event["details"] = details

    try:
        _append_json_array_rmw(EVENTS_BLOB, event, MAX_EVENTS)
    except Exception as e:
        # Don't let debug logging break the main flow
        logger.error("debug_logger: Failed to log event: %s", e)


def get_recent_events(limit: int = 50, level: Optional[str] = None) -> List[Dict]:
    """Get recent debug events, newest first.

    Args:
        limit: Max number of events to return
        level: Optional filter by level ("error", "warning", "info")
    """
    events = _read_events()
    if level:
        events = [e for e in events if e.get("level") == level]
    return list(reversed(events[-limit:]))


def clear_events() -> None:
    """Clear all debug events."""
    _write_events([])


# ─── Processing Stats (BlobLogProcessor run history) ─────────────────────────


def save_processing_stats(stats: Dict) -> None:
    """Save a BlobLogProcessor run summary to the stats ring buffer."""
    stats["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        _append_json_array_rmw(PROCESSING_STATS_BLOB, stats, MAX_PROCESSING_RUNS)
    except Exception as e:
        logger.error("debug_logger: Failed to save processing stats: %s", e)


def get_processing_stats(limit: int = 10) -> List[Dict]:
    """Get recent BlobLogProcessor run summaries, newest first."""
    try:
        client = _get_blob_client(PROCESSING_STATS_BLOB)
        if not client:
            return []
        data = client.download_blob().readall().decode("utf-8")
        runs = json.loads(data)
        return list(reversed(runs[-limit:]))
    except Exception:
        return []


# ─── Config Validation ───────────────────────────────────────────────────────


def validate_config() -> List[Dict]:
    """Check for common configuration issues.

    Returns a list of {level, field, message} dicts.
    """
    issues = []

    # Required env vars
    required = {
        "SUBSCRIPTION_IDS": "Azure subscription IDs to monitor",
        "SITE24X7_API_KEY": "Site24x7 device key for API authentication",
        "SITE24X7_BASE_URL": "Site24x7 data center URL",
    }
    for var, desc in required.items():
        val = os.environ.get(var, "")
        if not val:
            issues.append({
                "level": "error",
                "field": var,
                "message": f"Not set — {desc}",
            })
        elif var == "SITE24X7_API_KEY":
            # Device keys follow patterns like ab_<hex>, aa_<hex>, in_<hex>
            import re
            _placeholders = {"vuvuvi", "test", "changeme", "placeholder",
                             "dummy", "xxx", "your-key-here", "TODO"}
            if val in _placeholders:
                issues.append({
                    "level": "error",
                    "field": var,
                    "message": "Placeholder value — set a real Site24x7 device key",
                })
            elif len(val) < 20 or not re.match(r"^[a-z]{2}_[a-f0-9]{20,}$", val):
                issues.append({
                    "level": "warning",
                    "field": var,
                    "message": "Value does not match expected device key format (e.g., ab_<hex>, in_<hex>)",
                })

    # Storage connection
    if not os.environ.get("AzureWebJobsStorage"):
        issues.append({
            "level": "error",
            "field": "AzureWebJobsStorage",
            "message": "Not set — blob storage for configs/state won't work",
        })

    # Processing state
    if os.environ.get("PROCESSING_ENABLED", "true").lower() != "true":
        issues.append({
            "level": "warning",
            "field": "PROCESSING_ENABLED",
            "message": "Log processing is DISABLED — logs are accumulating but not forwarded",
        })

    # General logtype
    general_enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"
    if general_enabled and not os.environ.get("S247_GENERAL_LOGTYPE"):
        issues.append({
            "level": "warning",
            "field": "S247_GENERAL_LOGTYPE",
            "message": "GENERAL_LOGTYPE_ENABLED=true but S247_GENERAL_LOGTYPE is empty — general fallback won't work",
        })

    # Update URL
    if not os.environ.get("UPDATE_CHECK_URL"):
        issues.append({
            "level": "info",
            "field": "UPDATE_CHECK_URL",
            "message": "Not set — set UPDATE_CHECK_URL app setting to enable auto-update checks",
        })

    return issues


# ─── S247 Connectivity Test ──────────────────────────────────────────────────


def test_s247_connectivity() -> Dict:
    """Test connectivity to Site24x7 API endpoints.

    Returns dict with results for each endpoint tested.
    """
    import urllib.request
    import urllib.parse
    import time

    base_url = os.environ.get("SITE24X7_BASE_URL", "https://www.site24x7.com")
    device_key = os.environ.get("SITE24X7_API_KEY", "")
    results = {}

    # Test 1: logtype_supported endpoint
    try:
        url = f"{base_url}/applog/azure/logtype_supported?{urllib.parse.urlencode({'deviceKey': device_key})}"
        start = time.time()
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        resp = urllib.request.urlopen(req, timeout=15)
        elapsed_ms = int((time.time() - start) * 1000)
        body = json.loads(resp.read().decode("utf-8"))
        results["logtype_supported"] = {
            "status": "ok",
            "http_status": resp.status,
            "response_time_ms": elapsed_ms,
            "type_count": len(body.get("supported_types", [])),
        }
    except Exception as e:
        results["logtype_supported"] = {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
        }

    # Test 2: logtype endpoint (check a known type)
    try:
        url = f"{base_url}/applog/logtype?{urllib.parse.urlencode({'deviceKey': device_key, 'logType': 'auditlogs'})}"
        start = time.time()
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        resp = urllib.request.urlopen(req, timeout=15)
        elapsed_ms = int((time.time() - start) * 1000)
        body = json.loads(resp.read().decode("utf-8"))
        results["logtype_check"] = {
            "status": "ok",
            "http_status": resp.status,
            "response_time_ms": elapsed_ms,
            "api_status": body.get("status"),
            "api_upload": body.get("apiUpload"),
        }
    except Exception as e:
        results["logtype_check"] = {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
        }

    # Test 3: Upload domain reachability (just TCP connect, no actual upload)
    from shared.site24x7_client import Site24x7Client
    client = Site24x7Client()
    upload_domain = client._get_upload_domain()
    try:
        import socket
        host = upload_domain.replace("http://", "").replace("https://", "").split("/")[0]
        port = 443
        if ":" in host:
            host, port = host.rsplit(":", 1)
            port = int(port)
        elif upload_domain.startswith("http://"):
            port = 80

        start = time.time()
        sock = socket.create_connection((host, port), timeout=10)
        elapsed_ms = int((time.time() - start) * 1000)
        sock.close()
        results["upload_endpoint"] = {
            "status": "ok",
            "domain": upload_domain,
            "connect_time_ms": elapsed_ms,
        }
    except Exception as e:
        results["upload_endpoint"] = {
            "status": "error",
            "domain": upload_domain,
            "error": str(e),
            "error_type": type(e).__name__,
        }

    # Flatten into the format the dashboard expects
    base_url = os.environ.get("SITE24X7_BASE_URL", "https://www.site24x7.com")
    lt_ok = results.get("logtype_supported", {}).get("status") == "ok"
    # Fall back to logtype_check if logtype_supported fails (relay may not support it)
    if not lt_ok:
        lt_ok = results.get("logtype_check", {}).get("status") == "ok"
    up_ok = results.get("upload_endpoint", {}).get("status") == "ok"
    upload_domain = results.get("upload_endpoint", {}).get("domain", "?")

    results["base_url"] = base_url
    results["upload_domain"] = upload_domain
    results["logtype_supported_ok"] = lt_ok
    results["upload_domain_ok"] = up_ok
    results["logtype_ok"] = results.get("logtype_check", {}).get("status") == "ok"

    # Collect errors for display (skip logtype_supported if logtype_check succeeded)
    errors = []
    lt_check_ok = results.get("logtype_check", {}).get("status") == "ok"
    for key in ("logtype_supported", "logtype_check", "upload_endpoint"):
        entry = results.get(key, {})
        if entry.get("status") == "error":
            if key == "logtype_supported" and lt_check_ok:
                continue
            errors.append(f"{key}: {entry.get('error', 'unknown')}")
    if errors:
        results["error"] = "; ".join(errors)

    return results


# ─── Audit Logging ──────────────────────────────────────────────────────────

AUDIT_BLOB = "config/audit-log.json"
MAX_AUDIT_EVENTS = 500


def log_audit(action: str, component: str, details: dict = None,
              caller_ip: str = None) -> None:
    """Log a destructive or sensitive operation to a persistent audit trail.

    Called from write endpoints (UpdateIgnoreList, RemoveDiagSettings,
    StopProcessing, UpdateSettings, UpdateDisabledLogTypes) to record
    WHO did WHAT and WHEN.
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "component": component,
    }
    if caller_ip:
        event["caller_ip"] = caller_ip
    if details:
        event["details"] = details

    try:
        blob_client = _get_blob_client(AUDIT_BLOB)
        if not blob_client:
            return
        from azure.core import MatchConditions
        # Use ETag-based optimistic concurrency to prevent lost writes
        # from concurrent function invocations.
        for _attempt in range(3):
            etag = None
            try:
                stream = blob_client.download_blob()
                etag = getattr(stream.properties, "etag", None)
                events = json.loads(stream.readall())
            except Exception:
                events = []
                etag = None

            events.append(event)
            if len(events) > MAX_AUDIT_EVENTS:
                events = events[-MAX_AUDIT_EVENTS:]

            try:
                if etag:
                    blob_client.upload_blob(
                        json.dumps(events), overwrite=True,
                        etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                else:
                    blob_client.upload_blob(
                        json.dumps(events), overwrite=True,
                        match_condition=MatchConditions.IfMissing,
                    )
                break  # success
            except Exception as conflict:
                msg = str(conflict)
                if ("ConditionNotMet" in msg or "BlobAlreadyExists" in msg
                        or "412" in msg or "409" in msg) and _attempt < 2:
                    continue  # retry on ETag mismatch
                raise
    except Exception as e:
        logger.warning("Failed to write audit log: %s", e)


def get_audit_log(limit: int = 50) -> list:
    """Retrieve recent audit events."""
    try:
        blob_client = _get_blob_client(AUDIT_BLOB)
        data = blob_client.download_blob().readall()
        events = json.loads(data)
        return events[-limit:]
    except Exception:
        return []
