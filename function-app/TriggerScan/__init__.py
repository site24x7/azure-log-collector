import json
import logging
from datetime import datetime, timezone

import azure.functions as func


def main(req: func.HttpRequest, scanQueue: func.Out[str]) -> func.HttpResponse:
    """Enqueue a scan request via output binding.  The ScanWorker queue trigger
    picks it up and runs the full resource scan — avoiding the HTTP gateway
    230-second timeout."""
    logging.info("TriggerScan: On-demand scan requested")

    try:
        msg = json.dumps({
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "source": "dashboard",
        })
        scanQueue.set(msg)

        logging.info("TriggerScan: Enqueued scan request")
        return func.HttpResponse(
            json.dumps({"status": "started",
                        "message": "Scan queued. Poll /api/status for results."}),
            mimetype="application/json",
            status_code=202,
        )

    except Exception as e:
        import traceback
        logging.error("TriggerScan: Error: %s\n%s", str(e), traceback.format_exc())
        return func.HttpResponse(
            json.dumps({"error": "Failed to enqueue scan request"}),
            mimetype="application/json",
            status_code=500,
        )
