import json
import logging
from datetime import datetime, timezone

import azure.functions as func


def main(req: func.HttpRequest, scanQueue: func.Out[str]) -> func.HttpResponse:
    """Start collecting after review.

    POST /api/start-collecting

    A new deployment's scans only discover and report until an admin has
    reviewed the exclusions and log categories and starts collecting here.
    Records the approval (a blob, so the app does not restart) and queues a
    scan that sets up storage, log types and diagnostic settings.
    """
    from shared.config_store import approve_collection, is_collection_active

    caller_ip = req.headers.get("X-Forwarded-For", req.headers.get("REMOTE_ADDR", ""))
    if is_collection_active():
        return _json({"collecting": True, "scan_queued": False}, 200)

    if not approve_collection(source="start-collecting"):
        logging.error("StartCollecting: could not save the approval")
        return _json({"error": "Failed to start collecting"}, 500)

    scanQueue.set(json.dumps({
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "source": "start-collecting",
    }))
    logging.info("StartCollecting: collection approved, scan queued")
    try:
        from shared.debug_logger import log_audit
        log_audit("start_collecting", "StartCollecting", {}, caller_ip)
    except Exception:
        pass
    try:
        from shared.registration import register_with_site24x7
        register_with_site24x7(timeout=10)  # Site24x7 shows "collecting" at once
    except Exception:
        pass
    return _json({"collecting": True, "scan_queued": True}, 202)


def _json(body, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), mimetype="application/json", status_code=status_code)
