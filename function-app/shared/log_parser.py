"""Parse Azure diagnostic log records from blob storage or event messages."""

import json
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def parse_diagnostic_records(event_body: str) -> List[Dict[str, Any]]:
    """Parse the standard Azure diagnostic log envelope.

    Azure diagnostic logs arrive as::

        {
            "records": [
                {
                    "time": "2024-01-01T00:00:00Z",
                    "resourceId": "/subscriptions/.../resourceGroups/.../providers/...",
                    "category": "AuditEvent",
                    "operationName": "...",
                    "resultType": "...",
                    "properties": { ... }
                }
            ]
        }
    """
    try:
        data = json.loads(event_body) if isinstance(event_body, str) else event_body
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Failed to parse event body: {e}")
        return []

    if not isinstance(data, dict):
        logger.error("Event body is not a JSON object")
        return []

    records = data.get("records", [])
    if not records:
        logger.warning("No 'records' field in diagnostic log envelope")
        return []

    parsed = []
    for record in records:
        parsed_record = {
            "time": record.get("time", ""),
            "resource_id": record.get("resourceId", ""),
            "category": record.get("category", ""),
            "operation_name": record.get("operationName", ""),
            "result_type": record.get("resultType", ""),
            "level": record.get("level", ""),
            "properties": record.get("properties", {}),
            "raw": record,  # keep full record for general log type
        }
        parsed.append(parsed_record)

    return parsed


def extract_resource_info(resource_id: str) -> Dict[str, str]:
    """Extract subscription, resource group, provider, and resource name from a resource ID.

    Example resource ID::

        /subscriptions/abc-123/resourceGroups/my-rg/providers/Microsoft.Compute/virtualMachines/my-vm
    """
    parts = resource_id.strip("/").split("/")
    info = {
        "subscription_id": "",
        "resource_group": "",
        "provider": "",
        "resource_type": "",
        "resource_name": "",
    }
    for i, part in enumerate(parts):
        if part.lower() == "subscriptions" and i + 1 < len(parts):
            info["subscription_id"] = parts[i + 1]
        elif part.lower() == "resourcegroups" and i + 1 < len(parts):
            info["resource_group"] = parts[i + 1]
        elif part.lower() == "providers" and i + 1 < len(parts):
            info["provider"] = parts[i + 1]
            if i + 2 < len(parts):
                info["resource_type"] = parts[i + 2]
            if i + 3 < len(parts):
                info["resource_name"] = parts[i + 3]
    return info
