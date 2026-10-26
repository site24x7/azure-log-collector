"""AutoUpdater — Timer trigger that checks for updates daily at 3 AM UTC.

It installs an update only when AUTO_UPDATE_ENABLED is true (off by default);
otherwise it records what is available for an admin to install."""

import logging

import azure.functions as func

logger = logging.getLogger(__name__)


def main(timer: func.TimerRequest) -> None:
    from shared.updater import automatic_updates_enabled, check_and_apply_update

    # Audit logging is best-effort — import inline so AutoUpdater still works
    # if debug_logger has issues.
    try:
        from shared.debug_logger import log_event
    except Exception:  # pragma: no cover — defensive
        def log_event(*_a, **_kw):  # type: ignore
            return None

    if timer.past_due:
        logger.info("AutoUpdater timer is past due — running now")

    automatic = automatic_updates_enabled()
    logger.info("AutoUpdater: checking for updates (automatic install %s) ...", "on" if automatic else "off")
    result = check_and_apply_update(auto_apply=automatic)
    logger.info(f"AutoUpdater result: {result}")

    action = result.get("action", "unknown")

    # Audit-log every AutoUpdater run so ops can see history even if the
    # new build breaks and Function-level logging looks weird afterwards.
    try:
        log_event(
            "info", "AutoUpdater", f"Auto-update run: {action}",
            {"action": action, "automatic_install": automatic, "local_version": result.get("local_version"),
             "remote_version": result.get("remote_version"), "message": result.get("message", "")},
        )
    except Exception as e:
        logger.warning("Could not audit-log AutoUpdater run: %s", e)

    if action == "deployed":
        # Submitted, not finished: Azure installs the package and restarts the
        # app, and the new version records confirmed_at on its first run
        # (update_state in /api/status). This process may be stopped by then.
        logger.info(
            f"Update from {result['local_version']} to {result['remote_version']} submitted; "
            "the new version confirms itself on its first run"
        )
    elif action == "deploy_failed":
        logger.error(
            f"Update deployment failed: {result.get('deploy_result', {}).get('error', 'unknown')}"
        )
    elif action == "update_available":
        logger.info(
            f"Update {result.get('remote_version')} is available; automatic install is off "
            "(AUTO_UPDATE_ENABLED), so an admin installs it from Site24x7"
        )
    elif action == "up_to_date":
        logger.info(f"Already on latest version ({result['local_version']})")
    else:
        logger.info(f"Update check: {result.get('message', action)}")
