import os
import json
import logging

import azure.functions as func


# Longest wait for the report to Site24x7 before answering the caller.
REPORT_TIMEOUT_SEC = 10


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.azure_manager import AzureManager

    """Toggle log processing on/off.

    PUT /api/processing  body: {"enabled": true/false}

    When disabled, the BlobLogProcessor skips processing
    without forwarding logs to Site24x7.
    """
    logging.info("StopProcessing: Updating processing state")
    caller_ip = req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", ""))

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            mimetype="application/json",
            status_code=400,
        )

    # A JSON boolean only: "false" (a non-empty string) would turn processing on.
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return func.HttpResponse(
            json.dumps({"error": "'enabled' must be true or false"}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        azure_mgr = AzureManager()
        new_value = "true" if enabled else "false"
        if not azure_mgr.update_app_setting("PROCESSING_ENABLED", new_value):
            # The setting was not saved: say so, or Site24x7 would report success.
            logging.error("StopProcessing: could not save PROCESSING_ENABLED=%s", new_value)
            return func.HttpResponse(
                json.dumps({"error": "Failed to update processing state"}),
                mimetype="application/json",
                status_code=500,
            )

        # Update in-process env so subsequent reads reflect the change
        os.environ["PROCESSING_ENABLED"] = new_value
        # Tell Site24x7 now, so its page shows the new state before the next scan.
        # Bounded: Site24x7 waits 30 s for this answer, and the setting is already
        # saved; if the report is slow, the next scan or timer run reports it.
        from shared.registration import register_with_site24x7
        register_with_site24x7(timeout=REPORT_TIMEOUT_SEC)

        action = "enabled" if enabled else "stopped"
        logging.info("StopProcessing: Processing %s", action)
        try:
            from shared.debug_logger import log_audit
            log_audit(f"processing_{action}", "StopProcessing",
                      {"enabled": enabled}, caller_ip)
        except Exception:
            pass

        return func.HttpResponse(
            json.dumps({
                "enabled": enabled,
                "message": f"Log processing has been {action}",
            }),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("StopProcessing: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to update processing state"}),
            mimetype="application/json",
            status_code=500,
        )
