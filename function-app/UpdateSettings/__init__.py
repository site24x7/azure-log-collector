import os
import json
import logging

import azure.functions as func


# Whitelisted yes/no settings that can be updated from the dashboard, with
# the value the collector uses when the setting is unset.
ALLOWED_SETTINGS = {
    "MONITOR_PIPELINE_RESOURCES": False,
    "AUTO_SCAN_ENABLED": True,
}


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Update a whitelisted app setting.

    PUT /api/settings  body: {"key": "AUTO_SCAN_ENABLED", "value": false}
    GET  /api/settings  → returns current values of all whitelisted settings
    """
    from shared.azure_manager import AzureManager

    if req.method == "GET":
        current = {}
        for key, default in ALLOWED_SETTINGS.items():
            # Read each setting exactly as the collector does: the default
            # only when it is unset; an empty setting counts as off.
            raw = os.environ.get(key)
            current[key] = raw.lower() == "true" if raw is not None else default
        return func.HttpResponse(
            json.dumps(current, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    # PUT
    logging.info("UpdateSettings: Updating setting")
    caller_ip = req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", ""))

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
    key = body.get("key", "")
    value = body.get("value")

    if not isinstance(key, str) or key not in ALLOWED_SETTINGS:
        return func.HttpResponse(
            json.dumps({"error": f"Setting '{key}' is not configurable from the dashboard"}),
            mimetype="application/json",
            status_code=400,
        )

    if value is None:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'value' field"}),
            mimetype="application/json",
            status_code=400,
        )

    # Exact JSON type: no coercion of strings, numbers or containers.
    if not isinstance(value, bool):
        return func.HttpResponse(
            json.dumps({"error": f"'{key}' must be true or false"}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        azure_mgr = AzureManager()
        str_value = str(value).lower()
        if not azure_mgr.update_app_setting(key, str_value):
            return func.HttpResponse(
                json.dumps({"error": f"Failed to update setting '{key}'"}),
                mimetype="application/json",
                status_code=500,
            )
        os.environ[key] = str_value

        logging.info("UpdateSettings: Set %s = %s", key, str_value)
        try:
            from shared.debug_logger import log_audit
            log_audit("update_setting", "UpdateSettings",
                      {"key": key, "value": str_value}, caller_ip)
        except Exception:
            pass

        return func.HttpResponse(
            json.dumps({
                "key": key,
                "value": value,
                "message": f"Setting '{key}' updated to {value}",
            }),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("UpdateSettings: Error updating %s: %s", key, str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to update setting"}),
            mimetype="application/json",
            status_code=500,
        )
