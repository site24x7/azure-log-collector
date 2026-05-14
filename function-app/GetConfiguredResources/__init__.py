import json
import logging

import azure.functions as func


logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Return the list of resources that have diagnostic settings configured."""
    logger.info("GetConfiguredResources: Starting request")

    try:
        from shared.config_store import get_configured_resources

        configured = get_configured_resources()

        # Build a summary list with key details for dashboard display
        resources = []
        for resource_id, info in configured.items():
            # Extract readable name from resource ID
            # e.g., /subscriptions/.../providers/Microsoft.Compute/virtualMachines/myVM → myVM
            parts = resource_id.rstrip("/").split("/")
            name = parts[-1] if parts else resource_id
            resource_type = ""
            if len(parts) >= 2:
                resource_type = f"{parts[-2]}"
            provider = ""
            for i, p in enumerate(parts):
                if p.lower() == "providers" and i + 1 < len(parts):
                    provider = parts[i + 1]
                    break

            resources.append({
                "id": resource_id,
                "name": name,
                "resource_type": resource_type,
                "provider": provider,
                "categories": info.get("categories", []),
                "storage_account": info.get("storage_account", ""),
                "configured_at": info.get("configured_at", ""),
            })

        # Sort by name for consistent display
        resources.sort(key=lambda r: r["name"].lower())

        return func.HttpResponse(
            json.dumps({
                "count": len(resources),
                "resources": resources,
            }, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logger.error("GetConfiguredResources: Error: %s", e)
        return func.HttpResponse(
            json.dumps({"error": "Failed to load configured resources"}),
            mimetype="application/json",
            status_code=500,
        )
