import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.config_store import save_processing_enabled

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
        if not save_processing_enabled(enabled):
            logging.error("StopProcessing: could not save processing state")
            return func.HttpResponse(
                json.dumps({"error": "Failed to update processing state"}),
                mimetype="application/json",
                status_code=500,
            )
        # No ARM update/restart, key reads or callback registration in this
        # request. Status reads and the next timer invocation read this blob.

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
