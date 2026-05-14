import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("GetGeneralLogType: Fetching general log type status")

    enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"

    return func.HttpResponse(
        json.dumps({"enabled": enabled}),
        mimetype="application/json",
        status_code=200,
    )
