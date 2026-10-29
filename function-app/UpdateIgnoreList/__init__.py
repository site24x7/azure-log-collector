import json
import logging
from datetime import datetime, timezone

import azure.functions as func

# Rules that can be checked from a resource ID alone; the rest (locations,
# resource types, tags) need the resource's details, which only a scan reads.
_ID_RULES = ("resource_ids", "subscriptions", "resource_groups")
_DETAIL_RULES = ("locations", "resource_types", "tags")


def main(req: func.HttpRequest, scanQueue: func.Out[str]) -> func.HttpResponse:
    from shared.ignore_list import IgnoreListUnavailable, load_ignore_list, update_ignore_list, is_ignored, validate_ignore_list
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
    err = validate_ignore_list(body)
    if err:
        return func.HttpResponse(
            json.dumps({"error": err}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        try:
            previous = load_ignore_list(strict=True)
        except IgnoreListUnavailable:
            previous = None  # unknown: treat every rule as changed, so a scan is queued
        # update_ignore_list fills missing lists and the tags structure in place;
        # what it saved is the new state (a re-read could fail and look empty).
        if not update_ignore_list(body):
            return func.HttpResponse(
                json.dumps({"error": "Failed to save the ignore list"}),
                mimetype="application/json",
                status_code=500,
            )
        updated = body

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
        # Also when a scan is running: it loaded the old list and could undo this change.
        from shared.config_store import get_scan_state
        scan_running = bool((get_scan_state() or {}).get("in_progress"))
        if scan_running or previous is None or any(
                _normalized(key, previous.get(key)) != _normalized(key, updated.get(key))
                for key in _DETAIL_RULES):
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


def _normalized(key, value):
    """Compare rule lists regardless of order, with the same case rules as the
    filter: everything matches case-insensitively, tag names and values too
    (Azure tag names are case-insensitive). Legacy flat tags are exclude rules."""
    if key == "tags":
        if isinstance(value, list):
            value = {"include": [], "exclude": value}
        value = value if isinstance(value, dict) else {}
        return {k: sorted(str(v).lower() for v in (value.get(k) or [])) for k in ("include", "exclude")}
    if isinstance(value, list):
        return sorted(str(v).lower() for v in value)
    return []
