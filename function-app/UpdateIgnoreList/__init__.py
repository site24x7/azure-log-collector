import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.ignore_list import load_ignore_list, save_ignore_list, is_ignored
    from shared.config_store import (
        get_configured_resources,
        save_configured_resources,
        unmark_resource_configured,
    )

    logging.info("UpdateIgnoreList: Updating ignore list")
    caller_ip = req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", ""))

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            mimetype="application/json",
            status_code=400,
        )

    # Validate ignore list structure
    err = _validate_ignore_list(body)
    if err:
        return func.HttpResponse(
            json.dumps({"error": err}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        save_ignore_list(body)
        updated = load_ignore_list()

        # Clean up diagnostic settings for newly ignored resources
        diag_removed = 0
        try:
            from shared.azure_manager import AzureManager
            azure_mgr = AzureManager()
            configured = get_configured_resources()
            resources_to_remove = []

            for resource_id in list(configured.keys()):
                resource_info = {
                    "id": resource_id,
                    "location": "",
                    "resource_group": "",
                }
                if is_ignored(resource_info, updated):
                    try:
                        azure_mgr.delete_diagnostic_setting(resource_id)
                        resources_to_remove.append(resource_id)
                        diag_removed += 1
                    except Exception as e:
                        logging.warning(
                            "Failed to remove diag setting for %s: %s",
                            resource_id, str(e),
                        )

            for rid in resources_to_remove:
                configured.pop(rid, None)
            if resources_to_remove:
                save_configured_resources(configured)
        except Exception as e:
            logging.warning("Error cleaning up diag settings for ignored resources: %s", str(e))

        try:
            from shared.debug_logger import log_audit
            log_audit("update_ignore_list", "UpdateIgnoreList",
                      {"diag_removed": diag_removed}, caller_ip)
        except Exception:
            pass

        return func.HttpResponse(
            json.dumps({
                "ignore_list": updated,
                "diag_settings_removed": diag_removed,
            }, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("UpdateIgnoreList: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to update ignore list"}),
            mimetype="application/json",
            status_code=500,
        )


# Maximum items per list to prevent denial-of-service via oversized payloads
_MAX_LIST_ITEMS = 1000
_MAX_STRING_LEN = 500

_ALLOWED_KEYS = {
    "resource_groups", "locations", "resource_ids",
    "subscriptions", "resource_types", "tags",
}


def _validate_ignore_list(body) -> str:
    """Validate ignore list structure. Returns error string or empty on success."""
    if not isinstance(body, dict):
        return "Body must be a JSON object"

    unknown = set(body.keys()) - _ALLOWED_KEYS
    if unknown:
        return f"Unknown keys: {', '.join(sorted(unknown))}"

    for key in ("resource_groups", "locations", "resource_ids", "subscriptions", "resource_types"):
        val = body.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            return f"'{key}' must be an array"
        if len(val) > _MAX_LIST_ITEMS:
            return f"'{key}' exceeds maximum of {_MAX_LIST_ITEMS} items"
        for item in val:
            if not isinstance(item, str):
                return f"'{key}' items must be strings"
            if len(item) > _MAX_STRING_LEN:
                return f"'{key}' item exceeds maximum length of {_MAX_STRING_LEN}"

    tags = body.get("tags")
    if tags is not None:
        if isinstance(tags, list):
            if len(tags) > _MAX_LIST_ITEMS:
                return f"'tags' exceeds maximum of {_MAX_LIST_ITEMS} items"
            for t in tags:
                if not isinstance(t, str):
                    return "'tags' items must be strings"
                if len(t) > _MAX_STRING_LEN:
                    return f"'tags' item exceeds maximum length of {_MAX_STRING_LEN}"
        elif isinstance(tags, dict):
            for sub_key in ("include", "exclude"):
                sub = tags.get(sub_key)
                if sub is not None:
                    if not isinstance(sub, list):
                        return f"'tags.{sub_key}' must be an array"
                    if len(sub) > _MAX_LIST_ITEMS:
                        return f"'tags.{sub_key}' exceeds maximum of {_MAX_LIST_ITEMS} items"
                    for t in sub:
                        if not isinstance(t, str):
                            return f"'tags.{sub_key}' items must be strings"
                        if len(t) > _MAX_STRING_LEN:
                            return f"'tags.{sub_key}' item exceeds maximum length of {_MAX_STRING_LEN}"
        else:
            return "'tags' must be an array or object with include/exclude"

    return ""
