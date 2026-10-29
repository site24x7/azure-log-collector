"""Resource filters ("ignore list") stored in Azure Blob Storage.

What is collected is chosen with include lists: subscriptions, regions
(locations) and resource types. An empty list means all of them, including
ones that appear later. The subscription that hosts the collector is always
included. Tags can be included and excluded; single resources can be excluded
by ID (dashboard). It is persisted as a JSON blob in the Function App's
storage account.

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

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

CONTAINER_NAME = "config"
BLOB_NAME = "ignore-list.json"

# Resource filters chosen on the deployment form (template parameter → app
# setting). Used once: saved as the ignore list when none exists yet, so later
# edits from Site24x7 (including clearing every filter) always win.
INITIAL_FILTERS_SETTING = "INITIAL_RESOURCE_FILTERS"

# Include lists (empty = all) and the exclusion of single resources by ID.
LIST_KEYS = ("include_subscriptions", "include_locations", "include_resource_types", "resource_ids")

_EMPTY_IGNORE_LIST: Dict = {
    "include_subscriptions": [],
    "include_locations": [],
    "include_resource_types": [],
    "resource_ids": [],
    "tags": {"include": [], "exclude": []},
}

# Exclusion lists of earlier versions. Empty ones are dropped; one with rules cannot be
# turned into include lists, so the filters count as unreadable (scans stop) rather
# than silently collecting what it excluded.
_LEGACY_EXCLUDE_KEYS = ("subscriptions", "resource_groups", "locations", "resource_types")


class LegacyFilters(ValueError):
    """Saved filters use the earlier exclusion lists."""


def _migrate_legacy(ignore_list: Dict) -> Dict:
    for key in _LEGACY_EXCLUDE_KEYS:
        if key in ignore_list:
            if ignore_list[key]:
                raise LegacyFilters(key)
            del ignore_list[key]
    for key in LIST_KEYS:
        ignore_list.setdefault(key, [])
    return _migrate_tags(ignore_list)


def _hosting_subscription() -> str:
    """The subscription that hosts the collector: always collected from."""
    first = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0]
    return first.strip().lower()


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


# Maximum items per list to prevent denial-of-service via oversized payloads
_MAX_LIST_ITEMS = 1000
_MAX_STRING_LEN = 500

_ALLOWED_KEYS = set(LIST_KEYS) | {"tags"}


def validate_ignore_list(body) -> str:
    """Validate ignore list structure. Returns error string or empty on success.

    Shared by the update endpoint and the deployment's initial filters."""
    if not isinstance(body, dict):
        return "Body must be a JSON object"

    unknown = set(body.keys()) - _ALLOWED_KEYS
    if unknown:
        return f"Unknown keys: {', '.join(sorted(unknown))}"

    for key in LIST_KEYS:
        if key not in body:
            continue
        val = body[key]
        if not isinstance(val, list):
            return f"'{key}' must be an array"
        if len(val) > _MAX_LIST_ITEMS:
            return f"'{key}' exceeds maximum of {_MAX_LIST_ITEMS} items"
        for item in val:
            if not isinstance(item, str):
                return f"'{key}' items must be strings"
            if len(item) > _MAX_STRING_LEN:
                return f"'{key}' item exceeds maximum length of {_MAX_STRING_LEN}"

    if "tags" in body:
        tags = body["tags"]
        if isinstance(tags, list):
            if len(tags) > _MAX_LIST_ITEMS:
                return f"'tags' exceeds maximum of {_MAX_LIST_ITEMS} items"
            for t in tags:
                if not isinstance(t, str):
                    return "'tags' items must be strings"
                if len(t) > _MAX_STRING_LEN:
                    return f"'tags' item exceeds maximum length of {_MAX_STRING_LEN}"
        elif isinstance(tags, dict):
            unknown_tags = set(tags.keys()) - {"include", "exclude"}
            if unknown_tags:
                return f"Unknown tags keys: {', '.join(sorted(unknown_tags))}"
            for sub_key in ("include", "exclude"):
                if sub_key in tags:
                    sub = tags[sub_key]
                    if not isinstance(sub, list):
                        return f"'tags.{sub_key}' must be an array"
                    if len(sub) > _MAX_LIST_ITEMS:
                        return f"'tags.{sub_key}' exceeds maximum of {_MAX_LIST_ITEMS} items"
                    for t in sub:
                        if not isinstance(t, str):
                            return f"'tags.{sub_key}' items must be strings"
                        if len(t) > _MAX_STRING_LEN:
                            return f"'tags.{sub_key}' item exceeds maximum length of {_MAX_STRING_LEN}"
        else:
            return "'tags' must be an array or object with include/exclude"

    return ""


class IgnoreListUnavailable(RuntimeError):
    """The ignore list exists but could not be read; treating it as empty would
    configure resources the user excluded."""


def load_ignore_list(strict: bool = False) -> Dict:
    """Load the ignore list from blob storage.

    Returns an empty-list structure if the blob does not exist or cannot
    be read.  Automatically migrates legacy flat tag lists to the new
    include/exclude structure.

    With ``strict`` (scans), a read failure other than "not found" raises
    IgnoreListUnavailable instead of returning an empty list.
    """
    blob_client = _get_blob_client()
    if blob_client is None:
        if strict:
            raise IgnoreListUnavailable("storage is not configured")
        return copy.deepcopy(_EMPTY_IGNORE_LIST)

    try:
        data = blob_client.download_blob().readall()
        ignore_list = _migrate_legacy(json.loads(data))
        logger.info("Loaded ignore list from blob storage")
        return ignore_list
    except LegacyFilters as e:
        logger.error("Saved filters use the earlier exclusion lists (%s): save new filters", e)
        if strict:
            raise IgnoreListUnavailable("legacy exclusion filters") from e
        return copy.deepcopy(_EMPTY_IGNORE_LIST)
    except Exception as e:
        # A missing blob or a missing container (a new collector) means nothing is saved yet.
        missing = isinstance(e, ResourceNotFoundError) or "BlobNotFound" in str(e) or "not found" in str(e).lower()
        if not missing:
            logger.error(f"Failed to load ignore list: {e}")
            if strict:
                raise IgnoreListUnavailable(type(e).__name__) from e
            return copy.deepcopy(_EMPTY_IGNORE_LIST)

    # None saved yet: the deployment's filters, if it has any, become the list.
    try:
        seeded = _save_initial_filters(blob_client)
    except Exception as e:
        # Never collect everything instead of what the customer chose.
        logger.error("Initial resource filters not applied: %s", type(e).__name__)
        if strict:
            raise IgnoreListUnavailable("initial filters: " + type(e).__name__) from e
        return copy.deepcopy(_EMPTY_IGNORE_LIST)
    if seeded is not None:
        return seeded
    logger.info("Ignore-list blob not found — returning empty ignore list")
    return copy.deepcopy(_EMPTY_IGNORE_LIST)


def _initial_filters():
    """The deployment's filters as an ignore list, or None when it has none.

    Raises ValueError when they are malformed.
    """
    raw = os.environ.get(INITIAL_FILTERS_SETTING, "").strip()
    if not raw:
        return None
    # Earlier versions' exclusion keys: empty ones are dropped, others raise LegacyFilters.
    filters = json.loads(raw)
    if isinstance(filters, dict):
        filters = _migrate_legacy(filters)
    err = validate_ignore_list(filters)
    if err:
        raise ValueError(err)
    ignore_list = copy.deepcopy(_EMPTY_IGNORE_LIST)
    ignore_list.update(_migrate_tags(filters))
    ignore_list = _migrate_legacy(ignore_list)
    has_rules = any(ignore_list[key] for key in ignore_list if key != "tags") \
        or ignore_list["tags"]["include"] or ignore_list["tags"]["exclude"]
    return ignore_list if has_rules else None


def _save_initial_filters(blob_client):
    """Save the deployment's filters as the ignore list, only if none exists.

    Create-only, so workers starting together write it once; when another
    writer won, its saved list is the one returned. None when there are no
    deployment filters.
    """
    filters = _initial_filters()
    if filters is None:
        return None
    _ensure_container()
    try:
        blob_client.upload_blob(json.dumps(filters, indent=2), overwrite=False)
        logger.info("Saved the deployment's resource filters as the ignore list")
        return filters
    except ResourceExistsError:
        return _migrate_legacy(json.loads(blob_client.download_blob().readall()))


def _ensure_container() -> None:
    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if not conn_str:
        raise RuntimeError("AzureWebJobsStorage is not set")
    container_client = BlobServiceClient.from_connection_string(conn_str).get_container_client(CONTAINER_NAME)
    try:
        container_client.create_container()
    except ResourceExistsError:
        pass


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


def _tag_value(resource_tags: Dict, tag_key: str):
    """The resource's value for this tag, or None when it has no such tag.
    Azure tag names are case-insensitive (Owner and owner are one tag)."""
    wanted = tag_key.lower()
    for name, value in (resource_tags or {}).items():
        if str(name).lower() == wanted:
            return "" if value is None else str(value)
    return None


def _tag_matches(resource_tags: Dict, tag_rule: str) -> bool:
    """Check if a single tag rule matches the resource's tags: ``key`` (any value)
    or ``key=value`` (``key=`` is an empty value). Names and values match
    case-insensitively; a resource without the tag never matches."""
    if "=" in tag_rule:
        tag_key, tag_val = tag_rule.split("=", 1)
        value = _tag_value(resource_tags, tag_key)
        return value is not None and value.lower() == tag_val.lower()
    return _tag_value(resource_tags, tag_rule) is not None


def is_ignored(resource: Dict, ignore_list: Dict) -> bool:
    """Check if a resource should be excluded from log collection.

    A resource is ignored when:
    1. Its ID is in ``resource_ids``, **OR**
    2. An include list (subscriptions, locations, resource types) has entries
       and the resource is not in it (the hosting subscription is always
       included), **OR**
    3. It matches an exclude tag, or include tags are defined and it matches
       none of them. Exclude tags always take priority over include tags.

    ``resource`` is expected to have ``id``, ``location``, ``subscription_id``,
    ``type``, and ``tags`` keys; a rule whose detail is missing (an ID-only
    resource) does not apply.
    """
    resource_id = resource.get("id", "").lower()
    location = resource.get("location", "").lower()
    subscription_id = resource.get("subscription_id", "").lower()
    resource_type = resource.get("type", "").lower()
    resource_tags = resource.get("tags") or {}

    # If subscription_id isn't a direct field, try parsing from the resource id
    if not subscription_id and resource_id:
        subscription_id = _extract_sub_from_id(resource_id).lower()

    ignored_ids = [rid.lower() for rid in (ignore_list.get("resource_ids") or [])]
    if resource_id in ignored_ids:
        return True

    subscriptions = {s.lower() for s in (ignore_list.get("include_subscriptions") or [])}
    if subscriptions and subscription_id and subscription_id != _hosting_subscription() \
            and subscription_id not in subscriptions:
        return True

    # Regions choose regional resources; global ones (DNS, Front Door, ...) have no region to pick.
    locations = {loc.lower() for loc in (ignore_list.get("include_locations") or [])}
    if locations and location and location != "global" and location not in locations:
        return True

    resource_types = {t.lower() for t in (ignore_list.get("include_resource_types") or [])}
    if resource_types and resource_type and resource_type not in resource_types:
        return True

    # ── Tag-based filtering (include/exclude) ──
    tags_config = ignore_list.get("tags") or {}

    # Support legacy flat list format
    if isinstance(tags_config, list):
        tags_config = {"include": [], "exclude": tags_config}

    exclude_tags = tags_config.get("exclude") or []
    include_tags = tags_config.get("include") or []

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
    for key in LIST_KEYS:
        if key not in ignore_list:
            ignore_list[key] = []
    # Ensure tags has include/exclude structure
    ignore_list = _migrate_tags(ignore_list)
    return save_ignore_list(ignore_list)


def _extract_sub_from_id(resource_id: str) -> str:
    """Extract subscription ID from a fully-qualified Azure resource ID."""
    parts = resource_id.strip("/").split("/")
    for i, part in enumerate(parts):
        if part.lower() == "subscriptions" and i + 1 < len(parts):
            return parts[i + 1]
    return ""
