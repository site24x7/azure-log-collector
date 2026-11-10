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

# Checked once per worker process: a restart, a redeploy or a new instance.
_checked = False


def _minutes_since(timestamp: str) -> Optional[float]:
    try:
        then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - then).total_seconds() / 60
    except Exception:
        return None


def startup_scan_reason() -> Optional[str]:
    """Why this worker start needs a scan ("installation", "update", "restart"), or None.

    Only the first call in a worker process can return a reason. Never raises."""
    global _checked
    if _checked:
        return None
    _checked = True
    try:
        if os.environ.get("AUTO_SCAN_ENABLED", "true").lower() != "true":
            return None
        from shared.config_store import get_scan_state
        from shared.updater import get_local_version
        state = get_scan_state() or {}
        if state.get("in_progress"):
            return None
        if not state.get("last_scan_time"):
            return "installation"
        if state.get("collector_version") != get_local_version():
            return "update"
        # A restart: scan unless one finished recently, so a burst of restarts
        # (settings changes, new instances) does not scan again and again.
        min_gap = float(os.environ.get("MIN_SCAN_INTERVAL_MINUTES", "30") or 30)
        age = _minutes_since(state["last_scan_time"])
        if age is None or age >= min_gap:
            return "restart"
    except Exception as e:
        logger.warning("Startup scan check skipped after %s", type(e).__name__)
    return None
