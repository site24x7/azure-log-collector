"""Multi-level ignore list stored in Azure Blob Storage.

The ignore list allows filtering resources by subscription, resource group,
location, resource type, tag, or specific resource ID.  It is persisted as a
JSON blob in the Function App's storage account.

Tag filtering supports both **include** and **exclude** modes:

- **Include tags**: If any include tags are defined, *only* resources that
  match at least one include tag are eligible for log collection.  Acts as
  an allow-list.
- **Exclude tags**: Resources matching any exclude tag are always skipped,
  even if they also match an include tag.  Exclude takes precedence.
- If the include list is empty, all resources pass the include filter
  (i.e. no allow-list restriction — only the exclude list applies).

Each tag rule can be ``"key=value"`` (exact match) or ``"key"`` (matches
any value for that key).
"""

import copy
import json
import logging
import os
from typing import Dict

from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

CONTAINER_NAME = "config"
BLOB_NAME = "ignore-list.json"

_EMPTY_IGNORE_LIST: Dict = {
    "resource_groups": [],
    "locations": [],
    "resource_ids": [],
    "subscriptions": [],
    "tags": {"include": [], "exclude": []},
    "resource_types": [],
}


def _get_blob_client():
    """Create a BlobClient for the ignore-list blob."""
    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if not conn_str:
        logger.error("AzureWebJobsStorage environment variable is not set")
        return None
    service_client = BlobServiceClient.from_connection_string(conn_str)
    return service_client.get_blob_client(
        container=CONTAINER_NAME, blob=BLOB_NAME
    )


def _migrate_tags(ignore_list: Dict) -> Dict:
    """Migrate legacy flat tags list to include/exclude structure.

    Old format: ``"tags": ["env=dev", "temporary"]``
    New format: ``"tags": {"include": [], "exclude": ["env=dev", "temporary"]}``

    If tags is already a dict with include/exclude keys, returns unchanged.
    """
    tags = ignore_list.get("tags")
    if isinstance(tags, list):
        ignore_list["tags"] = {"include": [], "exclude": tags}
    elif not isinstance(tags, dict):
        ignore_list["tags"] = {"include": [], "exclude": []}
    else:
        tags.setdefault("include", [])
        tags.setdefault("exclude", [])
    return ignore_list


def load_ignore_list() -> Dict:
    """Load the ignore list from blob storage.

    Returns an empty-list structure if the blob does not exist or cannot
    be read.  Automatically migrates legacy flat tag lists to the new
    include/exclude structure.
    """
    blob_client = _get_blob_client()
    if blob_client is None:
        return copy.deepcopy(_EMPTY_IGNORE_LIST)

    try:
        data = blob_client.download_blob().readall()
        ignore_list = json.loads(data)
        ignore_list = _migrate_tags(ignore_list)
        logger.info("Loaded ignore list from blob storage")
        return ignore_list
    except Exception as e:
        if "BlobNotFound" in str(e) or "not found" in str(e).lower():
            logger.info(
                "Ignore-list blob not found — returning empty ignore list"
            )
        else:
            logger.error(f"Failed to load ignore list: {e}")
        return copy.deepcopy(_EMPTY_IGNORE_LIST)


def save_ignore_list(ignore_list: Dict) -> bool:
    """Save the ignore list to blob storage.

    Creates the container if it does not exist.
    """
    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if not conn_str:
        logger.error("AzureWebJobsStorage environment variable is not set")
        return False

    try:
        service_client = BlobServiceClient.from_connection_string(conn_str)
        container_client = service_client.get_container_client(CONTAINER_NAME)
        if not container_client.exists():
            container_client.create_container()
            logger.info(f"Created blob container '{CONTAINER_NAME}'")

        blob_client = container_client.get_blob_client(BLOB_NAME)
        blob_client.upload_blob(
            json.dumps(ignore_list, indent=2),
            overwrite=True,
        )
        logger.info("Saved ignore list to blob storage")
        return True
    except Exception as e:
        logger.error(f"Failed to save ignore list: {e}")
        return False


def _tag_matches(resource_tags: Dict, tag_rule: str) -> bool:
    """Check if a single tag rule matches the resource's tags."""
    if "=" in tag_rule:
        tag_key, tag_val = tag_rule.split("=", 1)
        return resource_tags.get(tag_key, "").lower() == tag_val.lower()
    else:
        return tag_rule in resource_tags


def is_ignored(resource: Dict, ignore_list: Dict) -> bool:
    """Check if a resource should be excluded from log collection.

    A resource is ignored when:
    1. It matches any dimension rule (subscription, resource group, location,
       resource type, resource ID, or exclude tag), **OR**
    2. Include tags are defined and the resource does NOT match any of them.

    Exclude tags always take priority over include tags.

    ``resource`` is expected to have ``id``, ``location``,
    ``resource_group``, ``subscription_id``, ``type``, and ``tags`` keys.
    """
    resource_id = resource.get("id", "").lower()
    location = resource.get("location", "").lower()
    resource_group = resource.get("resource_group", "").lower()
    subscription_id = resource.get("subscription_id", "").lower()
    resource_type = resource.get("type", "").lower()
    resource_tags = resource.get("tags") or {}

    # If resource_group isn't a direct field, try parsing from the resource id
    if not resource_group and resource_id:
        resource_group = _extract_rg_from_id(resource_id).lower()

    # If subscription_id isn't a direct field, try parsing from the resource id
    if not subscription_id and resource_id:
        subscription_id = _extract_sub_from_id(resource_id).lower()

    # Check resource_ids
    ignored_ids = [rid.lower() for rid in ignore_list.get("resource_ids", [])]
    if resource_id in ignored_ids:
        return True

    # Check subscriptions
    ignored_subs = [s.lower() for s in ignore_list.get("subscriptions", [])]
    if subscription_id in ignored_subs:
        return True

    # Check resource_groups
    ignored_rgs = [rg.lower() for rg in ignore_list.get("resource_groups", [])]
    if resource_group in ignored_rgs:
        return True

    # Check locations
    ignored_locations = [
        loc.lower() for loc in ignore_list.get("locations", [])
    ]
    if location in ignored_locations:
        return True

    # Check resource_types (e.g. "Microsoft.Compute/virtualMachines")
    ignored_types = [t.lower() for t in ignore_list.get("resource_types", [])]
    if resource_type in ignored_types:
        return True

    # ── Tag-based filtering (include/exclude) ──
    tags_config = ignore_list.get("tags", {})

    # Support legacy flat list format
    if isinstance(tags_config, list):
        tags_config = {"include": [], "exclude": tags_config}

    exclude_tags = tags_config.get("exclude", [])
    include_tags = tags_config.get("include", [])

    # Exclude tags — if resource matches any, it's ignored
    for tag_rule in exclude_tags:
        if _tag_matches(resource_tags, tag_rule):
            return True

    # Include tags — if defined and resource matches NONE, it's ignored
    if include_tags:
        matched_any = any(
            _tag_matches(resource_tags, tag_rule) for tag_rule in include_tags
        )
        if not matched_any:
            return True

    return False


def get_ignore_list() -> Dict:
    """Public getter — loads and returns the current ignore list."""
    return load_ignore_list()


def update_ignore_list(ignore_list: Dict) -> bool:
    """Public setter — validates and saves an updated ignore list."""
    for key in ("resource_groups", "locations", "resource_ids", "subscriptions", "resource_types"):
        if key not in ignore_list:
            ignore_list[key] = []
    # Ensure tags has include/exclude structure
    ignore_list = _migrate_tags(ignore_list)
    return save_ignore_list(ignore_list)


def _extract_rg_from_id(resource_id: str) -> str:
    """Extract resource group name from a fully-qualified Azure resource ID."""
    parts = resource_id.strip("/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _extract_sub_from_id(resource_id: str) -> str:
    """Extract subscription ID from a fully-qualified Azure resource ID."""
    parts = resource_id.strip("/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == "subscriptions" and i + 1 < len(parts):
            return parts[i + 1]
    return ""
