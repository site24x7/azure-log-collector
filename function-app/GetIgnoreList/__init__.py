import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.ignore_list import load_ignore_list
    from shared.azure_manager import AzureManager

    logging.info("GetIgnoreList: Fetching ignore list and available resources")

    try:
        subscription_ids = [
            s.strip()
            for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]

        ignore_list = load_ignore_list()
        azure_mgr = AzureManager()

        # Get available resources for UI dropdowns
        all_resources = azure_mgr.get_all_resources(subscription_ids)
        resource_groups = sorted(set(r.get("resource_group", "") for r in all_resources))
        locations = sorted(set(r.get("location", "") for r in all_resources))
        resource_summaries = [
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "location": r.get("location", ""),
                "resource_group": r.get("resource_group", ""),
            }
            for r in all_resources
        ]

        # Collect available tags from resources for UI suggestions
        all_tags = set()
        all_types = set()
        for r in all_resources:
            for k, v in (r.get("tags") or {}).items():
                all_tags.add(f"{k}={v}")
            if r.get("type"):
                all_types.add(r["type"])

        response = {
            "ignore_list": ignore_list,
            "available": {
                "subscriptions": subscription_ids,
                "resource_groups": resource_groups,
                "locations": locations,
                "resource_ids": resource_summaries,
                "tags": sorted(all_tags),
                "resource_types": sorted(all_types),
            },
        }

        return func.HttpResponse(
            json.dumps(response, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("GetIgnoreList: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to load ignore list"}),
            mimetype="application/json",
            status_code=500,
        )
