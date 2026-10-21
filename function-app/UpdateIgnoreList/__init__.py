import json
import logging
from datetime import datetime, timezone

import azure.functions as func

# Rules that can be checked from a resource ID alone; the rest (locations,
# resource types, tags) need the resource's details, which only a scan reads.
_ID_RULES = ("resource_ids", "subscriptions", "resource_groups")
_DETAIL_RULES = ("locations", "resource_types", "tags")


def main(req: func.HttpRequest, scanQueue: func.Out[str]) -> func.HttpResponse:
    from shared.ignore_list import load_ignore_list, save_ignore_list, is_ignored
    from shared.config_store import get_configured_resources, unmark_resource_configured

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
        previous = load_ignore_list()
        if not save_ignore_list(body):
            return func.HttpResponse(
                json.dumps({"error": "Failed to save the ignore list"}),
                mimetype="application/json",
                status_code=500,
            )
        updated = load_ignore_list()

        # Remove settings now only where the ID decides it. Applying every rule
        # here with just the ID made any include-tag rule look unmatched and
        # removed settings from resources that carry the tag.
        id_rules = {key: updated.get(key) or [] for key in _ID_RULES}
        diag_removed = 0
        removal_failed = 0
        try:
            from shared.azure_manager import AzureManager
            azure_mgr = AzureManager()
            configured = get_configured_resources()
            for resource_id in list(configured.keys()):
                if not is_ignored({"id": resource_id}, id_rules):
                    continue
                try:
                    removed = azure_mgr.delete_own_settings(resource_id, configured.get(resource_id))
                except Exception as e:
                    logging.warning("Failed to remove diag setting for %s: %s", resource_id, str(e))
                    removed = False
                # Keep the record of a setting that is still there, so it is not forgotten.
                if removed and unmark_resource_configured(resource_id):
                    diag_removed += 1
                else:
                    removal_failed += 1
        except Exception as e:
            logging.warning("Error cleaning up diag settings for ignored resources: %s", str(e))
            removal_failed += 1

        # Location / type / tag changes are applied by a scan, which has the details.
        scan_queued = False
        if any(_normalized(previous.get(key)) != _normalized(updated.get(key)) for key in _DETAIL_RULES):
            scanQueue.set(json.dumps({
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "source": "ignore-list",
            }))
            scan_queued = True

        try:
            from shared.debug_logger import log_audit
            log_audit("update_ignore_list", "UpdateIgnoreList",
                      {"diag_removed": diag_removed, "removal_failed": removal_failed,
                       "scan_queued": scan_queued}, caller_ip)
        except Exception:
            pass

        return func.HttpResponse(
            json.dumps({
                "ignore_list": updated,
                "diag_settings_removed": diag_removed,
                "diag_settings_not_removed": removal_failed,
                "scan_queued": scan_queued,
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


def _normalized(value):
    """Compare rule lists regardless of order and case. load_ignore_list has
    already turned legacy flat tags into include/exclude."""
    if isinstance(value, dict):
        return {k: sorted(str(v).lower() for v in (value.get(k) or [])) for k in ("include", "exclude")}
    if isinstance(value, list):
        return sorted(str(v).lower() for v in value)
    return []


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
        if key not in body:
            continue
        val = body[key]
        if not isinstance(val, list):
            return f"'{key}' must be an array"
        if len(val) > _MAX_LIST_ITEMS:
            return f"'{key}' exceeds maximum of {_MAX_LIST_ITEMS} items"
        for item in val:
            if not isinstance(item, str):
                return f"'{key}' items must be strings"
            if len(item) > _MAX_STRING_LEN:
                return f"'{key}' item exceeds maximum length of {_MAX_STRING_LEN}"

    if "tags" in body:
        tags = body["tags"]
        if isinstance(tags, list):
            if len(tags) > _MAX_LIST_ITEMS:
                return f"'tags' exceeds maximum of {_MAX_LIST_ITEMS} items"
            for t in tags:
                if not isinstance(t, str):
                    return "'tags' items must be strings"
                if len(t) > _MAX_STRING_LEN:
                    return f"'tags' item exceeds maximum length of {_MAX_STRING_LEN}"
        elif isinstance(tags, dict):
            unknown_tags = set(tags.keys()) - {"include", "exclude"}
            if unknown_tags:
                return f"Unknown tags keys: {', '.join(sorted(unknown_tags))}"
            for sub_key in ("include", "exclude"):
                if sub_key in tags:
                    sub = tags[sub_key]
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
