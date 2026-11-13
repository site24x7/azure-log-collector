"""Delete Collector.

POST /api/delete-collector  body: {"confirm": "<this Function App's name>"}

Queues the job in shared/teardown.py: every diagnostic setting is removed, the
storage emptied, and then this collector's own resource group is deleted, the
Function App with it. Answers 202 right away; /api/status (collection.delete)
shows the progress until the app is gone.
"""

import json
import logging
import os

import azure.functions as func


def main(req: func.HttpRequest, scanQueue: func.Out[str]) -> func.HttpResponse:
    from shared import teardown

    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "Invalid JSON body"}, 400)
    name = os.environ.get("WEBSITE_SITE_NAME", "").strip().lower()
    confirm = body.get("confirm") if isinstance(body, dict) else None
    # The admin types the Function App's name: a stray call never deletes anything.
    if not name or not isinstance(confirm, str) or confirm.strip().lower() != name:
        return _json({"error": "'confirm' must be this Function App's name"}, 400)
    try:
        message = teardown.request_delete()
        if message is None:
            return _json({"error": "Failed to save the delete request"}, 500)
        scanQueue.set(json.dumps(message))
    except Exception as e:
        logging.error("DeleteCollector: %s", type(e).__name__)
        return _json({"error": "Failed to start the deletion"}, 500)
    try:
        from shared.debug_logger import log_audit
        log_audit("delete_collector_requested", "DeleteCollector", {},
                  req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", "")))
    except Exception:
        pass
    return _json({"queued": True}, 202)


def _json(body, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), mimetype="application/json", status_code=status_code)
