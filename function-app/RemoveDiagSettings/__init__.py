import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.azure_manager import AzureManager

    """Remove s247-diag-logs diagnostic settings from all resources.

    POST /api/remove-diagnostic-settings

    Iterates through all resources in configured subscriptions and
    deletes the s247-diag-logs diagnostic setting where it exists.
    """
    logging.info("RemoveDiagSettings: Starting bulk removal of diagnostic settings")
    caller_ip = req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", ""))

    try:
        subscription_ids = [
            s.strip()
            for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]

        if not subscription_ids:
            return func.HttpResponse(
                json.dumps({"error": "No SUBSCRIPTION_IDS configured"}),
                mimetype="application/json",
                status_code=400,
            )

        from shared.config_store import get_configured_resources, get_scan_state, unmark_resource_configured
        # A running scan would re-create settings while they are being removed.
        if (get_scan_state() or {}).get("in_progress"):
            return func.HttpResponse(
                json.dumps({"error": "A resource scan is running. Try again when it finishes."}),
                mimetype="application/json",
                status_code=409,
            )

        azure_mgr = AzureManager()
        # Turn automatic scans off first, so the next timer cannot re-create what is removed.
        if not azure_mgr.update_app_setting("AUTO_SCAN_ENABLED", "false"):
            return func.HttpResponse(
                json.dumps({"error": "Could not turn off automatic scans; nothing was removed"}),
                mimetype="application/json",
                status_code=500,
            )
        os.environ["AUTO_SCAN_ENABLED"] = "false"

        result = azure_mgr.remove_all_diagnostic_settings(subscription_ids, get_configured_resources())
        result["auto_scan_disabled"] = True

        logging.info(
            "RemoveDiagSettings: Complete — removed=%d, skipped=%d, errors=%d",
            result["removed"],
            result["skipped"],
            result["errors"],
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

        return func.HttpResponse(
            json.dumps(result, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("RemoveDiagSettings: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to remove diagnostic settings"}),
            mimetype="application/json",
            status_code=500,
        )
