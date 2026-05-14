import os
import json
import logging

import azure.functions as func


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

    enabled = body.get("enabled")
    if enabled is None:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'enabled' field (true/false)"}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        azure_mgr = AzureManager()
        new_value = "true" if enabled else "false"
        azure_mgr.update_app_setting("PROCESSING_ENABLED", new_value)

        # Update in-process env so subsequent reads reflect the change
        os.environ["PROCESSING_ENABLED"] = new_value

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
