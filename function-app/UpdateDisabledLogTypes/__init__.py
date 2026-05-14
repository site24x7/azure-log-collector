import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.config_store import (
        disable_log_type,
        enable_log_type,
        get_disabled_log_types,
        get_configured_resources,
        save_configured_resources,
        delete_logtype_config,
    )
    from shared.azure_manager import AzureManager

    """Toggle log type enabled/disabled status.

    POST /api/disabled-logtypes
    Body: { "action": "disable"|"enable", "category": "AuditEvent" }

    When disabling:
    - Adds category to disabled list
    - Removes diagnostic settings for resources that only have this category
    - Deletes stored sourceConfig for this category

    When enabling:
    - Removes category from disabled list
    - Next scan will re-create log types and diagnostic settings
    """
    logging.info("UpdateDisabledLogTypes: Processing request")

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

    action = str(body.get("action", "")).lower()
    category = body.get("category", "")
    categories = body.get("categories", [])

    if action not in ("disable", "enable"):
        return func.HttpResponse(
            json.dumps({"error": "action must be 'disable' or 'enable'"}),
            mimetype="application/json",
            status_code=400,
        )

    # Validate category/categories types and length
    _MAX_CAT_LEN = 200
    if category and not isinstance(category, str):
        return func.HttpResponse(
            json.dumps({"error": "'category' must be a string"}),
            mimetype="application/json",
            status_code=400,
        )
    if category and len(category) > _MAX_CAT_LEN:
        return func.HttpResponse(
            json.dumps({"error": f"'category' exceeds maximum length of {_MAX_CAT_LEN}"}),
            mimetype="application/json",
            status_code=400,
        )
    if not isinstance(categories, list) or any(not isinstance(c, str) for c in categories):
        return func.HttpResponse(
            json.dumps({"error": "'categories' must be an array of strings"}),
            mimetype="application/json",
            status_code=400,
        )
    if len(categories) > 200:
        return func.HttpResponse(
            json.dumps({"error": "'categories' exceeds maximum of 200 items"}),
            mimetype="application/json",
            status_code=400,
        )
    if any(len(c) > _MAX_CAT_LEN for c in categories):
        return func.HttpResponse(
            json.dumps({"error": f"'categories' item exceeds maximum length of {_MAX_CAT_LEN}"}),
            mimetype="application/json",
            status_code=400,
        )

    # Support both single category and bulk categories
    targets = categories if categories else ([category] if category else [])
    if not targets:
        return func.HttpResponse(
            json.dumps({"error": "category or categories is required"}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        result = {"action": action, "categories": targets, "count": len(targets)}

        if action == "disable":
            for cat in targets:
                disable_log_type(cat)
                delete_logtype_config(cat)

            # Remove diagnostic settings for resources using only disabled categories
            diag_removed = 0
            try:
                azure_mgr = AzureManager()
                configured = get_configured_resources()
                resources_to_update = []
                target_normalized = {
                    cat.replace("-", "_").replace(" ", "").lower()
                    for cat in targets
                }

                for resource_id, info in configured.items():
                    cats = info.get("categories", [])
                    matching = [
                        c for c in cats
                        if c.replace("-", "_").replace(" ", "").lower() in target_normalized
                    ]
                    if matching:
                        remaining = [c for c in cats if c not in matching]
                        if not remaining:
                            try:
                                azure_mgr.delete_diagnostic_setting(resource_id)
                                resources_to_update.append(resource_id)
                                diag_removed += 1
                            except Exception as e:
                                logging.warning(
                                    "Failed to remove diag setting for %s: %s",
                                    resource_id, str(e),
                                )
                        else:
                            info["categories"] = remaining

                for rid in resources_to_update:
                    configured.pop(rid, None)
                save_configured_resources(configured)
            except Exception as e:
                logging.error("Error cleaning up diagnostic settings: %s", str(e))

            result["diag_settings_removed"] = diag_removed

        else:  # enable
            for cat in targets:
                enable_log_type(cat)
            result["note"] = "Categories re-enabled. Run a scan to re-create log types and diagnostic settings."

        result["disabled_logtypes"] = get_disabled_log_types()

        return func.HttpResponse(
            json.dumps(result, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("UpdateDisabledLogTypes: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to update disabled log types"}),
            mimetype="application/json",
            status_code=500,
        )
