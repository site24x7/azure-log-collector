import os
import json
import logging

import azure.functions as func


# Whitelisted settings that can be updated from the dashboard.
# Maps setting key → { "type": "int"|"str"|"bool", "min": N, "max": N }
ALLOWED_SETTINGS = {
    "SAFE_DELETE_MAX_AGE_DAYS": {"type": "int", "min": 1, "max": 365},
    "MONITOR_PIPELINE_RESOURCES": {"type": "bool"},
    "AUTO_SCAN_ENABLED": {"type": "bool"},
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
            raw = os.environ.get(key, "")
            if meta["type"] == "int":
                try:
                    current[key] = int(raw) if raw else meta.get("min", 0)
                except (ValueError, TypeError):
                    current[key] = meta.get("min", 0)
            elif meta["type"] == "bool":
                current[key] = raw.lower() == "true" if raw else False
            else:
                current[key] = raw
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

    key = body.get("key", "")
    value = body.get("value")

    if key not in ALLOWED_SETTINGS:
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
    if meta["type"] == "int":
        try:
            value = int(value)
        except (ValueError, TypeError):
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
        if isinstance(value, str):
            value = value.lower() in ("true", "1", "yes")
        else:
            value = bool(value)

    try:
        azure_mgr = AzureManager()
        str_value = str(value).lower() if meta["type"] == "bool" else str(value)
        azure_mgr.update_app_setting(key, str_value)
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
