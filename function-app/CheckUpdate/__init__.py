"""CheckUpdate — HTTP endpoint to check for available updates.

GET  /api/check-update          → check only
POST /api/check-update?apply=1  → check and apply if available
"""

import json
import logging

import azure.functions as func

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.updater import check_and_apply_update

    auto_apply = req.method == "POST" and req.params.get("apply", "0") == "1"

    logger.info(f"CheckUpdate called (auto_apply={auto_apply})")
    result = check_and_apply_update(auto_apply=auto_apply)

    return func.HttpResponse(
        json.dumps(result, indent=2),
        status_code=200,
        mimetype="application/json",
    )
