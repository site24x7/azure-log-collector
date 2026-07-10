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

    try:
        if action == "disable":
            delete_logtype_config(normalized)
            set_entra_logtype_state(normalized, {
                "enabled": False, "status": "disabled", "message": "", "updated": now,
            })
            return _ok({"category": normalized, "enabled": False, "status": "disabled",
                        "states": get_entra_logtype_states()})

        # enable → create the log type in Site24x7
        client = Site24x7Client()
        supported = get_supported_log_types()
        created = client.create_log_types([normalized], supported_types=supported)

        # Pull structured errors (first element may carry an _errors list)
        batch_errors = []
        if created:
            batch_errors = created[0].pop("_errors", []) if isinstance(created[0], dict) else []

        saved_config = None
        for lt in (created or []):
            if lt.get("sourceConfig"):
                saved_config = lt["sourceConfig"]
                cat_name = lt.get("category", "").replace("S247_", "") or normalized
                save_logtype_config(cat_name, saved_config)
                break

        if saved_config:
            state = {"enabled": True, "status": "created", "message": "", "updated": now}
        else:
            msg = "Site24x7 did not return a config for this log type."
            for e in batch_errors:
                if e.get("message"):
                    msg = e["message"]
                    break
            # Common, expected case: the sign-in family isn't defined server-side yet.
            msg += " If this log type hasn't been created in Site24x7 yet, it will succeed once it is."
            state = {"enabled": True, "status": "failed", "message": msg, "updated": now}

        set_entra_logtype_state(normalized, state)
        return _ok({"category": normalized, **state, "states": get_entra_logtype_states()})

    except Exception as e:
        logging.error("UpdateEntraLogTypes: %s", e)
        set_entra_logtype_state(normalized, {
            "enabled": True, "status": "failed", "message": str(e)[:300], "updated": now,
        })
        return _err("Failed to update Entra log type", 500)


def _ok(payload):
    return func.HttpResponse(json.dumps(payload, indent=2),
                             mimetype="application/json", status_code=200)


def _err(message, status):
    return func.HttpResponse(json.dumps({"error": message}),
                             mimetype="application/json", status_code=status)
