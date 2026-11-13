import json
import logging

import azure.functions as func


# Attempts before a scan request is dropped. A request that finds another scan
# running is retried (host.json: queues visibilityTimeout 5 min), so a change
# that asked for a scan (e.g. an exclusion edit) is applied after that scan.
MAX_ATTEMPTS = 5


def main(msg: func.QueueMessage, context: func.Context = None) -> None:
    """Queue-triggered worker that runs the full resource scan, or switches log
    collection off (shared/collection.py). Decoupled from the HTTP trigger to
    avoid the 230s gateway timeout. The function timeout (host.json) is 10 minutes."""
    from shared.log_context import bind
    bind(context)
    from shared import collection, teardown
    body = msg.get_body().decode("utf-8")
    dequeue_count = msg.dequeue_count
    action = _action(body)
    delete = action == teardown.DELETE_ACTION
    stop = delete or action == collection.STOP_ACTION
    logging.info("ScanWorker: Picked up %s request (attempt %d): %s",
                 action or "scan", dequeue_count, body)

    if dequeue_count > MAX_ATTEMPTS:
        # The lock is not cleared here: it may belong to a scan that is running
        # now; a stale one expires on its own (try_acquire_scan_lock).
        logging.warning("ScanWorker: Message retried %d times — giving up", dequeue_count)
        if delete:
            teardown.mark_abandoned()
        elif stop:
            collection.mark_stop_abandoned()
        try:
            from shared.debug_logger import log_event
            log_event("warning", "ScanWorker",
                      f"Request abandoned after {dequeue_count} attempts (likely timing out)")
        except Exception:
            pass
        return

    if not stop:
        from shared.config_store import collection_enabled
        if not collection_enabled():
            logging.info("ScanWorker: Log collection is off — scan request dropped")
            return
        if _already_scanned_since(body):
            # A scan that started after this request covers it: for example the
            # first scan asked for by both Site24x7 and the collector's own start.
            logging.info("ScanWorker: A scan started after this request — nothing to do")
            return

    try:
        from shared.debug_logger import log_event
        log_event("info", "ScanWorker",
                  f"{'Stopping log collection' if stop else 'Scan starting'} (attempt {dequeue_count})")
    except Exception:
        pass

    # Concurrency guard: if a scan is already running (timer-triggered), skip.
    # The timer path calls try_acquire_scan_lock; the queue path mirrors it so
    # TriggerScan + scheduled scan can't overlap.
    try:
        from shared.config_store import try_acquire_scan_lock
        acquired = try_acquire_scan_lock()
    except Exception as guard_err:
        logging.warning("ScanWorker: scan lock check failed (%s)", guard_err)
        acquired = False
    if not acquired:
        # Another scan (or Remove All) is running, or the lock is unreadable:
        # let the queue redeliver this request instead of dropping it.
        logging.info("ScanWorker: Another scan in progress — retrying later")
        try:
            from shared.debug_logger import log_event
            log_event("info", "ScanWorker",
                      f"Request waits for a running scan (attempt {dequeue_count})")
        except Exception:
            pass
        raise RuntimeError("Scan lock busy; the queue retries this request")

    if delete:
        try:
            outcome = teardown.delete_collector()
        finally:
            _clear_in_progress()
        if outcome.get("state") != "deleting":
            from shared.registration import register_with_site24x7
            register_with_site24x7()
        return

    if stop:
        try:
            collection.stop_collection()
        finally:
            _clear_in_progress()
            from shared.registration import register_with_site24x7
            register_with_site24x7()
        return

    try:
        from DiagSettingsManager import run_scan

        # Scan now (and switching collection on) checks every tracked setting in Azure.
        result = run_scan(full_reconcile=_source(body) in ("dashboard", collection.START_SOURCE))

        logging.info(
            "ScanWorker: Scan complete — configured=%s, errors=%s",
            result.get("newly_configured", "?"),
            result.get("errors", "?"),
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logging.error("ScanWorker: Scan failed: %s\n%s", str(e), tb)
        _clear_in_progress(error_msg=f"{e}\n{tb[-500:]}")
        try:
            from shared.debug_logger import log_event
            log_event("error", "ScanWorker", f"Scan failed: {e}",
                      {"traceback": tb[-1000:]})
        except Exception:
            pass
    finally:
        from shared.registration import register_with_site24x7
        register_with_site24x7()


def _action(body: str) -> str:
    try:
        return str(json.loads(body).get("action") or "")
    except Exception:
        return ""


def _source(body: str) -> str:
    try:
        return str(json.loads(body).get("source", ""))
    except Exception:
        return ""


def _already_scanned_since(body: str) -> bool:
    """True when a scan that started after the request was made has saved its results.

    A running scan does not count: it may be stopped (an update, a restart, the
    function timeout), and this request would then be lost. The lock makes the
    request wait for it instead, and the retry finds its saved results."""
    try:
        from datetime import datetime
        from shared.config_store import get_scan_state
        requested = datetime.fromisoformat(json.loads(body)["requested_at"].replace("Z", "+00:00"))
        state = get_scan_state() or {}
        started = state.get("last_scan_started_at")
        return bool(started) and datetime.fromisoformat(started.replace("Z", "+00:00")) > requested
    except Exception:
        return False  # unreadable request or state: scan, as before


def _clear_in_progress(error_msg=None):
    """Clear the in_progress flag so the Dashboard doesn't show stale state."""
    try:
        from shared.config_store import update_scan_state
        patch = {"in_progress": False}
        if error_msg:
            patch["last_error"] = error_msg
        update_scan_state(patch)
    except Exception:
        pass
