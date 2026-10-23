import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Remove this collector's diagnostic settings from all resources.

    POST /api/remove-diagnostic-settings

    Holds the scan lock for the whole removal (a scan would re-create settings),
    turns automatic scans off first, and clears the tracking only of resources
    whose setting is confirmed gone.
    """
    from shared.config_store import try_acquire_scan_lock, update_scan_state

    logging.info("RemoveDiagSettings: Starting bulk removal of diagnostic settings")
    caller_ip = req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", ""))

    subscription_ids = [
        s.strip()
        for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
        if s.strip()
    ]
    if not subscription_ids:
        return _json({"error": "No SUBSCRIPTION_IDS configured"}, 400)

    try:
        if not try_acquire_scan_lock():
            return _json({"error": "A resource scan is running. Try again when it finishes."}, 409)
    except Exception as e:
        logging.error("RemoveDiagSettings: scan lock check failed: %s", type(e).__name__)
        return _json({"error": "Failed to remove diagnostic settings"}, 500)

    try:
        return _remove_all(subscription_ids, caller_ip)
    except Exception as e:
        logging.error("RemoveDiagSettings: Error: %s", str(e))
        return _json({"error": "Failed to remove diagnostic settings"}, 500)
    finally:
        update_scan_state({"in_progress": False})


def _remove_all(subscription_ids, caller_ip) -> func.HttpResponse:
    from shared.azure_manager import AzureManager
    from shared.config_store import get_configured_resources, unmark_resource_configured

    azure_mgr = AzureManager()
    # Turn automatic scans off first, so the next timer cannot re-create what is removed.
    if not azure_mgr.update_app_setting("AUTO_SCAN_ENABLED", "false"):
        return _json({"error": "Could not turn off automatic scans; nothing was removed"}, 500)
    os.environ["AUTO_SCAN_ENABLED"] = "false"

    result = azure_mgr.remove_all_diagnostic_settings(subscription_ids, get_configured_resources())
    result["auto_scan_disabled"] = True
    logging.info(
        "RemoveDiagSettings: Complete — removed=%d, skipped=%d, errors=%d, unresolved=%d",
        result["removed"], result["skipped"], result["errors"], result["unresolved"],
    )

    # Drop the tracking only of resources whose setting is gone (removed now or
    # confirmed absent). Failed and undiscovered ones keep their records, so
    # nothing that may still export is forgotten.
    resolved = result.pop("resolved_ids", [])
    not_cleared = sum(1 for rid in resolved if not unmark_resource_configured(rid))
    result["tracking_cleared"] = (not_cleared == 0 and result["errors"] == 0
                                  and result["unresolved"] == 0)

    try:
        from shared.debug_logger import log_audit
        log_audit("remove_all_diagnostic_settings", "RemoveDiagSettings",
                  {"removed": result["removed"], "skipped": result["skipped"],
                   "errors": result["errors"], "unresolved": result["unresolved"]}, caller_ip)
    except Exception:
        pass

    return _json(result, 200)


def _json(body, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, indent=2), mimetype="application/json",
                             status_code=status_code)
