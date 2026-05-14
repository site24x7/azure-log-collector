import os
import json
import logging
import traceback

import azure.functions as func


logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("GetStatus: Starting request")

    try:
        subscription_ids = [
            s.strip()
            for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]
        resource_group = os.environ.get(
            "RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg")
        )
        general_enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"
        processing_enabled = os.environ.get("PROCESSING_ENABLED", "true").lower() == "true"
        monitor_pipeline = os.environ.get("MONITOR_PIPELINE_RESOURCES", "false").lower() == "true"
        auto_scan = os.environ.get("AUTO_SCAN_ENABLED", "true").lower() == "true"
        try:
            safe_delete_days = int(os.environ.get("SAFE_DELETE_MAX_AGE_DAYS", "7"))
        except (ValueError, TypeError):
            safe_delete_days = 7
        # Load scan state from blob (primary) or app setting (fallback)
        last_scan_time = "never"
        s247_reachable = None
        scan_in_progress = False
        s247_errors = []
        scan_details = {}
        try:
            from shared.config_store import get_scan_state
            scan_state = get_scan_state()
            if scan_state.get("last_scan_time"):
                last_scan_time = scan_state["last_scan_time"]
            if "s247_reachable" in scan_state:
                s247_reachable = scan_state["s247_reachable"]
            scan_in_progress = scan_state.get("in_progress", False)
            s247_errors = scan_state.get("s247_errors", [])
            # Expose scan details for the dashboard widget
            scan_details = {
                "newly_configured": scan_state.get("newly_configured", 0),
                "updated": scan_state.get("updated", 0),
                "already_configured": scan_state.get("already_configured", 0),
                "removed": scan_state.get("removed", 0),
                "skipped": scan_state.get("skipped", 0),
                "logtypes_created": scan_state.get("logtypes_created", 0),
                "errors": scan_state.get("errors", 0),
                "phase_timings": scan_state.get("phase_timings", {}),
                "total_duration": scan_state.get("total_duration", 0),
                "regions_count": scan_state.get("regions_count", 0),
                "unique_resource_types": scan_state.get("unique_resource_types", 0),
            }
        except Exception:
            pass
        if last_scan_time == "never":
            last_scan_time = os.environ.get("LAST_SCAN_TIME", "never")
        update_check_url = os.environ.get("UPDATE_CHECK_URL", "")

        logger.info(
            "GetStatus: Config loaded — subs=%s, rg=%s, processing=%s",
            subscription_ids, resource_group, processing_enabled,
        )

        status = {
            "last_scan_time": last_scan_time,
            "scan_in_progress": scan_in_progress,
            "s247_reachable": s247_reachable,
            "s247_errors": s247_errors,
            "scan_details": scan_details,
            "subscription_ids": subscription_ids,
            "general_logtype_enabled": general_enabled,
            "processing_enabled": processing_enabled,
            "monitor_pipeline_resources": monitor_pipeline,
            "auto_scan_enabled": auto_scan,
            "safe_delete_days": safe_delete_days,
            "update_check_url": bool(update_check_url),
            "provisioned_regions": [],
            "resources": {"total": 0, "active": 0, "ignored": 0},
            "errors": [],
        }

        # Load ignore list
        try:
            from shared.ignore_list import load_ignore_list
            ignore_list = load_ignore_list()
            logger.info("GetStatus: Ignore list loaded OK")
        except Exception as e:
            logger.error("GetStatus: Failed to load ignore list: %s\n%s", e, traceback.format_exc())
            ignore_list = {"resource_groups": [], "locations": [], "resource_ids": []}
            status["errors"].append(f"ignore_list: {e}")

        # Get provisioned regions (returns dict: region → storage account name)
        try:
            from shared.region_manager import RegionManager
            region_mgr = RegionManager(subscription_ids[0] if subscription_ids else "")
            provisioned_map = region_mgr.get_provisioned_regions(resource_group)
            status["provisioned_regions"] = [
                {"region": region, "storage_account": sa_name}
                for region, sa_name in provisioned_map.items()
            ]
            logger.info("GetStatus: Provisioned regions: %s", provisioned_map)
        except Exception as e:
            logger.error("GetStatus: Failed to get regions: %s\n%s", e, traceback.format_exc())
            status["errors"].append(f"provisioned_regions: {e}")

        # List resources — use cached configured-resources count instead of live API call
        try:
            from shared.config_store import get_scan_state
            # Resource counts from scan state (populated by DiagSettingsManager)
            if scan_state.get("resources"):
                status["resources"] = scan_state["resources"]
            elif scan_state.get("total_resources"):
                status["resources"] = {
                    "total": scan_state.get("total_resources", 0),
                    "active": scan_state.get("active_resources", 0),
                    "ignored": scan_state.get("ignored_resources", 0),
                }
            logger.info("GetStatus: Resources from scan state — %s", status["resources"])
        except Exception as e:
            logger.error("GetStatus: Failed to get resource counts: %s", e)
            status["errors"].append(f"resources: {e}")

        # Log type config status
        try:
            from shared.config_store import (
                get_all_logtype_configs,
                get_disabled_log_types,
                get_configured_resources,
            )
            logtype_configs = get_all_logtype_configs()
            disabled = get_disabled_log_types()
            configured_res = get_configured_resources()
            status["logtypes"] = {
                "configured_count": len(logtype_configs),
                "configured_keys": list(logtype_configs.keys()),
                "disabled_count": len(disabled),
                "disabled": disabled,
            }
            status["configured_resources_count"] = len(configured_res)
        except Exception as e:
            logger.error("GetStatus: Failed to get logtype info: %s\n%s", e, traceback.format_exc())
            status["errors"].append(f"logtypes: {e}")

        return func.HttpResponse(
            json.dumps(status, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logger.error("GetStatus: Unhandled exception: %s\n%s", e, traceback.format_exc())
        return func.HttpResponse(
            json.dumps({"error": "Internal server error"}, indent=2),
            mimetype="application/json",
            status_code=500,
        )
