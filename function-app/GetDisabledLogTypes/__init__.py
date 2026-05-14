import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.config_store import (
        get_disabled_log_types, get_supported_log_types,
        get_category_resource_types,
    )

    logging.info("GetDisabledLogTypes: Fetching disabled log types")

    try:
        disabled = get_disabled_log_types()
        supported = get_supported_log_types()

        # Category → resource types mapping (built from ALL discovered resources
        # during scan, not just configured ones)
        cat_resource_types = get_category_resource_types()

        # Build category-centric view:
        #   Each card = one Azure log category (not S247 logtype)
        #   S247 logtype shown as metadata inside each card
        all_resource_types = set()
        supported_list = []
        disabled_lower = [d.lower() for d in disabled]

        if supported:
            # Identify S247 logtypes that have sub-category entries
            logtype_has_subcats = set()
            for key, info in supported.items():
                if isinstance(info, dict):
                    s247_lt = info.get("logtype", key)
                    if key != s247_lt:
                        logtype_has_subcats.add(s247_lt)

            for key, info in supported.items():
                if not isinstance(info, dict) or "display_name" not in info:
                    continue
                s247_logtype = info.get("logtype", key)

                # Skip parent entries when their sub-categories exist
                # separately (avoids duplicate cards)
                if key == s247_logtype and s247_logtype in logtype_has_subcats:
                    continue

                rt = sorted(cat_resource_types.get(key, []))
                all_resource_types.update(rt)

                # Recover original Azure category name (un-normalized)
                original_name = key
                for cat in info.get("log_categories", []):
                    if cat.replace("-", "").replace("_", "").lower() == key:
                        original_name = cat
                        break

                supported_list.append({
                    "category": original_name,
                    "category_key": key,
                    "s247_logtype": s247_logtype,
                    "s247_display_name": info.get("display_name", s247_logtype),
                    "resource_types": rt,
                    "disabled": key.lower() in disabled_lower,
                })

        supported_list.sort(key=lambda x: x["category"].lower())

        return func.HttpResponse(
            json.dumps({
                "disabled_logtypes": disabled,
                "supported_types": supported_list,
                "all_resource_types": sorted(all_resource_types),
            }, indent=2),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.error("GetDisabledLogTypes: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": "Failed to load disabled log types"}),
            mimetype="application/json",
            status_code=500,
        )
