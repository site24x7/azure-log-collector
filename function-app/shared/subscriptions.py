"""The subscriptions the collector works in.

The deployment lists them in SUBSCRIPTION_IDS (the hosting subscription first).
Any other enabled subscription the collector's managed identity can see is
monitored too: granting the identity Reader and Monitoring Contributor on a
new subscription adds it at the next scan, without redeploying. Which of them
logs are collected from is chosen with the include_subscriptions filter.
"""

import logging
import os
import uuid
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_NAME_MAX = 64


def _normal(raw) -> Optional[str]:
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def configured_subscription_ids() -> List[str]:
    """SUBSCRIPTION_IDS, in order (the hosting subscription first), without duplicates."""
    result: List[str] = []
    for raw in os.environ.get("SUBSCRIPTION_IDS", "").split(","):
        sub = _normal(raw)
        if sub and sub not in result:
            result.append(sub)
    return result


def monitored_subscriptions() -> Tuple[List[str], Dict[str, str], Optional[str]]:
    """The configured subscriptions, then the other enabled ones the managed
    identity can see, and their display names (as the Azure portal shows them).
    The third value is set when the list could not be read: discovery is then
    incomplete, so a scan removes nothing for subscriptions it did not see."""
    configured = configured_subscription_ids()
    # Only with a managed identity (on Azure); elsewhere the credential chain is slow to fail.
    if not (os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT")):
        return configured, {}, None
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resource import SubscriptionClient

        extra = []
        names: Dict[str, str] = {}
        for sub in SubscriptionClient(DefaultAzureCredential()).subscriptions.list():
            sub_id = _normal(getattr(sub, "subscription_id", ""))
            if not sub_id:
                continue
            name = _CONTROL_CHARS.sub("", getattr(sub, "display_name", "") or "").strip()[:_NAME_MAX]
            if name:
                names[sub_id] = name
            state = str(getattr(sub, "state", "") or "")
            if sub_id not in configured and sub_id not in extra and state.lower().endswith("enabled"):
                extra.append(sub_id)
        ids = configured + sorted(extra, key=lambda s: (names.get(s, "").lower(), s))
        return ids, {s: names[s] for s in ids if s in names}, None
    except Exception as e:
        logger.warning("Could not list the subscriptions the collector can access (%s)", type(e).__name__)
        return configured, {}, f"subscription list: {type(e).__name__}"
