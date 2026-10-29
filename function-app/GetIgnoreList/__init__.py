import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.ignore_list import load_ignore_list
    from shared.config_store import get_resource_inventory

    logging.info("GetIgnoreList: Fetching ignore list and filter choices")

    try:
        subscription_ids = [
            s.strip()
            for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]

        ignore_list = load_ignore_list()
        # Choices come from the last scan's inventory: listing the subscription
        # on every request is too slow for large environments.
        inventory = get_resource_inventory() or {}

        response = {
            "ignore_list": ignore_list,
            "available": {
                "subscriptions": inventory.get("subscriptions") or subscription_ids,
                "subscription_names": inventory.get("subscription_names") or {},
                "locations": inventory.get("locations", []),
                "resource_ids": [],
                "tags": inventory.get("tags", []),
                "resource_types": inventory.get("resource_types", []),
            },
            "available_updated_at": inventory.get("updated_at"),
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
