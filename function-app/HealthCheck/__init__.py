"""Minimal health check — no shared imports, no SDK dependencies."""
import json
import os
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

    # If azure_test param is set, test actual Azure API calls
    if req.params.get("azure_test"):
        azure_result = {}
        try:
            from azure.identity import DefaultAzureCredential
            cred = DefaultAzureCredential()
            azure_result["credential"] = "ok"
        except Exception as e:
            azure_result["credential"] = f"FAILED: {e}"
            result["azure_test"] = azure_result
            return func.HttpResponse(json.dumps(result, indent=2), mimetype="application/json", status_code=200)

        try:
            sub_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()
            azure_result["subscription_id"] = sub_id
            from azure.mgmt.resource import ResourceManagementClient
            client = ResourceManagementClient(cred, sub_id)
            resources = []
            for r in client.resources.list():
                resources.append({"name": r.name, "type": r.type, "location": r.location})
                if len(resources) >= 5:
                    break
            azure_result["resource_count_sample"] = len(resources)
            azure_result["resources_sample"] = resources
        except Exception as e:
            azure_result["resource_list"] = f"FAILED: {type(e).__name__}: {e}"

        try:
            if resources:
                from azure.mgmt.monitor import MonitorManagementClient
                mon_client = MonitorManagementClient(cred, sub_id)
                test_resource = resources[0]
                rid = None
                # Get full resource ID
                for r in client.resources.list():
                    if r.name == test_resource["name"]:
                        rid = r.id
                        break
                if rid:
                    cats = mon_client.diagnostic_settings_category.list(rid)
                    azure_result["diag_categories_test"] = {
                        "resource": test_resource["name"],
                        "categories": [{"name": c.name, "type": str(c.category_type)} for c in cats.value]
                    }
        except Exception as e:
            azure_result["diag_categories_test"] = f"FAILED: {type(e).__name__}: {e}"

        result["azure_test"] = azure_result

    return func.HttpResponse(
        json.dumps(result, indent=2),
        mimetype="application/json",
        status_code=200,
    )
