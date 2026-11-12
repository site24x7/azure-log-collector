"""Resources with this collector's diagnostic setting.

GET /api/configured-resources                       every resource (the collector's dashboard)
GET /api/configured-resources?page=1&per_page=25    one page, sorted by name
    &type=Microsoft.Web/sites                       only that resource type (any case)

Every answer carries ``type_counts`` (resource type -> resources), over all of
them whatever the filter, so a page can show the counts and a type picker from
one small request.
"""

import json
import logging

import azure.functions as func


logger = logging.getLogger(__name__)

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 25


def resource_type(resource_id: str) -> str:
    """Microsoft.Web/sites, or Microsoft.Sql/servers/databases for a child resource."""
    parts = resource_id.strip("/").split("/")
    lowered = [p.lower() for p in parts]
    if "providers" not in lowered:
        return ""
    after = parts[len(lowered) - 1 - lowered[::-1].index("providers") + 1:]
    if len(after) < 2:
        return ""
    # namespace, then type/name pairs: keep the type of each pair
    return "/".join([after[0]] + after[1::2])


def _int_param(req, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(req.params.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Return the resources that have diagnostic settings configured."""
    logger.info("GetConfiguredResources: Starting request")

    try:
        from shared.config_store import get_configured_resources

        configured = get_configured_resources()

        resources = []
        type_counts = {}
        type_names = {}
        for resource_id, info in configured.items():
            parts = resource_id.rstrip("/").split("/")
            rtype = resource_type(resource_id)
            key = rtype.lower()
            type_names.setdefault(key, rtype)
            type_counts[key] = type_counts.get(key, 0) + 1
            provider = ""
            for i, p in enumerate(parts):
                if p.lower() == "providers" and i + 1 < len(parts):
                    provider = parts[i + 1]
                    break
            resources.append({
                "id": resource_id,
                "name": parts[-1] if parts else resource_id,
                "type": type_names[key],
                "resource_type": parts[-2] if len(parts) >= 2 else "",
                "provider": provider,
                "resource_group": _segment(parts, "resourcegroups"),
                "subscription_id": _segment(parts, "subscriptions"),
                "categories": info.get("categories", []),
                "storage_account": info.get("storage_account", ""),
                "configured_at": info.get("configured_at", ""),
            })

        resources.sort(key=lambda r: (r["name"].lower(), r["id"].lower()))
        answer = {
            "count": len(resources),
            "type_counts": {type_names[k]: v for k, v in sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))},
        }
        wanted_type = (req.params.get("type") or "").strip().lower()
        if wanted_type:
            resources = [r for r in resources if r["type"].lower() == wanted_type]
        if req.params.get("page") is not None:
            per_page = _int_param(req, "per_page", DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)
            total = len(resources)
            pages = max(1, -(-total // per_page))
            page = _int_param(req, "page", 1, 1, pages)
            answer.update({"total": total, "page": page, "per_page": per_page, "pages": pages})
            resources = resources[(page - 1) * per_page: page * per_page]
        answer["resources"] = resources

        return func.HttpResponse(json.dumps(answer, indent=2), mimetype="application/json", status_code=200)

    except Exception as e:
        logger.error("GetConfiguredResources: Error: %s", e)
        return func.HttpResponse(
            json.dumps({"error": "Failed to load configured resources"}),
            mimetype="application/json",
            status_code=500,
        )


def _segment(parts, name: str) -> str:
    for i, p in enumerate(parts):
        if p.lower() == name and i + 1 < len(parts):
            return parts[i + 1]
    return ""
