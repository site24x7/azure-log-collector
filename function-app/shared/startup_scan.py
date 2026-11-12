"""Scan once when a worker starts after an installation, an update or a restart.

The scan timer's runOnStartup does not fire reliably on the Flex Consumption plan,
so the first BlobLogProcessor run of each worker (every 2 minutes) asks here
whether a scan is due and, if so, queues one like Scan now does. The scan lock
and ScanWorker's "already scanned since the request" check keep several workers
starting at once from running more than one scan.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Decided once per worker process (a restart, a redeploy or a new instance).
_checked = False

# host.json functionTimeout: a scan flagged in progress for longer has ended
# without clearing it (an update or a restart stops a running scan).
SCAN_TIMEOUT_MINUTES = 10


def _minutes_since(timestamp: str) -> Optional[float]:
    try:
        then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - then).total_seconds() / 60
    except Exception:
        return None


def startup_scan_reason() -> Optional[str]:
    """Why this worker start needs a scan ("installation", "update", "restart"), or None.

    Decided once per worker process; while a scan that may still be running
    holds the flag, or the state cannot be read, the next run asks again.
    Never raises."""
    global _checked
    if _checked:
        return None
    try:
        if os.environ.get("AUTO_SCAN_ENABLED", "true").lower() != "true":
            _checked = True
            return None
        from shared.config_store import get_collection_state, get_scan_state
        from shared.updater import get_local_version
        if get_collection_state(strict=True).get("enabled") is not True:
            # Switched off in Site24x7; switching it on queues its own scan.
            _checked = True
            return None
        # Strict: an unreadable state must not read as "never scanned".
        state = get_scan_state(strict=True)
        reason = _reason(state, get_local_version())
    except Exception as e:
        logger.warning("Startup scan check postponed after %s", type(e).__name__)
        return None
    if reason == "running":
        return None
    _checked = True
    return reason


def postpone() -> None:
    """The scan request could not be queued: decide again on the next run."""
    global _checked
    _checked = False


def _reason(state: dict, version: str) -> Optional[str]:
    if not state.get("last_scan_time"):
        reason = "installation"
    elif state.get("collector_version") != version:
        reason = "update"
    else:
        # A restart: scan unless one finished recently, so a burst of restarts
        # (settings changes, new instances) does not scan again and again.
        min_gap = float(os.environ.get("MIN_SCAN_INTERVAL_MINUTES", "30") or 30)
        age = _minutes_since(state["last_scan_time"])
        reason = "restart" if age is None or age >= min_gap else None
    # A scan of this version that may still be running covers a new start; decide
    # when it ends. An older version's scan never covers an update, and a flag older
    # than the function timeout is left over from a scan that was stopped.
    if state.get("in_progress") and reason != "update":
        started = _minutes_since(state.get("scan_started_at") or "")
        if started is not None and started < SCAN_TIMEOUT_MINUTES:
            return "running"
    return reason
