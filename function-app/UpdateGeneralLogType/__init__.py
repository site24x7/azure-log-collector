import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.azure_manager import AzureManager

    logging.info("UpdateGeneralLogType: Updating general log type setting")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            mimetype="application/json",
            status_code=400,
        )

    if not isinstance(body, dict):
        return func.HttpResponse(
            json.dumps({"error": "Body must be a JSON object"}),
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
        subscription_ids = [
            s.strip()
            for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]
        azure_mgr = AzureManager()

        new_value = "true" if enabled else "false"
        azure_mgr.update_app_setting("GENERAL_LOGTYPE_ENABLED", new_value)

        # Also update in-process env so subsequent reads reflect the change
        os.environ["GENERAL_LOGTYPE_ENABLED"] = new_value

        return func.HttpResponse(
            json.dumps({"enabled": enabled}),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("UpdateGeneralLogType: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to update general log type"}),
            mimetype="application/json",
            status_code=500,
        )
