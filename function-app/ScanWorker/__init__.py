import json
import logging

import azure.functions as func


def main(msg: func.QueueMessage) -> None:
    """Queue-triggered worker that runs the full resource scan.
    Decoupled from the HTTP trigger to avoid the 230s gateway timeout.
    The function timeout (host.json) is 10 minutes."""
    body = msg.get_body().decode("utf-8")
    dequeue_count = msg.dequeue_count
    logging.info("ScanWorker: Picked up scan request (attempt %d): %s", dequeue_count, body)

    if dequeue_count > 2:
        logging.warning("ScanWorker: Message retried %d times — giving up", dequeue_count)
        _clear_in_progress()
        try:
            from shared.debug_logger import log_event
            log_event("warning", "ScanWorker",
                      f"Scan abandoned after {dequeue_count} attempts (likely timing out)")
        except Exception:
            pass
        return

    try:
        from shared.debug_logger import log_event
        log_event("info", "ScanWorker",
                  f"Scan starting (attempt {dequeue_count})")
    except Exception:
        pass

    # Concurrency guard: if a scan is already running (timer-triggered), skip.
    # The timer path calls try_acquire_scan_lock; the queue path mirrors it so
    # TriggerScan + scheduled scan can't overlap.
    try:
        from shared.config_store import try_acquire_scan_lock
        if not try_acquire_scan_lock():
            logging.info("ScanWorker: Another scan in progress — skipping")
            try:
                from shared.debug_logger import log_event
                log_event("info", "ScanWorker",
                          "On-demand scan skipped — concurrent scan in progress")
            except Exception:
                pass
            return
    except Exception as guard_err:
        logging.warning(
            "ScanWorker: scan lock check failed (%s) — proceeding",
            guard_err,
        )

    try:
        from DiagSettingsManager import run_scan

        result = run_scan()

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
