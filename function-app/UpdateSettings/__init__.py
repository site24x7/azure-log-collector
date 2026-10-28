import os
import json
import logging

import azure.functions as func


# Whitelisted settings that can be updated from the dashboard.
# Maps setting key → { "type": "int"|"str"|"bool", "min": N, "max": N }
# "default" is the value the collector uses when the setting is unset.
ALLOWED_SETTINGS = {
    "SAFE_DELETE_MAX_AGE_DAYS": {"type": "int", "min": 1, "max": 365, "default": 7},
    "MONITOR_PIPELINE_RESOURCES": {"type": "bool", "default": False},
    "AUTO_SCAN_ENABLED": {"type": "bool", "default": True},
}


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Update a whitelisted app setting.

    PUT /api/settings  body: {"key": "SAFE_DELETE_MAX_AGE_DAYS", "value": 14}
    GET  /api/settings  → returns current values of all whitelisted settings
    """
    from shared.azure_manager import AzureManager

    if req.method == "GET":
        current = {}
        for key, meta in ALLOWED_SETTINGS.items():
            # Read each setting exactly as the collector does: the default
            # only when it is unset; an empty yes/no setting counts as off.
            raw = os.environ.get(key)
            if meta["type"] == "int":
                try:
                    current[key] = int(raw) if raw is not None else meta["default"]
                except (ValueError, TypeError):
                    current[key] = meta["default"]
            elif meta["type"] == "bool":
                current[key] = raw.lower() == "true" if raw is not None else meta["default"]
            else:
                current[key] = raw or ""
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

    meta = ALLOWED_SETTINGS[key]

    # Validate type and range
    # Exact JSON types: no coercion of strings, numbers or containers.
    if meta["type"] == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return func.HttpResponse(
                json.dumps({"error": f"'{key}' must be an integer"}),
                mimetype="application/json",
                status_code=400,
            )
        if "min" in meta and value < meta["min"]:
            return func.HttpResponse(
                json.dumps({"error": f"'{key}' must be >= {meta['min']}"}),
                mimetype="application/json",
                status_code=400,
            )
        if "max" in meta and value > meta["max"]:
            return func.HttpResponse(
                json.dumps({"error": f"'{key}' must be <= {meta['max']}"}),
                mimetype="application/json",
                status_code=400,
            )
    elif meta["type"] == "bool":
        if not isinstance(value, bool):
            return func.HttpResponse(
                json.dumps({"error": f"'{key}' must be true or false"}),
                mimetype="application/json",
                status_code=400,
            )

    try:
        azure_mgr = AzureManager()
        str_value = str(value).lower() if meta["type"] == "bool" else str(value)
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
