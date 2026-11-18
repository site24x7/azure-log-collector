"""Minimal health check — no shared imports, no SDK dependencies."""
import json
import sys
import logging

import azure.functions as func

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("HealthCheck: OK")

    # Try importing each dependency and report status
    deps = {}
    for mod_name in [
        "azure.identity",
        "azure.mgmt.resource",
        "azure.mgmt.monitor",
        "azure.mgmt.storage",
        "azure.mgmt.web",
        "azure.storage.blob",
        "requests",
        "shared",
        "shared.azure_manager",
        "shared.region_manager",
        "shared.ignore_list",
        "shared.log_parser",
        "shared.site24x7_client",
        "shared.updater",
    ]:
        try:
            __import__(mod_name)
            deps[mod_name] = "ok"
        except Exception as e:
            deps[mod_name] = f"FAILED: {type(e).__name__}"

    result = {
        "status": "alive",
        "python_version": f"Python {sys.version.split()[0]}",
        "dependencies": deps,
        "deps_ok": all(v == "ok" for v in deps.values()),
    }

    return func.HttpResponse(
        json.dumps(result, indent=2),
        mimetype="application/json",
        status_code=200,
    )
