import json
import logging
from datetime import datetime, timezone

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Provision (or un-provision) a single tenant-scoped Entra ID log type on
    the Site24x7 side.

    POST /api/entra-logtypes
    Body: { "action": "enable"|"disable", "category": "<normalized>" }

    On enable: creates the log type in Site24x7 and stores its sourceConfig, so
    BlobLogProcessor can forward those logs the moment the tenant admin points an
    Entra diagnostic setting at our storage account. The per-category result
    (created / failed + message) is persisted and surfaced on the dashboard.

    On disable: removes our stored config for that category (we don't touch the
    Site24x7 log type itself). Note: this does NOT stop Azure writing the logs —
    the tenant admin controls that in the Entra diagnostic setting.

    This endpoint only reflects OUR side. It cannot and does not verify whether
    the Entra diagnostic setting is actually enabled in Azure.
    """
    from shared.config_store import (
        get_supported_log_types,
        get_logtype_config,
        save_logtype_config,
        delete_logtype_config,
        set_entra_logtype_state,
        get_entra_logtype_states,
    )
    from shared.entra_config import get_entra_normalized_categories
    from shared.site24x7_client import Site24x7Client

    try:
        body = req.get_json()
    except ValueError:
        return _err("Invalid JSON body", 400)
    if not isinstance(body, dict):
        return _err("Body must be a JSON object", 400)

    action = str(body.get("action", "")).lower()
    category = body.get("category", "")

    if action not in ("enable", "disable"):
        return _err("action must be 'enable' or 'disable'", 400)
    if not isinstance(category, str) or not category:
        return _err("'category' (normalized) is required", 400)

    normalized = category.replace("-", "").replace("_", "").replace(" ", "").lower()
    if normalized not in get_entra_normalized_categories():
        return _err(f"'{category}' is not a known Entra log category", 400)

    now = datetime.now(timezone.utc).isoformat()

    previous_state = {}
    previous_config = None
    config_category = normalized
    config_changed = False
    snapshot_loaded = False
    failure_message = None
    try:
        previous_state = get_entra_logtype_states(strict=True).get(normalized, {})
        previous_config = get_logtype_config(normalized, force_refresh=True, strict=True)
        snapshot_loaded = True
        if action == "disable":
            if not delete_logtype_config(normalized):
                raise RuntimeError("Failed to delete Entra log type configuration")
            config_changed = True
            state = {"enabled": False, "status": "disabled", "message": "", "updated": now}
        else:
            client = Site24x7Client()
            supported = get_supported_log_types(force_refresh=True)
            created = client.create_log_types([normalized], supported_types=supported)
            batch_errors = []
            if created and isinstance(created[0], dict):
                batch_errors = created[0].get("_errors", [])

            saved_config = None
            for lt in (created or []):
                if not isinstance(lt, dict) or not lt.get("sourceConfig"):
                    continue
                config_category = lt.get("category", "").replace("S247_", "") or normalized
                previous_config = get_logtype_config(config_category, force_refresh=True, strict=True)
                if not save_logtype_config(config_category, lt["sourceConfig"]):
                    raise RuntimeError("Failed to save Entra log type configuration")
                config_changed = True
                saved_config = lt["sourceConfig"]
                break

            if saved_config:
                state = {"enabled": True, "status": "created", "message": "", "updated": now}
            else:
                msg = "Site24x7 did not return a config for this log type."
                for error in batch_errors:
                    if error.get("message"):
                        msg = error["message"]
                        break
                # A failed retry must not disable a previously working category.
                if previous_state.get("enabled"):
                    failure_message = msg
                    # Preserve the working state/config while retaining the server's
                    # explanation of the failed retry for the caller and dashboard.
                    if set_entra_logtype_state(normalized, {"message": msg, "updated": now}) is None:
                        logging.error("UpdateEntraLogTypes: Failed to persist retry error for %s", normalized)
                    raise RuntimeError("Failed to refresh existing Entra log type")
                state = {"enabled": False, "status": "failed", "message": msg, "updated": now}

        states = set_entra_logtype_state(normalized, state)
        if states is None:
            raise RuntimeError("Failed to persist Entra log type state")
        return _ok({"category": normalized, **state, "states": states})

    except Exception:
        logging.exception("UpdateEntraLogTypes: Failed to %s %s", action, normalized)
        if config_changed:
            # Config and UI state live in separate blobs. Restore the original
            # config if committing the state fails, so a failed toggle is retryable.
            try:
                restored = (save_logtype_config(config_category, previous_config)
                            if previous_config is not None
                            else delete_logtype_config(config_category))
                if not restored:
                    logging.error("UpdateEntraLogTypes: Config rollback failed for %s", normalized)
            except Exception:
                logging.exception("UpdateEntraLogTypes: Config rollback failed for %s", normalized)
        if snapshot_loaded and action == "enable" and not previous_state.get("enabled"):
            try:
                failed = set_entra_logtype_state(normalized, {
                    "enabled": False, "status": "failed",
                    "message": "Could not provision this log type. Check collector logs and retry.",
                    "updated": now,
                })
                if failed is None:
                    logging.error("UpdateEntraLogTypes: Failed to persist failure state for %s", normalized)
            except Exception:
                logging.exception("UpdateEntraLogTypes: Failed to persist failure state for %s", normalized)
        return _err(failure_message or "Failed to update Entra log type. Retry the operation.", 500)


def _ok(payload):
    return func.HttpResponse(json.dumps(payload, indent=2),
                             mimetype="application/json", status_code=200)


def _err(message, status):
    return func.HttpResponse(json.dumps({"error": message}),
                             mimetype="application/json", status_code=status)
