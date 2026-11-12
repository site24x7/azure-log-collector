"""Log Collection switch.

PUT /api/collection  body: {"enabled": true|false}

Off: forwarding stops at once; a queued job removes this collector's diagnostic
settings from every resource and clears its stored logs (shared/collection.py).
On: forwarding resumes and a scan sets everything up again. Answers 202 right
away; /api/status (collection) shows the progress.
"""

import json
import logging

import azure.functions as func


def main(req: func.HttpRequest, scanQueue: func.Out[str]) -> func.HttpResponse:
    from shared import collection

    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "Invalid JSON body"}, 400)
    # A JSON boolean only: "false" (a non-empty string) would read as on.
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return _json({"error": "'enabled' must be true or false"}, 400)

    caller_ip = req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", ""))
    try:
        message = collection.request_change(enabled)
        if message is None:
            return _json({"error": "Failed to save the log collection state"}, 500)
        scanQueue.set(json.dumps(message))
    except Exception as e:
        logging.error("LogCollection: %s", type(e).__name__)
        return _json({"error": "Failed to change log collection"}, 500)

    try:
        from shared.debug_logger import log_audit
        log_audit("collection_" + ("on" if enabled else "off"), "LogCollection", {"enabled": enabled}, caller_ip)
    except Exception:
        pass
    return _json({"enabled": enabled, "queued": True}, 202)


def _json(body, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), mimetype="application/json", status_code=status_code)
