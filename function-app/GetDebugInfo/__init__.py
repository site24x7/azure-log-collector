"""Debug info endpoint — returns diagnostic data and exportable debug bundle.

GET /api/debug              — Returns debug summary (recent events, config issues, stats)
GET /api/debug?download=1   — Downloads full debug bundle as JSON file
GET /api/debug?test_s247=1  — Includes live Site24x7 connectivity test
GET /api/debug?clear=1      — Clears the debug event log
"""

import json
import logging
import os
import traceback
from datetime import datetime, timezone

import azure.functions as func

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("GetDebugInfo: Starting request")

    try:
        from shared.debug_logger import (
            get_recent_events,
            get_processing_stats,
            validate_config,
            clear_events,
        )
        from shared.config_store import (
            get_scan_state,
            get_all_logtype_configs,
            get_disabled_log_types,
            get_configured_resources,
        )

        # Handle clear request
        if req.params.get("clear") == "1":
            clear_events()
            return func.HttpResponse(
                json.dumps({"status": "ok", "message": "Debug events cleared"}),
                mimetype="application/json",
            )

        # ── Build debug info ──
        debug_info = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "function_app": os.environ.get("WEBSITE_SITE_NAME", "unknown"),
            "region": os.environ.get("REGION_NAME", os.environ.get("WEBSITE_REGION", "unknown")),
        }

        # 1. Config validation
        debug_info["config_issues"] = validate_config()

        # 2. Recent events (errors/warnings/info)
        debug_info["recent_events"] = get_recent_events(limit=100)
        debug_info["error_count"] = sum(
            1 for e in debug_info["recent_events"] if e.get("level") == "error"
        )
        debug_info["warning_count"] = sum(
            1 for e in debug_info["recent_events"] if e.get("level") == "warning"
        )

        # 3. Last scan state
        debug_info["scan_state"] = get_scan_state()

        # 4. Processing stats (BlobLogProcessor run history)
        debug_info["processing_runs"] = get_processing_stats(limit=20)

        # 5. Logtype config summary
        try:
            configs = get_all_logtype_configs()
            disabled = get_disabled_log_types()
            configured_res = get_configured_resources()
            debug_info["logtype_summary"] = {
                "configured_count": len(configs),
                "configured_keys": sorted(configs.keys()),
                "disabled_count": len(disabled),
                "disabled": disabled,
                "configured_resources_count": len(configured_res),
            }
        except Exception as e:
            debug_info["logtype_summary"] = {"error": str(e)}

        # 6. Environment summary (safe — no secrets)
        safe_env_keys = [
            "SUBSCRIPTION_IDS", "SITE24X7_BASE_URL",
            "GENERAL_LOGTYPE_ENABLED", "PROCESSING_ENABLED",
            "SAFE_DELETE_MAX_AGE_DAYS", "UPDATE_CHECK_URL",
            "RESOURCE_GROUP_NAME", "RESOURCE_GROUP",
            "DIAG_STORAGE_SUFFIX", "WEBSITE_SITE_NAME",
            "REGION_NAME", "FUNCTIONS_WORKER_RUNTIME",
        ]
        debug_info["environment"] = {}
        for key in safe_env_keys:
            val = os.environ.get(key, "")
            if val:
                debug_info["environment"][key] = val
        # Indicate presence of secrets without revealing them
        debug_info["environment"]["SITE24X7_API_KEY"] = "***set***" if os.environ.get("SITE24X7_API_KEY") else "NOT SET"
        debug_info["environment"]["AzureWebJobsStorage"] = "***set***" if os.environ.get("AzureWebJobsStorage") else "NOT SET"

        # 7. Live S247 connectivity test (optional — slow)
        if req.params.get("test_s247") == "1":
            try:
                from shared.debug_logger import test_s247_connectivity
                debug_info["s247_connectivity"] = test_s247_connectivity()
            except Exception as e:
                debug_info["s247_connectivity"] = {"error": str(e)}

        # 8. Circuit breaker state
        try:
            from shared.site24x7_client import Site24x7Client
            client = Site24x7Client()
            debug_info["circuit_breaker"] = {
                "state": client.circuit_breaker.state,
                "failure_count": client.circuit_breaker.failure_count,
            }
        except Exception:
            pass

        # ── Download as file ──
        if req.params.get("download") == "1":
            filename = f"s247-debug-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
            return func.HttpResponse(
                json.dumps(debug_info, indent=2),
                mimetype="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                },
            )

        return func.HttpResponse(
            json.dumps(debug_info, indent=2),
            mimetype="application/json",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    except Exception as e:
        logger.error("GetDebugInfo: Unhandled exception: %s\n%s", e, traceback.format_exc())
        return func.HttpResponse(
            json.dumps({"error": "Internal server error"}, indent=2),
            mimetype="application/json",
            status_code=500,
        )
